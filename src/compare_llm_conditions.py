from __future__ import annotations

import argparse
import csv
import math
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

import numpy as np

from run_bm25 import (
    RESULTS_DIR,
    save_csv,
    save_json,
)


LABELS = (
    "SUPPORT",
    "CONTRADICT",
    "NOT_ENOUGH_INFO",
)

CONDITIONS = (
    "oracle_cited",
    "retrieved_bm25",
    "retrieved_dense",
    "retrieved_hybrid",
    "no_evidence",
    "distractor_dense",
)

LABEL_CODES = {
    name: index
    for index, name in enumerate(
        (*LABELS, "INVALID")
    )
}

# 终端重点显示这些比较。
# 文件中仍保存全部15组比较。
KEY_PAIRS = (
    ("retrieved_hybrid", "retrieved_dense"),
    ("retrieved_dense", "retrieved_bm25"),
    ("retrieved_hybrid", "retrieved_bm25"),
    ("oracle_cited", "retrieved_dense"),
    ("retrieved_dense", "no_evidence"),
    ("retrieved_dense", "distractor_dense"),
    ("distractor_dense", "no_evidence"),
)


# ============================================================
# 1. 参数
# ============================================================

def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "对六种大模型证据条件进行"
            "配对统计比较，不调用API。"
        )
    )

    parser.add_argument(
        "--per-claim",
        type=Path,
        required=True,
    )

    parser.add_argument(
        "--bootstrap-samples",
        type=int,
        default=10000,
    )

    parser.add_argument(
        "--confidence-level",
        type=float,
        default=0.95,
    )

    parser.add_argument(
        "--seed",
        type=int,
        default=20261013,
    )

    args = parser.parse_args()

    if args.bootstrap_samples <= 0:
        parser.error(
            "--bootstrap-samples必须大于0。"
        )

    if not 0 < args.confidence_level < 1:
        parser.error(
            "--confidence-level必须在0和1之间。"
        )

    return args


# ============================================================
# 2. 读取并对齐逐条预测
# ============================================================

def load_paired_predictions(path):
    method_rows = {
        condition: {}
        for condition in CONDITIONS
    }

    with path.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as file:
        reader = csv.DictReader(file)

        required = {
            "claim_id",
            "condition",
            "gold_label",
            "predicted_label",
        }

        if not required.issubset(
            set(reader.fieldnames or [])
        ):
            raise ValueError(
                "逐条结果CSV缺少必需字段。"
            )

        for row in reader:
            condition = row["condition"]
            cid = row["claim_id"]

            if condition not in method_rows:
                raise ValueError(
                    f"未知条件：{condition}"
                )

            if cid in method_rows[condition]:
                raise ValueError(
                    f"重复claim：{condition}/{cid}"
                )

            if row["gold_label"] not in LABELS:
                raise ValueError(
                    "未知金标准标签。"
                )

            if (
                row["predicted_label"]
                not in LABEL_CODES
            ):
                raise ValueError(
                    "未知预测标签。"
                )

            method_rows[condition][cid] = row

    reference = method_rows[
        CONDITIONS[0]
    ]

    if not reference:
        raise ValueError(
            "没有可评测claim。"
        )

    for condition in CONDITIONS:
        if (
            set(method_rows[condition])
            != set(reference)
        ):
            raise ValueError(
                f"{condition}的claim集合"
                "与其他条件不一致。"
            )

    claim_ids = sorted(
        reference,
        key=int,
    )

    for cid in claim_ids:
        if any(
            method_rows[condition][cid]["gold_label"]
            != reference[cid]["gold_label"]
            for condition in CONDITIONS
        ):
            raise ValueError(
                f"claim {cid}的标准答案不一致。"
            )

    true_codes = np.array(
        [
            LABEL_CODES[
                reference[cid]["gold_label"]
            ]
            for cid in claim_ids
        ],
        dtype=np.int8,
    )

    predictions = {
        condition: np.array(
            [
                LABEL_CODES[
                    method_rows[condition][cid][
                        "predicted_label"
                    ]
                ]
                for cid in claim_ids
            ],
            dtype=np.int8,
        )
        for condition in CONDITIONS
    }

    return (
        claim_ids,
        true_codes,
        predictions,
    )


# ============================================================
# 3. 计算一批样本的指标
# ============================================================

def batch_metrics(
    true_samples,
    predicted_samples,
):
    """
    每行是一份重采样数据。

    Macro-F1每次重新计算，
    固定对三个金标准类别取平均。

    INVALID仍然是错误预测，
    不从样本中删除。
    """
    accuracy = np.mean(
        true_samples == predicted_samples,
        axis=1,
    )

    macro_f1 = np.zeros(
        true_samples.shape[0],
        dtype=np.float64,
    )

    for code in range(len(LABELS)):
        true_mask = (
            true_samples == code
        )

        predicted_mask = (
            predicted_samples == code
        )

        tp = np.sum(
            true_mask & predicted_mask,
            axis=1,
        )

        denominator = (
            np.sum(true_mask, axis=1)
            + np.sum(predicted_mask, axis=1)
        )

        class_f1 = np.divide(
            2.0 * tp,
            denominator,
            out=np.zeros_like(macro_f1),
            where=denominator > 0,
        )

        macro_f1 += class_f1

    macro_f1 /= len(LABELS)

    return {
        "accuracy": accuracy,
        "macro_f1": macro_f1,
    }


# ============================================================
# 4. 所有条件共用相同的重采样下标
# ============================================================

def bootstrap_all_conditions(
    true_codes,
    predictions,
    samples,
    seed,
):
    rng = np.random.default_rng(seed)

    distributions = {
        condition: {
            "accuracy": np.empty(
                samples,
                dtype=np.float64,
            ),
            "macro_f1": np.empty(
                samples,
                dtype=np.float64,
            ),
        }
        for condition in CONDITIONS
    }

    # 分小批次计算，减少内存占用。
    for start in range(
        0,
        samples,
        500,
    ):
        stop = min(
            start + 500,
            samples,
        )

        indices = rng.integers(
            0,
            len(true_codes),
            size=(
                stop - start,
                len(true_codes),
            ),
        )

        sampled_truth = true_codes[
            indices
        ]

        for condition in CONDITIONS:
            metrics = batch_metrics(
                sampled_truth,
                predictions[condition][indices],
            )

            for name in (
                "accuracy",
                "macro_f1",
            ):
                distributions[
                    condition
                ][name][
                    start:stop
                ] = metrics[name]

    return distributions


# ============================================================
# 5. 精确McNemar与Holm校正
# ============================================================

def exact_mcnemar(
    correct_a,
    correct_b,
):
    both = int(
        np.sum(correct_a & correct_b)
    )

    a_only = int(
        np.sum(correct_a & ~correct_b)
    )

    b_only = int(
        np.sum(~correct_a & correct_b)
    )

    neither = int(
        np.sum(~correct_a & ~correct_b)
    )

    discordant = a_only + b_only

    if discordant == 0:
        p_value = 1.0
    else:
        tail = sum(
            math.comb(discordant, i)
            for i in range(
                min(a_only, b_only) + 1
            )
        ) / (2 ** discordant)

        p_value = min(
            1.0,
            2.0 * tail,
        )

    return {
        "both_correct": both,
        "method_a_only_correct": a_only,
        "method_b_only_correct": b_only,
        "both_wrong": neither,
        "p_value_raw": p_value,
    }


def holm_adjust(p_values):
    count = len(p_values)
    adjusted = [0.0] * count
    running_max = 0.0

    order = sorted(
        range(count),
        key=lambda i: p_values[i],
    )

    for rank, index in enumerate(order):
        running_max = max(
            running_max,
            min(
                1.0,
                (count - rank) * p_values[index],
            ),
        )

        adjusted[index] = running_max

    return adjusted


def comparison_pairs():
    pairs = list(KEY_PAIRS)

    seen = {
        frozenset(pair)
        for pair in pairs
    }

    for pair in combinations(
        CONDITIONS,
        2,
    ):
        if frozenset(pair) not in seen:
            pairs.append(pair)
            seen.add(frozenset(pair))

    return pairs


# ============================================================
# 6. 配对比较
# ============================================================

def compare_conditions(
    true_codes,
    predictions,
    samples,
    confidence,
    seed,
):
    observed = {
        condition: {
            name: float(values[0])
            for name, values in batch_metrics(
                true_codes[None, :],
                predictions[condition][None, :],
            ).items()
        }
        for condition in CONDITIONS
    }

    distributions = (
        bootstrap_all_conditions(
            true_codes,
            predictions,
            samples,
            seed,
        )
    )

    alpha = 1.0 - confidence

    bootstrap_rows = []
    mcnemar_rows = []

    for method_a, method_b in (
        comparison_pairs()
    ):
        for metric in (
            "accuracy",
            "macro_f1",
        ):
            differences = (
                distributions[method_a][metric]
                - distributions[method_b][metric]
            )

            lower, upper = np.quantile(
                differences,
                [
                    alpha / 2,
                    1 - alpha / 2,
                ],
            )

            bootstrap_rows.append(
                {
                    "method_a": method_a,
                    "method_b": method_b,
                    "metric": metric,
                    "mean_a": (
                        observed[method_a][metric]
                    ),
                    "mean_b": (
                        observed[method_b][metric]
                    ),
                    "difference_a_minus_b": (
                        observed[method_a][metric]
                        - observed[method_b][metric]
                    ),
                    "confidence_level": confidence,
                    "ci_lower": float(lower),
                    "ci_upper": float(upper),
                    "ci_excludes_zero_unadjusted": bool(
                        lower > 0 or upper < 0
                    ),
                }
            )

        mcnemar_rows.append(
            {
                "method_a": method_a,
                "method_b": method_b,
                "accuracy_a": (
                    observed[method_a]["accuracy"]
                ),
                "accuracy_b": (
                    observed[method_b]["accuracy"]
                ),
                **exact_mcnemar(
                    predictions[method_a]
                    == true_codes,
                    predictions[method_b]
                    == true_codes,
                ),
            }
        )

    adjusted = holm_adjust(
        [
            row["p_value_raw"]
            for row in mcnemar_rows
        ]
    )

    for row, value in zip(
        mcnemar_rows,
        adjusted,
    ):
        row["p_value_holm"] = value
        row["significant_holm_0_05"] = (
            value < 0.05
        )

    return (
        observed,
        bootstrap_rows,
        mcnemar_rows,
    )


# ============================================================
# 7. 主程序
# ============================================================

def main():
    args = parse_arguments()

    (
        claim_ids,
        true_codes,
        predictions,
    ) = load_paired_predictions(
        args.per_claim
    )

    print(
        f"配对claim数：{len(claim_ids)}"
    )

    print(
        "Bootstrap重复次数："
        f"{args.bootstrap_samples}"
    )

    print(
        "正在比较Accuracy和Macro-F1……"
    )

    (
        observed,
        bootstrap_rows,
        mcnemar_rows,
    ) = compare_conditions(
        true_codes,
        predictions,
        args.bootstrap_samples,
        args.confidence_level,
        args.seed,
    )

    output_dir = RESULTS_DIR / "llm"

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    prefix = args.per_claim.stem.removesuffix(
        "_per_claim"
    )

    bootstrap_path = (
        output_dir
        / f"{prefix}_bootstrap.csv"
    )

    mcnemar_path = (
        output_dir
        / f"{prefix}_mcnemar.csv"
    )

    statistics_path = (
        output_dir
        / f"{prefix}_statistics.json"
    )

    save_csv(
        bootstrap_rows,
        bootstrap_path,
    )

    save_csv(
        mcnemar_rows,
        mcnemar_path,
    )

    save_json(
        {
            "generated_at_utc": (
                datetime.now(timezone.utc)
                .isoformat()
            ),
            "source": str(
                args.per_claim.resolve()
            ),
            "claim_count": len(claim_ids),
            "configuration": {
                "bootstrap_samples": (
                    args.bootstrap_samples
                ),
                "confidence_level": (
                    args.confidence_level
                ),
                "seed": args.seed,
                "sampling_unit": "claim",
                "macro_f1_labels": list(LABELS),
                "bootstrap_interval": (
                    "paired percentile, pointwise, unadjusted"
                ),
                "mcnemar": "two-sided exact",
                "p_value_correction": (
                    "Holm over all 15 pairwise accuracy tests"
                ),
                "invalid_prediction_policy": (
                    "retain and count as incorrect"
                ),
            },
            "observed_metrics": observed,
            "bootstrap": bootstrap_rows,
            "mcnemar": mcnemar_rows,
        },
        statistics_path,
    )

    print()
    print(
        "关键配对比较："
        "差值均为method_a减method_b"
    )

    for method_a, method_b in KEY_PAIRS:
        print(
            f"\n{method_a} - {method_b}"
        )

        for row in bootstrap_rows:
            if (
                row["method_a"],
                row["method_b"],
            ) == (
                method_a,
                method_b,
            ):
                print(
                    f"  {row['metric']}差值："
                    f"{row['difference_a_minus_b']:+.4f}；"
                    "置信区间："
                    f"[{row['ci_lower']:+.4f}, "
                    f"{row['ci_upper']:+.4f}]"
                )

        test = next(
            row
            for row in mcnemar_rows
            if (
                row["method_a"],
                row["method_b"],
            ) == (
                method_a,
                method_b,
            )
        )

        print(
            "  仅A正确："
            f"{test['method_a_only_correct']}；"
            "仅B正确："
            f"{test['method_b_only_correct']}"
        )

        print(
            "  McNemar原始p="
            f"{test['p_value_raw']:.6g}；"
            "Holm校正p="
            f"{test['p_value_holm']:.6g}；"
            "校正后p<0.05："
            f"{test['significant_holm_0_05']}"
        )

    print()
    print(
        "Bootstrap区间是逐项区间，"
        "未进行多重比较校正。"
    )

    print(
        "McNemar检验比较正确率，"
        "不是Macro-F1检验。"
    )

    print(
        "所有方法使用相同claim重采样；"
        "这些区间依赖claim抽样假设。"
    )

    print("结果文件：")

    for path in (
        bootstrap_path,
        mcnemar_path,
        statistics_path,
    ):
        print(f"  {path}")


if __name__ == "__main__":
    main()