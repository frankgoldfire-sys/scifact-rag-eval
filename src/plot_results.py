from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # 直接保存图片，不弹出窗口。

import matplotlib.pyplot as plt
import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = PROJECT_ROOT / "results"

DEFAULT_PREFIX = (
    "dev_all_k5_seed20261013_f2227ecf8839_responses"
)

METHODS = ["bm25", "dense", "hybrid"]
METHOD_NAMES = ["BM25", "Dense", "Hybrid"]
METHOD_COLORS = ["#757575", "#4477AA", "#228833"]

CONDITIONS = [
    "oracle_cited",
    "retrieved_bm25",
    "retrieved_dense",
    "retrieved_hybrid",
    "no_evidence",
    "distractor_dense",
]

CONDITION_NAMES = [
    "Oracle (cited)",
    "Retrieved BM25",
    "Retrieved Dense",
    "Retrieved Hybrid",
    "No evidence",
    "Distractor Dense",
]

# 展示之前讨论的七组比较。
# 完整的十五组比较仍保留在原统计文件中。
PAIRS = [
    ("retrieved_hybrid", "retrieved_dense", "Hybrid - Dense"),
    ("retrieved_dense", "retrieved_bm25", "Dense - BM25"),
    ("retrieved_hybrid", "retrieved_bm25", "Hybrid - BM25"),
    ("oracle_cited", "retrieved_dense", "Oracle - Dense"),
    ("retrieved_dense", "no_evidence", "Dense - No evidence"),
    ("retrieved_dense", "distractor_dense", "Dense - Distractor"),
    ("distractor_dense", "no_evidence", "Distractor - No evidence"),
]


def read_csv(
    path: Path,
    required: set[str],
) -> list[dict[str, str]]:
    """读取CSV，并检查必要字段。"""
    with path.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        reader = csv.DictReader(handle)

        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"{path.name}缺少字段：{sorted(missing)}"
            )

        rows = list(reader)

    if not rows:
        raise ValueError(f"{path.name}没有数据。")

    return rows


def index_rows(rows, key: str, expected: list[str]):
    """按方法或条件建立索引，并检查重复和缺失。"""
    indexed = {}

    for row in rows:
        value = row[key]
        if value in indexed:
            raise ValueError(f"{key}重复：{value}")
        indexed[value] = row

    if set(indexed) != set(expected):
        raise ValueError(
            f"{key}与预期不一致：{sorted(indexed)}"
        )

    return indexed


def score(row, key: str) -> float:
    """检查指标是有效的0～1数值。"""
    value = float(row[key])

    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(
            f"{key}不是有效的0～1数值：{value}"
        )

    return value


def style_axes(ax):
    """统一坐标轴样式。"""
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.set_axisbelow(True)
    ax.grid(
        axis="x",
        color="#DDDDDD",
        linewidth=0.7,
    )
    ax.tick_params(axis="both", labelsize=10)


def save_figure(fig, directory: Path, name: str):
    """每张图保存PNG和PDF两种格式。"""
    for extension in ("png", "pdf"):
        path = directory / f"{name}.{extension}"

        fig.savefig(
            path,
            dpi=300,
            bbox_inches="tight",
        )
        print(f"已保存：{path}")

    plt.close(fig)


def plot_retrieval(rows, directory: Path):
    """图1：三种方法的证据文档检索效果。"""
    indexed = index_rows(rows, "method", METHODS)

    counts = {
        int(row["evaluated_claims"])
        for row in rows
    }
    if len(counts) != 1:
        raise ValueError(
            "三种检索方法的评测claim数不一致。"
        )
    n = counts.pop()

    metrics = [
        ("recall_at_5", "Recall@5"),
        ("reciprocal_rank", "MRR"),
        ("ndcg_at_5", "NDCG@5"),
    ]

    fig, axes = plt.subplots(
        1,
        3,
        figsize=(12, 4.2),
    )
    positions = np.arange(len(METHODS))

    for ax, (key, title) in zip(axes, metrics):
        values = [
            100 * score(indexed[method], key)
            for method in METHODS
        ]

        ax.barh(
            positions,
            values,
            height=0.55,
            color=METHOD_COLORS,
        )
        ax.set_yticks(positions, METHOD_NAMES)
        ax.invert_yaxis()
        ax.set_xlim(0, 100)
        ax.set_xlabel("Score (%)")
        ax.set_ylabel("Retriever")
        ax.set_title(title)
        style_axes(ax)

        for y, value in zip(positions, values):
            ax.text(
                value + 1,
                y,
                f"{value:.1f}%",
                va="center",
                fontsize=10,
            )

    fig.suptitle(
        "SciFact dev: evidence-document retrieval",
        fontsize=14,
    )
    fig.text(
        0.5,
        0.02,
        "Source: retrieval_dev_summary.csv | "
        f"n={n} annotated-evidence claims; NEI excluded.",
        ha="center",
        fontsize=9,
    )
    fig.tight_layout(rect=(0, 0.09, 1, 0.92))

    save_figure(
        fig,
        directory,
        "retrieval_dev_comparison",
    )


def plot_llm(rows, directory: Path, prefix: str):
    """图2：六种证据条件的大模型判断成绩。"""
    indexed = index_rows(
        rows,
        "condition",
        CONDITIONS,
    )

    counts = {
        int(row["claim_count"])
        for row in rows
    }
    if len(counts) != 1:
        raise ValueError(
            "六种证据条件的claim数不一致。"
        )
    n = counts.pop()

    invalid = sum(
        int(row["invalid_label_count"])
        for row in rows
    )

    fig, axes = plt.subplots(
        1,
        2,
        figsize=(12, 5.5),
        sharey=True,
    )
    positions = np.arange(len(CONDITIONS))

    metrics = [
        ("accuracy", "Accuracy"),
        ("macro_f1", "Macro-F1"),
    ]

    for ax, (key, title) in zip(axes, metrics):
        values = [
            100 * score(indexed[condition], key)
            for condition in CONDITIONS
        ]

        ax.barh(
            positions,
            values,
            height=0.6,
            color="#4477AA",
        )
        ax.set_yticks(
            positions,
            CONDITION_NAMES,
        )
        ax.set_xlim(0, 100)
        ax.set_xlabel("Score (%)")
        ax.set_title(title)
        style_axes(ax)

        for y, value in zip(positions, values):
            ax.text(
                value + 1,
                y,
                f"{value:.1f}%",
                va="center",
                fontsize=10,
            )

    axes[0].invert_yaxis()
    axes[0].set_ylabel("Evidence condition")

    fig.suptitle(
        "SciFact dev: DeepSeek-Flash verification",
        fontsize=14,
    )
    fig.text(
        0.5,
        0.045,
        f"n={n} claims per condition, including NEI | "
        f"Invalid labels retained: {invalid}",
        ha="center",
        fontsize=9,
    )
    fig.text(
        0.5,
        0.015,
        f"Source: {prefix}_summary.csv",
        ha="center",
        fontsize=8,
    )
    fig.tight_layout(rect=(0, 0.10, 1, 0.92))

    save_figure(
        fig,
        directory,
        "llm_dev_conditions",
    )


def plot_differences(rows, statistics, directory: Path):
    """图3：配对差值及已有Bootstrap置信区间。"""
    indexed = {}

    for row in rows:
        key = (
            row["method_a"],
            row["method_b"],
            row["metric"],
        )

        if key in indexed:
            raise ValueError(
                f"Bootstrap记录重复：{key}"
            )

        indexed[key] = row

    config = statistics["configuration"]
    level = float(config["confidence_level"])

    limits = [0.0]
    panels = []

    for metric in ("accuracy", "macro_f1"):
        points = []
        lower = []
        upper = []

        for method_a, method_b, _ in PAIRS:
            key = (method_a, method_b, metric)

            if key not in indexed:
                raise ValueError(
                    f"缺少Bootstrap比较：{key}"
                )

            row = indexed[key]

            if not math.isclose(
                float(row["confidence_level"]),
                level,
            ):
                raise ValueError(
                    "置信水平不一致。"
                )

            values = [
                100 * float(row["difference_a_minus_b"]),
                100 * float(row["ci_lower"]),
                100 * float(row["ci_upper"]),
            ]

            if not all(
                math.isfinite(value)
                for value in values
            ):
                raise ValueError(
                    f"Bootstrap含无效数值：{key}"
                )

            point, lo, hi = values

            if lo > hi:
                raise ValueError(
                    f"置信区间上下界颠倒：{key}"
                )

            points.append(point)
            lower.append(lo)
            upper.append(hi)
            limits.extend(values)

        panels.append((points, lower, upper))

    # 两个面板使用同一横轴范围，便于比较。
    span = max(limits) - min(limits)
    padding = max(2.0, span * 0.08)

    x_limits = (
        min(limits) - padding,
        max(limits) + padding,
    )

    positions = np.arange(len(PAIRS))
    names = [
        name
        for _, _, name in PAIRS
    ]

    fig, axes = plt.subplots(
        1,
        2,
        figsize=(13, 6),
        sharey=True,
    )

    titles = [
        "Accuracy difference",
        "Macro-F1 difference",
    ]

    for ax, title, panel in zip(
        axes,
        titles,
        panels,
    ):
        points, lower, upper = panel

        ax.hlines(
            positions,
            lower,
            upper,
            color="#4477AA",
            linewidth=2,
        )
        ax.plot(
            lower,
            positions,
            "|",
            color="#4477AA",
            markersize=9,
        )
        ax.plot(
            upper,
            positions,
            "|",
            color="#4477AA",
            markersize=9,
        )
        ax.scatter(
            points,
            positions,
            color="#4477AA",
            s=30,
            zorder=3,
        )
        ax.axvline(
            0,
            color="#555555",
            linestyle="--",
            linewidth=1,
        )

        ax.set_yticks(positions, names)
        ax.set_xlim(*x_limits)
        ax.set_xlabel(
            "A - B (percentage points)"
        )
        ax.set_title(title)
        style_axes(ax)

    axes[0].invert_yaxis()
    axes[0].set_ylabel("Comparison")

    fig.suptitle(
        "SciFact dev: paired differences",
        fontsize=14,
    )
    fig.text(
        0.5,
        0.045,
        f"Pointwise {100 * level:.0f}% paired Bootstrap CIs, "
        "unadjusted | "
        f"B={config['bootstrap_samples']}, "
        f"seed={config['seed']}, "
        f"n={statistics['claim_count']}",
        ha="center",
        fontsize=9,
    )
    fig.text(
        0.5,
        0.015,
        "Positive: A higher than B. "
        "CIs are not Holm-adjusted McNemar tests.",
        ha="center",
        fontsize=9,
    )
    fig.tight_layout(rect=(0, 0.10, 1, 0.92))

    save_figure(
        fig,
        directory,
        "llm_dev_paired_differences",
    )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "读取已有dev结果并生成三张图，不调用API。"
        )
    )
    parser.add_argument(
        "--llm-prefix",
        default=DEFAULT_PREFIX,
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="允许覆盖本脚本生成的同名图片",
    )
    args = parser.parse_args()

    if Path(args.llm_prefix).name != args.llm_prefix:
        raise ValueError(
            "--llm-prefix必须是文件前缀，不能包含目录。"
        )

    llm_dir = RESULTS_DIR / "llm"
    prefix = args.llm_prefix
    output_dir = PROJECT_ROOT / "figures" / prefix

    retrieval = read_csv(
        RESULTS_DIR / "retrieval_dev_summary.csv",
        {
            "method",
            "evaluated_claims",
            "recall_at_5",
            "reciprocal_rank",
            "ndcg_at_5",
        },
    )

    llm = read_csv(
        llm_dir / f"{prefix}_summary.csv",
        {
            "condition",
            "claim_count",
            "accuracy",
            "macro_f1",
            "invalid_label_count",
        },
    )

    bootstrap = read_csv(
        llm_dir / f"{prefix}_bootstrap.csv",
        {
            "method_a",
            "method_b",
            "metric",
            "difference_a_minus_b",
            "confidence_level",
            "ci_lower",
            "ci_upper",
        },
    )

    with (
        llm_dir / f"{prefix}_statistics.json"
    ).open("r", encoding="utf-8") as handle:
        statistics = json.load(handle)

    # 检查汇总结果与统计结果是否来自同一批评测。
    indexed_llm = index_rows(
        llm,
        "condition",
        CONDITIONS,
    )

    for condition, row in indexed_llm.items():
        if int(row["claim_count"]) != int(
            statistics["claim_count"]
        ):
            raise ValueError(
                "汇总文件与统计文件的claim数不一致。"
            )

        for metric in ("accuracy", "macro_f1"):
            if not math.isclose(
                score(row, metric),
                float(
                    statistics["observed_metrics"]
                    [condition][metric]
                ),
                abs_tol=1e-10,
            ):
                raise ValueError(
                    "汇总文件与统计文件指标不一致："
                    f"{condition}/{metric}"
                )

    names = [
        "retrieval_dev_comparison",
        "llm_dev_conditions",
        "llm_dev_paired_differences",
    ]

    existing = [
        output_dir / f"{name}.{extension}"
        for name in names
        for extension in ("png", "pdf")
        if (
            output_dir / f"{name}.{extension}"
        ).exists()
    ]

    if existing and not args.overwrite:
        raise FileExistsError(
            "图片已存在。需要重新生成时加--overwrite。"
        )

    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 11,
            "pdf.fonttype": 42,
            "axes.unicode_minus": False,
        }
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    plot_retrieval(retrieval, output_dir)
    plot_llm(llm, output_dir, prefix)
    plot_differences(
        bootstrap,
        statistics,
        output_dir,
    )

    print(
        "\n绘图完成：3张图，每张包含PNG和PDF，共6个文件。"
    )
    print(
        "原始结果未修改，没有调用大模型API。"
    )


if __name__ == "__main__":
    main()