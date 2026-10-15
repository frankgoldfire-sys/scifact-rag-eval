from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any


# ============================================================
# 1. 文件路径
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[1]

RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
RESULTS_DIR = PROJECT_ROOT / "results"

CORPUS_PATH = RAW_DATA_DIR / "corpus.jsonl"
TRAIN_CLAIMS_PATH = (
    RAW_DATA_DIR / "claims_train.jsonl"
)

RANKINGS_PATH = (
    RESULTS_DIR / "bm25_train_rankings.jsonl"
)

PER_CLAIM_PATH = (
    RESULTS_DIR / "bm25_train_per_claim.csv"
)

SUMMARY_OUTPUT_PATH = (
    RESULTS_DIR
    / "bm25_train_case_summary.json"
)

ALL_CASES_OUTPUT_PATH = (
    RESULTS_DIR
    / "bm25_train_case_analysis.jsonl"
)

HARD_FAILURES_OUTPUT_PATH = (
    RESULTS_DIR
    / "bm25_train_hard_failures.jsonl"
)


# ============================================================
# 2. 读取文件
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

            records.append(record)

    return records


def load_csv(
    path: Path,
) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(
            f"没有找到文件：{path}"
        )

    with path.open(
        mode="r",
        encoding="utf-8-sig",
        newline="",
    ) as file:
        return list(
            csv.DictReader(file)
        )


# ============================================================
# 3. 保存文件
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


# ============================================================
# 4. 划分案例类型
# ============================================================

def categorize_rank(
    first_relevant_rank: int,
) -> str:
    if first_relevant_rank == 1:
        return "top_1_success"

    if first_relevant_rank <= 5:
        return "top_5_recovery"

    if first_relevant_rank <= 20:
        return "top_20_near_miss"

    return "hard_failure_after_20"


# ============================================================
# 5. 提取金标准证据
# ============================================================

def extract_gold_evidence(
    claim: dict[str, Any],
    corpus_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    evidence = claim.get(
        "evidence",
        {},
    )

    gold_documents: list[
        dict[str, Any]
    ] = []

    for raw_doc_id, rationales in (
        evidence.items()
    ):
        doc_id = str(raw_doc_id)

        if doc_id not in corpus_by_id:
            raise ValueError(
                f"金标准文档{doc_id}"
                "不存在于corpus中。"
            )

        document = corpus_by_id[doc_id]

        evidence_sets: list[
            dict[str, Any]
        ] = []

        for rationale in rationales:
            sentence_ids = rationale[
                "sentences"
            ]

            evidence_sentences = [
                document["abstract"][
                    sentence_id
                ]
                for sentence_id
                in sentence_ids
            ]

            evidence_sets.append(
                {
                    "label": rationale[
                        "label"
                    ],
                    "sentence_ids": (
                        sentence_ids
                    ),
                    "sentences": (
                        evidence_sentences
                    ),
                }
            )

        gold_documents.append(
            {
                "doc_id": doc_id,
                "title": document["title"],
                "evidence_sets": (
                    evidence_sets
                ),
            }
        )

    return gold_documents


# ============================================================
# 6. 整理Top-5检索结果
# ============================================================

def extract_top_retrieved_documents(
    ranking_record: dict[str, Any],
    corpus_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    top_documents: list[
        dict[str, Any]
    ] = []

    retrieved_documents = ranking_record[
        "retrieved_documents"
    ]

    for retrieved in retrieved_documents[:5]:
        doc_id = str(
            retrieved["doc_id"]
        )

        if doc_id not in corpus_by_id:
            raise ValueError(
                f"检索文档{doc_id}"
                "不存在于corpus中。"
            )

        document = corpus_by_id[doc_id]

        top_documents.append(
            {
                "rank": retrieved["rank"],
                "doc_id": doc_id,
                "score": retrieved["score"],
                "is_gold": retrieved[
                    "is_gold"
                ],
                "title": document["title"],
            }
        )

    return top_documents


# ============================================================
# 7. 主程序
# ============================================================

def main() -> None:
    print("正在读取BM25结果……")

    corpus_records = load_jsonl(
        CORPUS_PATH
    )

    train_claims = load_jsonl(
        TRAIN_CLAIMS_PATH
    )

    ranking_records = load_jsonl(
        RANKINGS_PATH
    )

    per_claim_rows = load_csv(
        PER_CLAIM_PATH
    )

    corpus_by_id = {
        str(document["doc_id"]): document
        for document in corpus_records
    }

    claims_by_id = {
        str(claim["id"]): claim
        for claim in train_claims
    }

    rankings_by_claim_id = {
        str(record["claim_id"]): record
        for record in ranking_records
    }

    if len(claims_by_id) != len(
        train_claims
    ):
        raise ValueError(
            "训练集中存在重复claim_id。"
        )

    if len(rankings_by_claim_id) != len(
        ranking_records
    ):
        raise ValueError(
            "BM25排名中存在重复claim_id。"
        )

    category_counts: Counter[str] = (
        Counter()
    )

    label_category_counts: dict[
        str,
        Counter[str],
    ] = {}

    all_case_records: list[
        dict[str, Any]
    ] = []

    hard_failure_records: list[
        dict[str, Any]
    ] = []

    excluded_nei_count = 0

    for row in per_claim_rows:
        claim_id = str(row["claim_id"])

        has_gold_evidence = (
            row["has_gold_evidence"]
            .strip()
            .lower()
            == "true"
        )

        if not has_gold_evidence:
            excluded_nei_count += 1
            continue

        if claim_id not in claims_by_id:
            raise ValueError(
                f"没有找到claim {claim_id}"
            )

        if claim_id not in (
            rankings_by_claim_id
        ):
            raise ValueError(
                f"没有找到claim {claim_id}"
                "的BM25排名。"
            )

        first_relevant_rank_text = row[
            "first_relevant_rank"
        ].strip()

        if not first_relevant_rank_text:
            raise ValueError(
                f"claim {claim_id}存在"
                "金标准证据，但没有第一相关排名。"
            )

        first_relevant_rank = int(
            float(first_relevant_rank_text)
        )

        category = categorize_rank(
            first_relevant_rank
        )

        gold_label = row["gold_label"]

        category_counts[category] += 1

        if gold_label not in (
            label_category_counts
        ):
            label_category_counts[
                gold_label
            ] = Counter()

        label_category_counts[
            gold_label
        ][category] += 1

        claim = claims_by_id[claim_id]

        ranking_record = (
            rankings_by_claim_id[
                claim_id
            ]
        )

        case_record = {
            "claim_id": claim["id"],
            "claim": claim["claim"],
            "gold_label": gold_label,
            "category": category,
            "first_relevant_rank": (
                first_relevant_rank
            ),
            "gold_documents": (
                extract_gold_evidence(
                    claim=claim,
                    corpus_by_id=(
                        corpus_by_id
                    ),
                )
            ),
            "top_5_retrieved_documents": (
                extract_top_retrieved_documents(
                    ranking_record=(
                        ranking_record
                    ),
                    corpus_by_id=(
                        corpus_by_id
                    ),
                )
            ),
        }

        all_case_records.append(
            case_record
        )

        if category == (
            "hard_failure_after_20"
        ):
            hard_failure_records.append(
                case_record
            )

    # 困难案例按照第一篇正确文档的排名
    # 从最差到相对较好排序。
    hard_failure_records.sort(
        key=lambda record: (
            -record[
                "first_relevant_rank"
            ],
            int(record["claim_id"]),
        )
    )

    category_order = [
        "top_1_success",
        "top_5_recovery",
        "top_20_near_miss",
        "hard_failure_after_20",
    ]

    evaluated_count = len(
        all_case_records
    )

    category_summary = {}

    for category in category_order:
        count = category_counts[
            category
        ]

        category_summary[category] = {
            "count": count,
            "rate": (
                count / evaluated_count
                if evaluated_count
                else 0.0
            ),
        }

    label_summary = {}

    for label, counts in sorted(
        label_category_counts.items()
    ):
        label_total = sum(
            counts.values()
        )

        label_summary[label] = {
            "total": label_total,
            "categories": {
                category: {
                    "count": counts[
                        category
                    ],
                    "rate_within_label": (
                        counts[category]
                        / label_total
                        if label_total
                        else 0.0
                    ),
                }
                for category in category_order
            },
        }

    summary = {
        "retrieval_method": "bm25",
        "split": "train",
        "all_claims": len(
            per_claim_rows
        ),
        "evaluated_claims": (
            evaluated_count
        ),
        "excluded_nei_claims": (
            excluded_nei_count
        ),
        "category_definition": {
            "top_1_success": (
                "第一篇金标准文档排名为1"
            ),
            "top_5_recovery": (
                "第一篇金标准文档排名为2至5"
            ),
            "top_20_near_miss": (
                "第一篇金标准文档排名为6至20"
            ),
            "hard_failure_after_20": (
                "第一篇金标准文档排名大于20"
            ),
        },
        "overall_categories": (
            category_summary
        ),
        "categories_by_label": (
            label_summary
        ),
    }

    save_json(
        summary,
        SUMMARY_OUTPUT_PATH,
    )

    save_jsonl(
        all_case_records,
        ALL_CASES_OUTPUT_PATH,
    )

    save_jsonl(
        hard_failure_records,
        HARD_FAILURES_OUTPUT_PATH,
    )

    print()
    print("=" * 70)
    print("BM25案例分类")
    print("=" * 70)

    print(
        f"参与分析：{evaluated_count}"
    )

    print(
        f"排除NEI：{excluded_nei_count}"
    )

    print()

    for category in category_order:
        current = category_summary[
            category
        ]

        print(
            f"{category}: "
            f"{current['count']} "
            f"({current['rate']:.2%})"
        )

    print()
    print("分标签结果：")

    for label, label_result in (
        label_summary.items()
    ):
        print()
        print(
            f"  {label}："
            f"{label_result['total']}条"
        )

        for category in category_order:
            current = (
                label_result[
                    "categories"
                ][category]
            )

            print(
                f"    {category}: "
                f"{current['count']} "
                f"("
                f"{current['rate_within_label']:.2%}"
                f")"
            )

    print()
    print("=" * 70)
    print("最困难的前3条案例")
    print("=" * 70)

    for case in hard_failure_records[:3]:
        print()
        print(
            f"claim_id："
            f"{case['claim_id']}"
        )

        print(
            f"标签：{case['gold_label']}"
        )

        print(
            "第一篇正确文档排名："
            f"{case['first_relevant_rank']}"
        )

        print(
            f"claim：{case['claim']}"
        )

        print("金标准文档：")

        for document in (
            case["gold_documents"]
        ):
            print(
                f"  - {document['title']}"
            )

        print("BM25前3名：")

        for document in (
            case[
                "top_5_retrieved_documents"
            ][:3]
        ):
            print(
                f"  {document['rank']}. "
                f"{document['title']}"
            )

    print()
    print("结果文件：")
    print(f"  {SUMMARY_OUTPUT_PATH}")
    print(f"  {ALL_CASES_OUTPUT_PATH}")
    print(f"  {HARD_FAILURES_OUTPUT_PATH}")


if __name__ == "__main__":
    main()