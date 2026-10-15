from __future__ import annotations

import argparse
import csv
import json
import math
import platform
import re
from collections import defaultdict
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from typing import Any

import numpy as np
from rank_bm25 import BM25Okapi


# ============================================================
# 1. 项目路径和默认设置
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
RESULTS_DIR = PROJECT_ROOT / "results"

CORPUS_PATH = RAW_DATA_DIR / "corpus.jsonl"

CLAIM_PATHS = {
    "train": RAW_DATA_DIR / "claims_train.jsonl",
    "dev": RAW_DATA_DIR / "claims_dev.jsonl",
    "test": RAW_DATA_DIR / "claims_test.jsonl",
}

DEFAULT_TOP_KS = (
    1,
    3,
    5,
    10,
    20,
)

DEFAULT_RANKING_DEPTH = 100
DEFAULT_K1 = 1.5
DEFAULT_B = 0.75

SUPPORT = "SUPPORT"
CONTRADICT = "CONTRADICT"
NOT_ENOUGH_INFO = "NOT_ENOUGH_INFO"
UNLABELED = "UNLABELED"

EVIDENCE_LABELS = {
    SUPPORT,
    CONTRADICT,
}

# 支持普通英文、数字以及带连字符的术语，
# 例如IL-6、COVID-19、p53。
TOKEN_PATTERN = re.compile(
    r"\b[\w]+(?:[-'][\w]+)*\b",
    flags=re.UNICODE,
)


# ============================================================
# 2. 命令行参数
# ============================================================

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "在SciFact上运行文档级BM25检索。"
        )
    )

    parser.add_argument(
        "--split",
        choices=[
            "train",
            "dev",
            "test",
        ],
        default="train",
        help=(
            "需要检索的数据划分。"
            "当前开发阶段请使用train。"
        ),
    )

    parser.add_argument(
        "--ranking-depth",
        type=int,
        default=DEFAULT_RANKING_DEPTH,
        help=(
            "为每条claim保存多少条检索结果。"
        ),
    )

    parser.add_argument(
        "--k1",
        type=float,
        default=DEFAULT_K1,
        help="BM25词频饱和参数。",
    )

    parser.add_argument(
        "--b",
        type=float,
        default=DEFAULT_B,
        help="BM25文档长度归一化参数。",
    )

    arguments = parser.parse_args()

    if arguments.ranking_depth <= 0:
        parser.error(
            "--ranking-depth必须是正整数。"
        )

    if arguments.k1 < 0:
        parser.error("--k1不能小于0。")

    if not 0 <= arguments.b <= 1:
        parser.error("--b必须位于0到1之间。")

    return arguments


# ============================================================
# 3. 读取JSONL文件
# ============================================================

def load_jsonl(
    path: Path,
) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(
            f"没有找到文件：{path}"
        )

    records: list[dict[str, Any]] = []

    with path.open(
        mode="r",
        encoding="utf-8",
    ) as file:
        for line_number, line in enumerate(
            file,
            start=1,
        ):
            stripped_line = line.strip()

            if not stripped_line:
                continue

            try:
                record = json.loads(
                    stripped_line
                )
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"{path}第{line_number}行"
                    "不是合法JSON。"
                ) from error

            if not isinstance(record, dict):
                raise ValueError(
                    f"{path}第{line_number}行"
                    "不是JSON对象。"
                )

            records.append(record)

    return records


# ============================================================
# 4. 文本分词
# ============================================================

def tokenize(
    text: str,
) -> list[str]:
    """
    第一版采用简单、透明且可复现的分词方法：

    1. 转为小写；
    2. 提取单词、数字和带连字符的术语；
    3. 不进行词干还原；
    4. 不删除停用词。
    """
    return TOKEN_PATTERN.findall(
        text.lower()
    )


def build_document_text(
    document: dict[str, Any],
) -> str:
    title = str(
        document.get("title", "")
    ).strip()

    abstract = document.get(
        "abstract",
        [],
    )

    if not isinstance(abstract, list):
        raise ValueError(
            f"文档{document.get('doc_id')}的"
            "abstract不是列表。"
        )

    abstract_text = " ".join(
        str(sentence).strip()
        for sentence in abstract
    )

    return f"{title} {abstract_text}".strip()


# ============================================================
# 5. 推导标签与金标准文档
# ============================================================

def get_label_and_gold_documents(
    claim: dict[str, Any],
    split: str,
) -> tuple[str, set[str]]:
    """
    返回：
    - claim的三分类标签；
    - 金标准证据文档ID集合。

    test没有公开标签，因此返回UNLABELED和空集合。
    """
    if split == "test":
        return UNLABELED, set()

    evidence = claim.get("evidence")

    if not isinstance(evidence, dict):
        raise ValueError(
            f"claim {claim.get('id')}的"
            "evidence不是JSON对象。"
        )

    if not evidence:
        return NOT_ENOUGH_INFO, set()

    labels: set[str] = set()

    for rationales in evidence.values():
        if not isinstance(rationales, list):
            raise ValueError(
                f"claim {claim.get('id')}的"
                "rationale集合不是列表。"
            )

        for rationale in rationales:
            label = str(
                rationale.get("label", "")
            ).strip().upper()

            if label not in EVIDENCE_LABELS:
                raise ValueError(
                    f"claim {claim.get('id')}出现"
                    f"未知标签：{label!r}"
                )

            labels.add(label)

    if len(labels) != 1:
        raise ValueError(
            f"claim {claim.get('id')}包含"
            f"多个标签：{sorted(labels)}"
        )

    gold_document_ids = {
        str(doc_id)
        for doc_id in evidence.keys()
    }

    return (
        next(iter(labels)),
        gold_document_ids,
    )


# ============================================================
# 6. 单条claim的检索指标
# ============================================================

def calculate_query_metrics(
    gold_document_ids: set[str],
    ranked_document_ids: list[str],
    top_ks: tuple[int, ...],
) -> dict[str, float | int]:
    """
    只对至少包含一个金标准文档的claim调用。

    Recall@K：
        Top-K中找回的金标准文档数 / 金标准文档总数

    Hit@K：
        Top-K中是否至少出现一个金标准文档

    MRR@K：
        第一篇金标准文档在Top-K内时，取其排名倒数

    NDCG@K：
        将每篇金标准文档视为二值相关文档。
    """
    if not gold_document_ids:
        raise ValueError(
            "没有金标准文档，无法计算检索指标。"
        )

    gold_ranks = [
        rank
        for rank, doc_id in enumerate(
            ranked_document_ids,
            start=1,
        )
        if doc_id in gold_document_ids
    ]

    if len(gold_ranks) != len(
        gold_document_ids
    ):
        missing_gold_documents = (
            gold_document_ids
            - set(ranked_document_ids)
        )

        raise ValueError(
            "完整排名中没有找到金标准文档："
            f"{sorted(missing_gold_documents)}"
        )

    first_relevant_rank = min(gold_ranks)

    metrics: dict[str, float | int] = {
        "gold_document_count": len(
            gold_document_ids
        ),
        "first_relevant_rank": (
            first_relevant_rank
        ),
        "reciprocal_rank": (
            1.0 / first_relevant_rank
        ),
    }

    for k in top_ks:
        retrieved_relevant_count = sum(
            rank <= k
            for rank in gold_ranks
        )

        recall = (
            retrieved_relevant_count
            / len(gold_document_ids)
        )

        hit = int(
            retrieved_relevant_count > 0
        )

        reciprocal_rank_at_k = (
            1.0 / first_relevant_rank
            if first_relevant_rank <= k
            else 0.0
        )

        dcg = sum(
            1.0 / math.log2(rank + 1)
            for rank in gold_ranks
            if rank <= k
        )

        ideal_relevant_count = min(
            len(gold_document_ids),
            k,
        )

        idcg = sum(
            1.0 / math.log2(rank + 1)
            for rank in range(
                1,
                ideal_relevant_count + 1,
            )
        )

        ndcg = (
            dcg / idcg
            if idcg > 0
            else 0.0
        )

        metrics[f"recall_at_{k}"] = recall
        metrics[f"hit_at_{k}"] = hit
        metrics[
            f"reciprocal_rank_at_{k}"
        ] = reciprocal_rank_at_k
        metrics[f"ndcg_at_{k}"] = ndcg

    return metrics


# ============================================================
# 7. 安全保存结果
# ============================================================

def save_json(
    data: dict[str, Any],
    path: Path,
) -> None:
    temporary_path = path.with_suffix(
        path.suffix + ".tmp"
    )

    with temporary_path.open(
        mode="w",
        encoding="utf-8",
    ) as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2,
        )

    temporary_path.replace(path)


def save_jsonl(
    records: list[dict[str, Any]],
    path: Path,
) -> None:
    temporary_path = path.with_suffix(
        path.suffix + ".tmp"
    )

    with temporary_path.open(
        mode="w",
        encoding="utf-8",
    ) as file:
        for record in records:
            file.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                )
            )
            file.write("\n")

    temporary_path.replace(path)


def save_csv(
    records: list[dict[str, Any]],
    path: Path,
) -> None:
    if not records:
        raise ValueError(
            "没有记录可以保存为CSV。"
        )

    temporary_path = path.with_suffix(
        path.suffix + ".tmp"
    )

    fieldnames = list(records[0].keys())

    with temporary_path.open(
        mode="w",
        encoding="utf-8-sig",
        newline="",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=fieldnames,
        )

        writer.writeheader()
        writer.writerows(records)

    temporary_path.replace(path)


# ============================================================
# 8. 汇总平均指标
# ============================================================

def aggregate_metrics(
    per_claim_results: list[dict[str, Any]],
    top_ks: tuple[int, ...],
) -> dict[str, float]:
    """
    只汇总has_gold_evidence=True的claim。
    """
    evaluable_results = [
        row
        for row in per_claim_results
        if row["has_gold_evidence"]
    ]

    if not evaluable_results:
        return {}

    metric_names = [
        "reciprocal_rank",
    ]

    for k in top_ks:
        metric_names.extend(
            [
                f"recall_at_{k}",
                f"hit_at_{k}",
                f"reciprocal_rank_at_{k}",
                f"ndcg_at_{k}",
            ]
        )

    aggregate: dict[str, float] = {}

    for metric_name in metric_names:
        values = [
            float(row[metric_name])
            for row in evaluable_results
        ]

        aggregate[metric_name] = float(
            np.mean(values)
        )

    return aggregate


# ============================================================
# 9. 主程序
# ============================================================

def main() -> None:
    arguments = parse_arguments()

    split = arguments.split
    claim_path = CLAIM_PATHS[split]

    RESULTS_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    print("正在读取SciFact语料库……")

    corpus_records = load_jsonl(
        CORPUS_PATH
    )

    claims = load_jsonl(
        claim_path
    )

    if arguments.ranking_depth > len(
        corpus_records
    ):
        raise ValueError(
            "--ranking-depth不能超过"
            f"语料库文档数{len(corpus_records)}。"
        )

    print(
        f"语料库文档数：{len(corpus_records)}"
    )

    print(
        f"{split} claim数：{len(claims)}"
    )

    # 保持corpus.jsonl中的稳定顺序。
    document_ids: list[str] = []
    tokenized_corpus: list[list[str]] = []

    print("正在构建BM25文档集合……")

    for document in corpus_records:
        doc_id = str(document["doc_id"])
        document_text = build_document_text(
            document
        )

        document_ids.append(doc_id)
        tokenized_corpus.append(
            tokenize(document_text)
        )

    if len(document_ids) != len(
        set(document_ids)
    ):
        raise ValueError(
            "语料库中存在重复doc_id。"
        )

    print(
        "正在建立BM25索引……"
    )

    bm25 = BM25Okapi(
        tokenized_corpus,
        k1=arguments.k1,
        b=arguments.b,
    )

    rankings_output: list[
        dict[str, Any]
    ] = []

    per_claim_output: list[
        dict[str, Any]
    ] = []

    label_counts: dict[str, int] = (
        defaultdict(int)
    )

    print(
        f"正在检索{len(claims)}条claim……"
    )

    for claim_number, claim in enumerate(
        claims,
        start=1,
    ):
        claim_id = claim["id"]
        claim_text = str(claim["claim"])

        (
            gold_label,
            gold_document_ids,
        ) = get_label_and_gold_documents(
            claim=claim,
            split=split,
        )

        label_counts[gold_label] += 1

        query_tokens = tokenize(
            claim_text
        )

        scores = bm25.get_scores(
            query_tokens
        )

        ranked_indices = np.argsort(
            -scores,
            kind="stable",
        )

        complete_ranked_document_ids = [
            document_ids[int(index)]
            for index in ranked_indices
        ]

        top_indices = ranked_indices[
            :arguments.ranking_depth
        ]

        retrieved_documents = [
            {
                "rank": rank,
                "doc_id": (
                    document_ids[int(index)]
                ),
                "score": float(
                    scores[int(index)]
                ),
                "is_gold": (
                    document_ids[int(index)]
                    in gold_document_ids
                ),
            }
            for rank, index in enumerate(
                top_indices,
                start=1,
            )
        ]

        rankings_output.append(
            {
                "claim_id": claim_id,
                "claim": claim_text,
                "gold_label": gold_label,
                "gold_document_ids": sorted(
                    gold_document_ids
                ),
                "retrieval_method": "bm25",
                "retrieved_documents": (
                    retrieved_documents
                ),
            }
        )

        per_claim_row: dict[str, Any] = {
            "claim_id": claim_id,
            "gold_label": gold_label,
            "has_gold_evidence": bool(
                gold_document_ids
            ),
            "gold_document_count": (
                len(gold_document_ids)
            ),
        }

        if gold_document_ids:
            query_metrics = (
                calculate_query_metrics(
                    gold_document_ids=(
                        gold_document_ids
                    ),
                    ranked_document_ids=(
                        complete_ranked_document_ids
                    ),
                    top_ks=DEFAULT_TOP_KS,
                )
            )

            per_claim_row.update(
                query_metrics
            )
        else:
            per_claim_row[
                "first_relevant_rank"
            ] = None

            per_claim_row[
                "reciprocal_rank"
            ] = None

            for k in DEFAULT_TOP_KS:
                per_claim_row[
                    f"recall_at_{k}"
                ] = None

                per_claim_row[
                    f"hit_at_{k}"
                ] = None

                per_claim_row[
                    f"reciprocal_rank_at_{k}"
                ] = None

                per_claim_row[
                    f"ndcg_at_{k}"
                ] = None

        per_claim_output.append(
            per_claim_row
        )

        if (
            claim_number % 100 == 0
            or claim_number == len(claims)
        ):
            print(
                f"  已完成："
                f"{claim_number}/"
                f"{len(claims)}"
            )

    aggregate = aggregate_metrics(
        per_claim_results=per_claim_output,
        top_ks=DEFAULT_TOP_KS,
    )

    evaluated_claim_count = sum(
        row["has_gold_evidence"]
        for row in per_claim_output
    )

    metrics_output = {
        "retrieval_method": "bm25",
        "split": split,
        "generated_at_utc": (
            datetime.now(timezone.utc)
            .isoformat()
        ),
        "configuration": {
            "document_text": (
                "title + abstract"
            ),
            "tokenizer": (
                "lowercase_unicode_regex"
            ),
            "k1": arguments.k1,
            "b": arguments.b,
            "top_ks": list(
                DEFAULT_TOP_KS
            ),
            "saved_ranking_depth": (
                arguments.ranking_depth
            ),
        },
        "environment": {
            "python": (
                platform.python_version()
            ),
            "numpy": version("numpy"),
            "rank_bm25": version(
                "rank-bm25"
            ),
        },
        "counts": {
            "corpus_documents": len(
                corpus_records
            ),
            "all_claims": len(claims),
            "evaluated_claims": (
                evaluated_claim_count
            ),
            "excluded_from_retrieval_metrics": (
                len(claims)
                - evaluated_claim_count
            ),
            "label_distribution": dict(
                sorted(label_counts.items())
            ),
        },
        "metrics": aggregate,
    }

    ranking_path = (
        RESULTS_DIR
        / f"bm25_{split}_rankings.jsonl"
    )

    per_claim_path = (
        RESULTS_DIR
        / f"bm25_{split}_per_claim.csv"
    )

    metrics_path = (
        RESULTS_DIR
        / f"bm25_{split}_metrics.json"
    )

    save_jsonl(
        rankings_output,
        ranking_path,
    )

    save_csv(
        per_claim_output,
        per_claim_path,
    )

    save_json(
        metrics_output,
        metrics_path,
    )

    print()
    print("=" * 70)
    print("BM25检索完成")
    print("=" * 70)

    print(
        f"全部claim数：{len(claims)}"
    )

    print(
        "参与检索指标计算的claim数："
        f"{evaluated_claim_count}"
    )

    print(
        "不参与检索指标计算的claim数："
        f"{len(claims) - evaluated_claim_count}"
    )

    print()
    print("汇总指标：")

    for metric_name, metric_value in (
        aggregate.items()
    ):
        print(
            f"  {metric_name}: "
            f"{metric_value:.4f}"
        )

    print()
    print("结果文件：")
    print(f"  {ranking_path}")
    print(f"  {per_claim_path}")
    print(f"  {metrics_path}")


if __name__ == "__main__":
    main()