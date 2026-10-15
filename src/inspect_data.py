from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


# ============================================================
# 1. 项目路径
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"

CORPUS_PATH = RAW_DATA_DIR / "corpus.jsonl"
TRAIN_PATH = RAW_DATA_DIR / "claims_train.jsonl"
DEV_PATH = RAW_DATA_DIR / "claims_dev.jsonl"
TEST_PATH = RAW_DATA_DIR / "claims_test.jsonl"


# ============================================================
# 2. 项目内部使用的标签
# ============================================================

SUPPORT = "SUPPORT"
CONTRADICT = "CONTRADICT"
NOT_ENOUGH_INFO = "NOT_ENOUGH_INFO"
UNLABELED = "UNLABELED"

EVIDENCE_LABELS = {
    SUPPORT,
    CONTRADICT,
}


# ============================================================
# 3. 读取JSONL文件
# ============================================================

def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """
    读取JSONL文件。

    JSONL文件每一行都是一个独立的JSON对象。
    如果某行不是合法JSON，会报告具体文件和行号。
    """
    if not path.exists():
        raise FileNotFoundError(
            f"没有找到文件：{path}\n"
            "请检查SciFact数据是否解压到了data/raw目录。"
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
                record = json.loads(stripped_line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"{path}第{line_number}行不是合法JSON。"
                ) from error

            if not isinstance(record, dict):
                raise ValueError(
                    f"{path}第{line_number}行"
                    "不是JSON对象。"
                )

            records.append(record)

    return records


# ============================================================
# 4. 检查语料库
# ============================================================

def validate_corpus(
    corpus_records: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """
    检查corpus.jsonl并建立doc_id到论文摘要的映射。

    doc_id统一转换成字符串，原因是：
    - corpus中的doc_id通常是整数；
    - claim的evidence对象使用JSON键，读取后一定是字符串。
    """
    corpus_by_id: dict[str, dict[str, Any]] = {}

    required_fields = {
        "doc_id",
        "title",
        "abstract",
        "structured",
    }

    for position, document in enumerate(
        corpus_records,
        start=1,
    ):
        missing_fields = (
            required_fields - document.keys()
        )

        if missing_fields:
            raise ValueError(
                f"语料库第{position}条记录缺少字段："
                f"{sorted(missing_fields)}"
            )

        doc_id = str(document["doc_id"])

        if doc_id in corpus_by_id:
            raise ValueError(
                f"语料库中出现重复doc_id：{doc_id}"
            )

        if not isinstance(document["title"], str):
            raise ValueError(
                f"文档{doc_id}的title不是字符串。"
            )

        abstract = document["abstract"]

        if not isinstance(abstract, list):
            raise ValueError(
                f"文档{doc_id}的abstract不是列表。"
            )

        if not all(
            isinstance(sentence, str)
            for sentence in abstract
        ):
            raise ValueError(
                f"文档{doc_id}的abstract中"
                "存在非字符串句子。"
            )

        corpus_by_id[doc_id] = document

    return corpus_by_id


# ============================================================
# 5. 从金标准证据推导claim标签
# ============================================================

def derive_claim_label(
    claim: dict[str, Any],
    is_labeled_split: bool,
) -> str:
    """
    train和dev：
    - evidence为空 -> NOT_ENOUGH_INFO
    - evidence中有标注 -> SUPPORT或CONTRADICT

    test：
    - 官方没有公开标签，统一记为UNLABELED
    """
    if not is_labeled_split:
        return UNLABELED

    if "evidence" not in claim:
        raise ValueError(
            f"有标签数据中的claim {claim.get('id')}"
            "缺少evidence字段。"
        )

    evidence = claim["evidence"]

    if not isinstance(evidence, dict):
        raise ValueError(
            f"claim {claim.get('id')}的"
            "evidence不是JSON对象。"
        )

    if not evidence:
        return NOT_ENOUGH_INFO

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
                    f"未知证据标签：{label!r}"
                )

            labels.add(label)

    if len(labels) != 1:
        raise ValueError(
            f"claim {claim.get('id')}包含多个"
            f"相互冲突的标签：{sorted(labels)}"
        )

    return next(iter(labels))


# ============================================================
# 6. 检查一个claim数据集
# ============================================================

def inspect_claim_split(
    split_name: str,
    claims: list[dict[str, Any]],
    corpus_by_id: dict[str, dict[str, Any]],
    is_labeled_split: bool,
) -> dict[str, Any]:
    claim_ids: set[int] = set()
    label_counts: Counter[str] = Counter()

    evidence_document_count = 0
    rationale_count = 0
    evidence_sentence_count = 0
    cited_document_count = 0
    claims_with_cited_doc_ids = 0

    missing_document_references: list[str] = []
    invalid_sentence_references: list[str] = []

    # 所有数据划分都必须提供id和claim文本。
    required_fields = {
        "id",
        "claim",
    }

    # train和dev有人工标签，因此还必须提供
    # evidence和cited_doc_ids。
    # 官方test集没有公开这些标注字段。
    if is_labeled_split:
        required_fields.update(
            {
                "evidence",
                "cited_doc_ids",
            }
        )

    for position, claim in enumerate(
        claims,
        start=1,
    ):
        missing_fields = (
            required_fields - claim.keys()
        )

        if missing_fields:
            raise ValueError(
                f"{split_name}第{position}条claim"
                f"缺少字段：{sorted(missing_fields)}"
            )

        claim_id = claim["id"]

        if claim_id in claim_ids:
            raise ValueError(
                f"{split_name}中出现重复claim_id："
                f"{claim_id}"
            )

        claim_ids.add(claim_id)

        if not isinstance(claim["claim"], str):
            raise ValueError(
                f"claim {claim_id}的claim文本"
                "不是字符串。"
            )

        # test集可能不提供cited_doc_ids，
        # 因此使用get读取，而不是直接访问。
        cited_doc_ids = claim.get(
            "cited_doc_ids"
        )

        if cited_doc_ids is not None:
            if not isinstance(
                cited_doc_ids,
                list,
            ):
                raise ValueError(
                    f"claim {claim_id}的"
                    "cited_doc_ids不是列表。"
                )

            claims_with_cited_doc_ids += 1
            cited_document_count += len(
                cited_doc_ids
            )

        label = derive_claim_label(
            claim=claim,
            is_labeled_split=is_labeled_split,
        )

        label_counts[label] += 1

        # 未标注test集不做金标准证据检查。
        if not is_labeled_split:
            continue

        evidence = claim["evidence"]

        for raw_doc_id, rationales in (
            evidence.items()
        ):
            doc_id = str(raw_doc_id)
            evidence_document_count += 1

            if doc_id not in corpus_by_id:
                missing_document_references.append(
                    f"claim={claim_id}, "
                    f"doc_id={doc_id}"
                )
                continue

            abstract = corpus_by_id[
                doc_id
            ]["abstract"]

            for rationale in rationales:
                rationale_count += 1

                sentence_ids = rationale.get(
                    "sentences"
                )

                if not isinstance(
                    sentence_ids,
                    list,
                ):
                    raise ValueError(
                        f"claim {claim_id}、"
                        f"文档{doc_id}的"
                        "sentences不是列表。"
                    )

                evidence_sentence_count += len(
                    sentence_ids
                )

                for sentence_id in sentence_ids:
                    is_valid_integer = (
                        isinstance(
                            sentence_id,
                            int,
                        )
                        and not isinstance(
                            sentence_id,
                            bool,
                        )
                    )

                    if not is_valid_integer:
                        invalid_sentence_references.append(
                            f"claim={claim_id}, "
                            f"doc_id={doc_id}, "
                            f"sentence_id="
                            f"{sentence_id!r}"
                        )
                        continue

                    if not (
                        0
                        <= sentence_id
                        < len(abstract)
                    ):
                        invalid_sentence_references.append(
                            f"claim={claim_id}, "
                            f"doc_id={doc_id}, "
                            f"sentence_id="
                            f"{sentence_id}, "
                            f"abstract_length="
                            f"{len(abstract)}"
                        )

    if claims_with_cited_doc_ids > 0:
        average_cited_documents = (
            cited_document_count
            / claims_with_cited_doc_ids
        )
    else:
        average_cited_documents = None

    return {
        "split": split_name,
        "claim_count": len(claims),
        "label_counts": label_counts,
        "evidence_document_count": (
            evidence_document_count
        ),
        "rationale_count": rationale_count,
        "evidence_sentence_count": (
            evidence_sentence_count
        ),
        "average_cited_documents": (
            average_cited_documents
        ),
        "claims_with_cited_doc_ids": (
            claims_with_cited_doc_ids
        ),
        "missing_document_references": (
            missing_document_references
        ),
        "invalid_sentence_references": (
            invalid_sentence_references
        ),
    }

# ============================================================
# 7. 打印检查结果
# ============================================================

def print_split_summary(
    summary: dict[str, Any],
) -> None:
    print()
    print("=" * 70)
    print(f"数据集：{summary['split']}")
    print("=" * 70)

    print(
        f"claim数量："
        f"{summary['claim_count']}"
    )

    print("标签分布：")

    for label, count in sorted(
        summary["label_counts"].items()
    ):
        print(f"  {label}: {count}")

    average_cited_documents = summary[
        "average_cited_documents"
    ]

    if average_cited_documents is None:
        print(
            "平均每条claim的"
            "cited_doc_ids数量：未提供"
        )
    else:
        print(
            "平均每条claim的"
            "cited_doc_ids数量："
            f"{average_cited_documents:.2f}"
        )

    if UNLABELED not in summary["label_counts"]:
        print(
            "金标准证据文档引用数："
            f"{summary['evidence_document_count']}"
        )
        print(
            "金标准rationale数量："
            f"{summary['rationale_count']}"
        )
        print(
            "金标准证据句引用数："
            f"{summary['evidence_sentence_count']}"
        )

    missing_documents = summary[
        "missing_document_references"
    ]

    invalid_sentences = summary[
        "invalid_sentence_references"
    ]

    print(
        "不存在于corpus中的证据文档引用："
        f"{len(missing_documents)}"
    )

    print(
        "越界或无效的证据句引用："
        f"{len(invalid_sentences)}"
    )

    if missing_documents:
        print("前5条缺失文档引用：")

        for item in missing_documents[:5]:
            print(f"  {item}")

    if invalid_sentences:
        print("前5条无效句子引用：")

        for item in invalid_sentences[:5]:
            print(f"  {item}")


# ============================================================
# 8. 显示一条样例，帮助理解字段
# ============================================================

def print_example(
    claims: list[dict[str, Any]],
    corpus_by_id: dict[str, dict[str, Any]],
) -> None:
    example = next(
        claim
        for claim in claims
        if claim.get("evidence")
    )

    label = derive_claim_label(
        claim=example,
        is_labeled_split=True,
    )

    print()
    print("=" * 70)
    print("一条带证据的训练样例")
    print("=" * 70)

    print(f"claim_id：{example['id']}")
    print(f"claim：{example['claim']}")
    print(f"推导标签：{label}")
    print(
        "cited_doc_ids："
        f"{example['cited_doc_ids']}"
    )

    for raw_doc_id, rationales in (
        example["evidence"].items()
    ):
        doc_id = str(raw_doc_id)
        document = corpus_by_id[doc_id]

        print()
        print(f"金标准文档：{doc_id}")
        print(f"标题：{document['title']}")

        for rationale_index, rationale in enumerate(
            rationales,
            start=1,
        ):
            print(
                f"  rationale {rationale_index}："
                f"{rationale['sentences']}"
            )

            for sentence_id in rationale["sentences"]:
                sentence = document["abstract"][
                    sentence_id
                ]

                print(
                    f"    [{sentence_id}] {sentence}"
                )


# ============================================================
# 9. 主程序
# ============================================================

def main() -> None:
    print("正在读取SciFact数据……")

    corpus_records = load_jsonl(CORPUS_PATH)
    train_claims = load_jsonl(TRAIN_PATH)
    dev_claims = load_jsonl(DEV_PATH)
    test_claims = load_jsonl(TEST_PATH)

    corpus_by_id = validate_corpus(
        corpus_records
    )

    print()
    print("=" * 70)
    print("语料库")
    print("=" * 70)
    print(f"文档数量：{len(corpus_by_id)}")

    abstract_sentence_counts = [
        len(document["abstract"])
        for document in corpus_by_id.values()
    ]

    average_abstract_length = (
        sum(abstract_sentence_counts)
        / len(abstract_sentence_counts)
    )

    print(
        "平均每篇摘要的句子数："
        f"{average_abstract_length:.2f}"
    )

    summaries = [
        inspect_claim_split(
            split_name="train",
            claims=train_claims,
            corpus_by_id=corpus_by_id,
            is_labeled_split=True,
        ),
        inspect_claim_split(
            split_name="dev",
            claims=dev_claims,
            corpus_by_id=corpus_by_id,
            is_labeled_split=True,
        ),
        inspect_claim_split(
            split_name="test",
            claims=test_claims,
            corpus_by_id=corpus_by_id,
            is_labeled_split=False,
        ),
    ]

    for summary in summaries:
        print_split_summary(summary)

    print_example(
        claims=train_claims,
        corpus_by_id=corpus_by_id,
    )

    total_errors = sum(
        len(summary["missing_document_references"])
        + len(summary["invalid_sentence_references"])
        for summary in summaries
    )

    print()
    print("=" * 70)

    if total_errors == 0:
        print("数据检查通过：没有发现引用完整性错误。")
    else:
        print(
            "数据检查完成，但发现"
            f"{total_errors}个引用完整性问题。"
        )

    print("=" * 70)


if __name__ == "__main__":
    main()