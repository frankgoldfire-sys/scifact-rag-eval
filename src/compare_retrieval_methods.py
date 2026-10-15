from __future__ import annotations

import argparse
import csv
import math
import platform
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from typing import Any

import numpy as np

from run_bm25 import (
    RESULTS_DIR,
    save_csv,
    save_json,
)


# ============================================================
# 1. 比较设置
# ============================================================

METHOD_PATHS = {
    "bm25": (
        RESULTS_DIR
        / "bm25_dev_per_claim.csv"
    ),
    "dense": (
        RESULTS_DIR
        / "dense_dev_per_claim.csv"
    ),
    "hybrid": (
        RESULTS_DIR
        / "hybrid_dev_per_claim.csv"
    ),
}

METHOD_ORDER = (
    "bm25",
    "dense",
    "hybrid",
)

COMPARISON_PAIRS = (
    ("dense", "bm25"),
    ("hybrid", "bm25"),
    ("hybrid", "dense"),
)

TOP_KS = (
    1,
    3,
    5,
    10,
    20,
)

BOOTSTRAP_METRICS = (
    "reciprocal_rank",
    "recall_at_1",
    "recall_at_3",
    "recall_at_5",
    "recall_at_10",
    "recall_at_20",
    "ndcg_at_1",
    "ndcg_at_3",
    "ndcg_at_5",
    "ndcg_at_10",
    "ndcg_at_20",
)

HIT_METRICS = tuple(
    f"hit_at_{k}"
    for k in TOP_KS
)

SUMMARY_METRICS = (
    "reciprocal_rank",
    "recall_at_1",
    "hit_at_1",
    "ndcg_at_1",
    "recall_at_3",
    "hit_at_3",
    "ndcg_at_3",
    "recall_at_5",
    "hit_at_5",
    "ndcg_at_5",
    "recall_at_10",
    "hit_at_10",
    "ndcg_at_10",
    "recall_at_20",
    "hit_at_20",
    "ndcg_at_20",
)

DEFAULT_BOOTSTRAP_SAMPLES = 10_000
DEFAULT_CONFIDENCE_LEVEL = 0.95
DEFAULT_RANDOM_SEED = 20261013


# ============================================================
# 2. 命令行参数
# ============================================================

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "比较SciFact dev上的BM25、"
            "稠密检索和混合检索。"
        )
    )

    parser.add_argument(
        "--bootstrap-samples",
        type=int,
        default=DEFAULT_BOOTSTRAP_SAMPLES,
        help="Bootstrap重复次数。",
    )

    parser.add_argument(
        "--confidence-level",
        type=float,
        default=DEFAULT_CONFIDENCE_LEVEL,
        help="Bootstrap置信水平。",
    )

    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_RANDOM_SEED,
        help="随机种子。",
    )

    arguments = parser.parse_args()

    if arguments.bootstrap_samples <= 0:
        parser.error(
            "--bootstrap-samples必须是正整数。"
        )

    if not 0 < arguments.confidence_level < 1:
        parser.error(
            "--confidence-level必须位于0和1之间。"
        )

    return arguments


# ============================================================
# 3. 读取逐条结果
# ============================================================

def load_per_claim_results(
    path: Path,
) -> dict[str, dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(
            f"没有找到结果文件：{path}"
        )

    records: dict[
        str,
        dict[str, str],
    ] = {}

    with path.open(
        mode="r",
        encoding="utf-8-sig",
        newline="",
    ) as file:
        reader = csv.DictReader(file)

        if reader.fieldnames is None:
            raise ValueError(
                f"{path}没有CSV表头。"
            )

        required_fields = {
            "claim_id",
            "gold_label",
            "has_gold_evidence",
            *SUMMARY_METRICS,
        }

        missing_fields = (
            required_fields
            - set(reader.fieldnames)
        )

        if missing_fields:
            raise ValueError(
                f"{path}缺少字段："
                f"{sorted(missing_fields)}"
            )

        for row in reader:
            has_gold = str(
                row["has_gold_evidence"]
            ).strip().lower()

            if has_gold not in {
                "true",
                "1",
            }:
                continue

            claim_id = str(
                row["claim_id"]
            ).strip()

            if claim_id in records:
                raise ValueError(
                    f"{path}中claim_id "
                    f"{claim_id}重复。"
                )

            records[claim_id] = row

    if not records:
        raise ValueError(
            f"{path}中没有可评测claim。"
        )

    return records


# ============================================================
# 4. 检查三种方法是否能够配对
# ============================================================

def validate_paired_results(
    method_results: dict[
        str,
        dict[str, dict[str, str]],
    ],
) -> list[str]:
    reference_method = METHOD_ORDER[0]

    reference_ids = set(
        method_results[
            reference_method
        ].keys()
    )

    for method in METHOD_ORDER[1:]:
        current_ids = set(
            method_results[method].keys()
        )

        if current_ids != reference_ids:
            missing = (
                reference_ids
                - current_ids
            )

            extra = (
                current_ids
                - reference_ids
            )

            raise ValueError(
                f"{method}的claim集合不一致。"
                f"缺少：{sorted(missing)}；"
                f"多出：{sorted(extra)}"
            )

    for claim_id in reference_ids:
        reference_label = (
            method_results[
                reference_method
            ][claim_id]["gold_label"]
        )

        for method in METHOD_ORDER[1:]:
            current_label = (
                method_results[
                    method
                ][claim_id][
                    "gold_label"
                ]
            )

            if current_label != reference_label:
                raise ValueError(
                    f"claim {claim_id}在"
                    "不同方法中的标签不一致。"
                )

    try:
        ordered_claim_ids = sorted(
            reference_ids,
            key=int,
        )
    except ValueError:
        ordered_claim_ids = sorted(
            reference_ids
        )

    return ordered_claim_ids


# ============================================================
# 5. 提取某项指标
# ============================================================

def get_metric_values(
    method_data: dict[
        str,
        dict[str, str],
    ],
    claim_ids: list[str],
    metric_name: str,
) -> np.ndarray:
    values = np.asarray(
        [
            float(
                method_data[
                    claim_id
                ][metric_name]
            )
            for claim_id in claim_ids
        ],
        dtype=np.float64,
    )

    if not np.all(
        np.isfinite(values)
    ):
        raise ValueError(
            f"指标{metric_name}包含"
            "非有限数值。"
        )

    return values


# ============================================================
# 6. 配对Bootstrap
# ============================================================

def paired_bootstrap(
    values_a: np.ndarray,
    values_b: np.ndarray,
    bootstrap_indices: np.ndarray,
    confidence_level: float,
) -> dict[str, float | bool]:
    if values_a.shape != values_b.shape:
        raise ValueError(
            "配对Bootstrap的数组长度不一致。"
        )

    paired_differences = (
        values_a - values_b
    )

    observed_difference = float(
        np.mean(paired_differences)
    )

    bootstrap_differences = np.mean(
        paired_differences[
            bootstrap_indices
        ],
        axis=1,
    )

    alpha = 1.0 - confidence_level

    lower_bound = float(
        np.quantile(
            bootstrap_differences,
            alpha / 2.0,
        )
    )

    upper_bound = float(
        np.quantile(
            bootstrap_differences,
            1.0 - alpha / 2.0,
        )
    )

    excludes_zero = bool(
        lower_bound > 0
        or upper_bound < 0
    )

    return {
        "observed_difference": (
            observed_difference
        ),
        "ci_lower": lower_bound,
        "ci_upper": upper_bound,
        "ci_excludes_zero": (
            excludes_zero
        ),
    }


# ============================================================
# 7. 精确McNemar检验
# ============================================================

def exact_mcnemar_p_value(
    method_a_only_success: int,
    method_b_only_success: int,
) -> float:
    """
    在原假设下，两种不一致结果出现的概率均为0.5，
    因而可以使用二项分布计算双侧精确p值。
    """
    discordant_count = (
        method_a_only_success
        + method_b_only_success
    )

    if discordant_count == 0:
        return 1.0

    smaller_count = min(
        method_a_only_success,
        method_b_only_success,
    )

    lower_tail_probability = sum(
        math.comb(
            discordant_count,
            success_count,
        )
        for success_count in range(
            smaller_count + 1
        )
    ) / (2 ** discordant_count)

    return min(
        1.0,
        2.0 * lower_tail_probability,
    )


def run_mcnemar_test(
    values_a: np.ndarray,
    values_b: np.ndarray,
) -> dict[str, int | float | bool]:
    binary_a = values_a.astype(
        np.int8
    )

    binary_b = values_b.astype(
        np.int8
    )

    if not np.all(
        np.isin(
            binary_a,
            [0, 1],
        )
    ):
        raise ValueError(
            "method_a包含非二值Hit结果。"
        )

    if not np.all(
        np.isin(
            binary_b,
            [0, 1],
        )
    ):
        raise ValueError(
            "method_b包含非二值Hit结果。"
        )

    both_success = int(
        np.sum(
            (binary_a == 1)
            & (binary_b == 1)
        )
    )

    method_a_only_success = int(
        np.sum(
            (binary_a == 1)
            & (binary_b == 0)
        )
    )

    method_b_only_success = int(
        np.sum(
            (binary_a == 0)
            & (binary_b == 1)
        )
    )

    both_failure = int(
        np.sum(
            (binary_a == 0)
            & (binary_b == 0)
        )
    )

    p_value = exact_mcnemar_p_value(
        method_a_only_success=(
            method_a_only_success
        ),
        method_b_only_success=(
            method_b_only_success
        ),
    )

    return {
        "both_success": both_success,
        "method_a_only_success": (
            method_a_only_success
        ),
        "method_b_only_success": (
            method_b_only_success
        ),
        "both_failure": both_failure,
        "discordant_pairs": (
            method_a_only_success
            + method_b_only_success
        ),
        "p_value": p_value,
        "significant_at_0_05": bool(
            p_value < 0.05
        ),
    }


# ============================================================
# 8. 主程序
# ============================================================

def main() -> None:
    arguments = parse_arguments()

    print(
        "正在读取三种方法的dev逐条结果……"
    )

    method_results = {
        method: load_per_claim_results(
            METHOD_PATHS[method]
        )
        for method in METHOD_ORDER
    }

    claim_ids = validate_paired_results(
        method_results
    )

    claim_count = len(claim_ids)

    print(
        f"配对评测claim数：{claim_count}"
    )

    print(
        "Bootstrap重复次数："
        f"{arguments.bootstrap_samples}"
    )

    print(
        "随机种子："
        f"{arguments.seed}"
    )

    # --------------------------------------------------------
    # 生成一次Bootstrap下标，所有比较共用，
    # 保证实验可复现。
    # --------------------------------------------------------

    random_generator = (
        np.random.default_rng(
            arguments.seed
        )
    )

    bootstrap_indices = (
        random_generator.integers(
            low=0,
            high=claim_count,
            size=(
                arguments.bootstrap_samples,
                claim_count,
            ),
            dtype=np.int32,
        )
    )

    # --------------------------------------------------------
    # 汇总三种方法
    # --------------------------------------------------------

    summary_records: list[
        dict[str, Any]
    ] = []

    for method in METHOD_ORDER:
        summary_row: dict[
            str,
            Any,
        ] = {
            "method": method,
            "evaluated_claims": (
                claim_count
            ),
        }

        for metric_name in SUMMARY_METRICS:
            values = get_metric_values(
                method_results[method],
                claim_ids,
                metric_name,
            )

            summary_row[metric_name] = (
                float(np.mean(values))
            )

        summary_records.append(
            summary_row
        )

    # --------------------------------------------------------
    # 配对Bootstrap
    # --------------------------------------------------------

    print("正在运行配对Bootstrap……")

    bootstrap_records: list[
        dict[str, Any]
    ] = []

    for method_a, method_b in (
        COMPARISON_PAIRS
    ):
        for metric_name in (
            BOOTSTRAP_METRICS
        ):
            values_a = get_metric_values(
                method_results[method_a],
                claim_ids,
                metric_name,
            )

            values_b = get_metric_values(
                method_results[method_b],
                claim_ids,
                metric_name,
            )

            result = paired_bootstrap(
                values_a=values_a,
                values_b=values_b,
                bootstrap_indices=(
                    bootstrap_indices
                ),
                confidence_level=(
                    arguments.confidence_level
                ),
            )

            observed_difference = float(
                result[
                    "observed_difference"
                ]
            )

            if observed_difference > 0:
                favored_method = method_a
            elif observed_difference < 0:
                favored_method = method_b
            else:
                favored_method = "tie"

            bootstrap_records.append(
                {
                    "method_a": method_a,
                    "method_b": method_b,
                    "metric": metric_name,
                    "mean_a": float(
                        np.mean(values_a)
                    ),
                    "mean_b": float(
                        np.mean(values_b)
                    ),
                    "difference_a_minus_b": (
                        observed_difference
                    ),
                    "confidence_level": (
                        arguments.confidence_level
                    ),
                    "ci_lower": float(
                        result["ci_lower"]
                    ),
                    "ci_upper": float(
                        result["ci_upper"]
                    ),
                    "ci_excludes_zero": bool(
                        result[
                            "ci_excludes_zero"
                        ]
                    ),
                    "favored_method": (
                        favored_method
                    ),
                }
            )

    # --------------------------------------------------------
    # McNemar检验
    # --------------------------------------------------------

    print("正在运行精确McNemar检验……")

    mcnemar_records: list[
        dict[str, Any]
    ] = []

    for method_a, method_b in (
        COMPARISON_PAIRS
    ):
        for metric_name in HIT_METRICS:
            values_a = get_metric_values(
                method_results[method_a],
                claim_ids,
                metric_name,
            )

            values_b = get_metric_values(
                method_results[method_b],
                claim_ids,
                metric_name,
            )

            result = run_mcnemar_test(
                values_a=values_a,
                values_b=values_b,
            )

            hit_rate_a = float(
                np.mean(values_a)
            )

            hit_rate_b = float(
                np.mean(values_b)
            )

            mcnemar_records.append(
                {
                    "method_a": method_a,
                    "method_b": method_b,
                    "metric": metric_name,
                    "hit_rate_a": hit_rate_a,
                    "hit_rate_b": hit_rate_b,
                    "difference_a_minus_b": (
                        hit_rate_a
                        - hit_rate_b
                    ),
                    **result,
                }
            )

    # --------------------------------------------------------
    # 保存结果
    # --------------------------------------------------------

    summary_path = (
        RESULTS_DIR
        / "retrieval_dev_summary.csv"
    )

    bootstrap_path = (
        RESULTS_DIR
        / "retrieval_dev_bootstrap.csv"
    )

    mcnemar_path = (
        RESULTS_DIR
        / "retrieval_dev_mcnemar.csv"
    )

    statistics_path = (
        RESULTS_DIR
        / "retrieval_dev_statistics.json"
    )

    save_csv(
        summary_records,
        summary_path,
    )

    save_csv(
        bootstrap_records,
        bootstrap_path,
    )

    save_csv(
        mcnemar_records,
        mcnemar_path,
    )

    statistics_output = {
        "experiment": (
            "retrieval_method_comparison"
        ),
        "split": "dev",
        "generated_at_utc": (
            datetime.now(timezone.utc)
            .isoformat()
        ),
        "configuration": {
            "paired_evaluation": True,
            "bootstrap_samples": (
                arguments.bootstrap_samples
            ),
            "confidence_level": (
                arguments.confidence_level
            ),
            "random_seed": (
                arguments.seed
            ),
            "mcnemar_variant": (
                "two_sided_exact"
            ),
            "primary_retrieval_metric": (
                "recall_at_5"
            ),
            "primary_hit_metric": (
                "hit_at_5"
            ),
        },
        "counts": {
            "evaluated_claims": (
                claim_count
            ),
        },
        "summary": summary_records,
        "bootstrap": bootstrap_records,
        "mcnemar": mcnemar_records,
        "environment": {
            "python": (
                platform.python_version()
            ),
            "numpy": version("numpy"),
        },
    }

    save_json(
        statistics_output,
        statistics_path,
    )

    # --------------------------------------------------------
    # 输出关键结果
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print("三种检索方法的dev汇总")
    print("=" * 70)

    for row in summary_records:
        print()
        print(f"方法：{row['method']}")

        print(
            "  MRR: "
            f"{row['reciprocal_rank']:.4f}"
        )

        print(
            "  Recall@5: "
            f"{row['recall_at_5']:.4f}"
        )

        print(
            "  Hit@5: "
            f"{row['hit_at_5']:.4f}"
        )

        print(
            "  NDCG@5: "
            f"{row['ndcg_at_5']:.4f}"
        )

    print()
    print("=" * 70)
    print(
        "关键指标的配对Bootstrap"
    )
    print("=" * 70)

    key_metrics = (
        "reciprocal_rank",
        "recall_at_5",
        "ndcg_at_5",
    )

    for record in bootstrap_records:
        if record["metric"] not in (
            key_metrics
        ):
            continue

        print()
        print(
            f"{record['method_a']} - "
            f"{record['method_b']}，"
            f"{record['metric']}"
        )

        print(
            "  差值："
            f"{record['difference_a_minus_b']:+.4f}"
        )

        print(
            "  置信区间："
            f"[{record['ci_lower']:+.4f}, "
            f"{record['ci_upper']:+.4f}]"
        )

        print(
            "  区间是否排除0："
            f"{record['ci_excludes_zero']}"
        )

    print()
    print("=" * 70)
    print("Hit@5精确McNemar检验")
    print("=" * 70)

    for record in mcnemar_records:
        if record["metric"] != "hit_at_5":
            continue

        print()
        print(
            f"{record['method_a']} vs "
            f"{record['method_b']}"
        )

        print(
            "  仅method_a成功："
            f"{record['method_a_only_success']}"
        )

        print(
            "  仅method_b成功："
            f"{record['method_b_only_success']}"
        )

        print(
            "  p值："
            f"{record['p_value']:.6f}"
        )

        print(
            "  p < 0.05："
            f"{record['significant_at_0_05']}"
        )

    print()
    print("结果文件：")
    print(f"  {summary_path}")
    print(f"  {bootstrap_path}")
    print(f"  {mcnemar_path}")
    print(f"  {statistics_path}")


if __name__ == "__main__":
    main()