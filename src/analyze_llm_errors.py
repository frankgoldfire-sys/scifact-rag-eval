from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from run_bm25 import (
    CORPUS_PATH,
    PROJECT_ROOT,
    RESULTS_DIR,
    load_jsonl,
    save_json,
    save_jsonl,
)

from evaluate_llm import (
    build_scored_rows,
)


TAG = "dev_all_k5_seed20261013"

PROFILE = (
    f"{TAG}_f2227ecf8839_responses"
)

DEFAULT_RESPONSES = (
    RESULTS_DIR
    / "llm"
    / f"{PROFILE}.jsonl"
)

DEFAULT_EVALUATION = (
    PROJECT_ROOT
    / "data"
    / "prepared"
    / f"{TAG}_evaluation.jsonl"
)

GROUP_NAMES = {
    "gold_hit_correct": "命中标注证据，标签一致",
    "gold_hit_wrong": "命中标注证据，标签不一致",
    "gold_miss_wrong": "未命中标注证据，标签不一致",
    "gold_miss_correct": "未命中标注证据，标签一致",
    "nei_wrong": "NEI被预测为其他标签",
    "nei_correct": "NEI预测一致",
}

FOCUS_GROUPS = (
    "gold_hit_wrong",
    "gold_miss_wrong",
    "gold_miss_correct",
    "nei_wrong",
)


# ============================================================
# 1. 参数与案例分组
# ============================================================

def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "整理Dense错误分析和引用核对材料，"
            "不调用API。"
        )
    )

    parser.add_argument(
        "--responses",
        type=Path,
        default=DEFAULT_RESPONSES,
    )

    parser.add_argument(
        "--evaluation",
        type=Path,
        default=DEFAULT_EVALUATION,
    )

    parser.add_argument(
        "--examples-per-group",
        type=int,
        default=2,
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
    )

    args = parser.parse_args()

    if args.examples_per_group <= 0:
        parser.error(
            "--examples-per-group必须大于0。"
        )

    return args


def classify_case(row):
    if not row["has_gold_evidence"]:
        return (
            "nei_correct"
            if row["correct"]
            else "nei_wrong"
        )

    if row["gold_document_hit"]:
        return (
            "gold_hit_correct"
            if row["correct"]
            else "gold_hit_wrong"
        )

    return (
        "gold_miss_correct"
        if row["correct"]
        else "gold_miss_wrong"
    )


# ============================================================
# 2. 引用编号和金标准匹配检查
# ============================================================

def citation_diagnostic(
    gold,
    prediction,
    corpus,
):
    """
    仅检查引用编号和标注证据句匹配。

    不自动判定引用内容是否在语义上
    支持模型结论。
    """
    errors = []
    cited_points = set()

    citations = (
        prediction.get("citations")
        if isinstance(prediction, dict)
        else None
    )

    visible = set(
        map(
            str,
            gold["context_document_ids"],
        )
    )

    if not isinstance(citations, list):
        errors.append(
            "citations缺失或不是列表。"
        )
        citations = []

    for citation in citations:
        if not isinstance(citation, dict):
            errors.append(
                "引用项不是对象。"
            )
            continue

        doc_id = citation.get("doc_id")
        ids = citation.get("sentence_ids")

        if (
            not isinstance(doc_id, str)
            or doc_id not in visible
            or doc_id not in corpus
        ):
            errors.append(
                "文档ID未提供或不存在。"
            )
            continue

        if (
            not isinstance(ids, list)
            or not ids
        ):
            errors.append(
                "句子编号不是非空列表。"
            )
            continue

        for index in ids:
            if (
                type(index) is not int
                or not 0 <= index
                < len(corpus[doc_id]["abstract"])
            ):
                errors.append(
                    "句子编号类型不合法或越界。"
                )
            else:
                cited_points.add(
                    (doc_id, index)
                )

    annotated_points = set()
    rationales = []

    for doc_id, sentence_sets in (
        gold["gold_rationales"].items()
    ):
        for sentence_set in sentence_sets:
            points = {
                (str(doc_id), int(i))
                for i in sentence_set
            }

            annotated_points.update(
                points
            )

            if points:
                rationales.append(points)

    overlap = (
        cited_points & annotated_points
    )

    has_gold = bool(
        gold["gold_document_ids"]
    )

    complete = (
        any(
            rationale.issubset(cited_points)
            for rationale in rationales
        )
        if has_gold and not errors
        else None
    )

    return {
        "reference_ids_valid": not errors,
        "reference_errors": errors,
        "has_citations": bool(cited_points),
        "unique_cited_sentence_count": len(
            cited_points
        ),
        "annotated_sentence_overlap_count": len(
            overlap
        ),
        "annotated_sentence_overlap_fraction": (
            len(overlap) / len(cited_points)
            if (
                has_gold
                and cited_points
                and not errors
            )
            else None
        ),
        "contains_complete_gold_rationale": (
            complete
        ),
        "cited_sentence_points": [
            {
                "doc_id": doc_id,
                "sentence_id": index,
            }
            for doc_id, index in sorted(
                cited_points
            )
        ],
    }


def document_view(
    doc_id,
    corpus,
):
    document = corpus[doc_id]

    return {
        "doc_id": doc_id,
        "title": document["title"],
        "sentences": [
            {
                "sentence_id": index,
                "text": sentence,
            }
            for index, sentence in enumerate(
                document["abstract"]
            )
        ],
    }


# ============================================================
# 3. 构建完整核对材料
# ============================================================

def build_analysis(
    evaluation,
    journal,
    corpus,
):
    (
        scored,
        final_responses,
        api_errors,
    ) = build_scored_rows(
        evaluation,
        journal,
    )

    metadata = {
        row["request_id"]: row
        for row in evaluation
    }

    by_claim = {}

    for row in scored:
        by_claim.setdefault(
            str(row["claim_id"]),
            {},
        )[row["condition"]] = row

    citation_summaries = {}

    for condition in dict.fromkeys(
        row["condition"]
        for row in scored
    ):
        rows = [
            row
            for row in scored
            if row["condition"] == condition
        ]

        diagnostics = [
            (
                row,
                citation_diagnostic(
                    metadata[row["request_id"]],
                    final_responses[
                        row["request_id"]
                    ].get("prediction"),
                    corpus,
                ),
            )
            for row in rows
        ]

        evidence_claims = [
            (row, diagnostic)
            for row, diagnostic in diagnostics
            if row["decision_basis"]
            == "provided_evidence"
        ]

        citation_summaries[condition] = {
            "claim_count": len(rows),
            "self_reported_provided_evidence_count": (
                len(evidence_claims)
            ),
            "with_valid_nonempty_citations": sum(
                d["reference_ids_valid"]
                and d["has_citations"]
                for _, d in evidence_claims
            ),
            "complete_gold_rationale_count": sum(
                d["contains_complete_gold_rationale"]
                is True
                for _, d in evidence_claims
            ),
            "complete_gold_rationale_but_label_disagreement_count": sum(
                (
                    d["contains_complete_gold_rationale"]
                    is True
                )
                and not row["correct"]
                for row, d in evidence_claims
            ),
        }

    cases = []

    dense_rows = sorted(
        (
            row
            for row in scored
            if row["condition"]
            == "retrieved_dense"
        ),
        key=lambda row: int(
            row["claim_id"]
        ),
    )

    for row in dense_rows:
        gold = metadata[
            row["request_id"]
        ]

        response = final_responses[
            row["request_id"]
        ]

        diagnostic = citation_diagnostic(
            gold,
            response.get("prediction"),
            corpus,
        )

        other_rows = by_claim[
            str(row["claim_id"])
        ]

        group = classify_case(row)
        flags = []

        if not row["label_valid"]:
            flags.append(
                "OUTPUT_PARSE_OR_LABEL_FAILURE"
            )

        if (
            diagnostic[
                "contains_complete_gold_rationale"
            ] is True
            and not row["correct"]
        ):
            flags.append(
                "ANNOTATION_MATCH_WITH_LABEL_DISAGREEMENT"
            )

        if group == "gold_miss_correct":
            flags.append(
                "CHECK_UNANNOTATED_EVIDENCE_OR_MODEL_KNOWLEDGE"
            )

        if group == "nei_wrong":
            flags.append(
                "CHECK_ANNOTATION_SCOPE_AND_KNOWLEDGE_POLICY"
            )

        cases.append(
            {
                "claim_id": row["claim_id"],
                "claim": row["claim"],
                "group": group,
                "group_description": (
                    GROUP_NAMES[group]
                ),
                "focus_case": (
                    group in FOCUS_GROUPS
                ),
                "gold_label": row["gold_label"],
                "dense_prediction": response.get(
                    "prediction"
                ),
                "dense_correct": row["correct"],
                "dense_validation_errors": (
                    row["validation_errors"]
                ),
                "dense_gold_document_hit": (
                    row["gold_document_hit"]
                ),
                "gold_document_ids": (
                    gold["gold_document_ids"]
                ),
                "citation_diagnostic": diagnostic,
                "review_flags": flags,
                "comparisons": {
                    condition: {
                        "predicted_label": (
                            other["predicted_label"]
                        ),
                        "correct": other["correct"],
                        "decision_basis": (
                            other["decision_basis"]
                        ),
                        "gold_document_hit": (
                            other["gold_document_hit"]
                        ),
                        "explanation": (
                            other["explanation"]
                        ),
                    }
                    for condition, other
                    in other_rows.items()
                },
                "provided_documents": [
                    document_view(
                        str(doc_id),
                        corpus,
                    )
                    for doc_id in gold[
                        "context_document_ids"
                    ]
                ],
                "gold_evidence_documents": [
                    {
                        **document_view(
                            str(doc_id),
                            corpus,
                        ),
                        "gold_rationales": (
                            gold["gold_rationales"][
                                str(doc_id)
                            ]
                        ),
                        "included_in_dense_context": (
                            str(doc_id)
                            in set(
                                gold[
                                    "context_document_ids"
                                ]
                            )
                        ),
                    }
                    for doc_id in gold[
                        "gold_document_ids"
                    ]
                ],
                "manual_review": {
                    "status": "UNREVIEWED",
                    "evidence_supports_model_conclusion": (
                        "UNREVIEWED"
                    ),
                    "suspected_annotation_issue": (
                        "UNREVIEWED"
                    ),
                    "error_type": "UNREVIEWED",
                    "notes": "",
                },
            }
        )

    group_counts = Counter(
        case["group"]
        for case in cases
    )

    missed_wrong = [
        case
        for case in cases
        if case["group"]
        == "gold_miss_wrong"
    ]

    # 这些是需要进一步检查的候选，
    # 不作为检索导致错误的因果证明。
    oracle_correct_candidates = [
        case["claim_id"]
        for case in missed_wrong
        if case["comparisons"][
            "oracle_cited"
        ]["correct"]
    ]

    summary = {
        "generated_at_utc": (
            datetime.now(timezone.utc)
            .isoformat()
        ),
        "claim_count": len(cases),
        "focus_case_count": sum(
            case["focus_case"]
            for case in cases
        ),
        "dense_label_disagreement_count": sum(
            not case["dense_correct"]
            for case in cases
        ),
        "group_counts": {
            name: group_counts[name]
            for name in GROUP_NAMES
        },
        "oracle_correct_dense_miss_wrong_candidate_ids": (
            oracle_correct_candidates
        ),
        "api_error_events": api_errors,
        "citation_diagnostics": (
            citation_summaries
        ),
        "limitations": [
            "Annotation matching is not semantic citation correctness.",
            "Non-gold documents may contain unannotated useful evidence.",
            "Oracle label agreement does not guarantee a scientifically correct answer.",
            "Conditional groups do not establish causal error propagation.",
            "Review examples prioritize selected groups; not a random citation-quality sample.",
        ],
    }

    return cases, summary


# ============================================================
# 4. 生成便于阅读的人工核对文档
# ============================================================

def render_review(
    cases,
    examples_per_group,
):
    lines = [
        "# Dense案例人工核对",
        "",
        "按claim ID排序，每个重点分组取前几条；这不是代表性随机抽样。",
        "保持原始标签和主评分不变，在人工记录中说明疑点。",
        "",
    ]

    for group in FOCUS_GROUPS:
        examples = [
            case
            for case in cases
            if case["group"] == group
        ][:examples_per_group]

        lines.extend(
            [
                f"## {GROUP_NAMES[group]}",
                "",
            ]
        )

        for case in examples:
            prediction = (
                case["dense_prediction"]
                or {}
            )

            lines.extend(
                [
                    f"### claim {case['claim_id']}",
                    "",
                    case["claim"],
                    "",
                    f"- 标准标签：{case['gold_label']}",
                    (
                        "- Dense预测："
                        f"{prediction.get('label', 'INVALID')}"
                    ),
                    (
                        "- 自报依据："
                        f"{prediction.get('decision_basis', 'INVALID')}"
                    ),
                    (
                        "- 引用了完整金标准rationale："
                        f"{case['citation_diagnostic']['contains_complete_gold_rationale']}"
                    ),
                    (
                        "- 自动核对提示："
                        f"{case['review_flags']}"
                    ),
                    "",
                    "模型解释：",
                    "",
                    str(
                        prediction.get(
                            "explanation",
                            "无可解析解释；请查看原始响应日志。",
                        )
                    ),
                    "",
                    "其他条件：",
                    "",
                ]
            )

            for condition, other in (
                case["comparisons"].items()
            ):
                lines.append(
                    f"- {condition}："
                    f"{other['predicted_label']}；"
                    "与标签一致="
                    f"{other['correct']}；"
                    f"依据={other['decision_basis']}"
                )

            lines.extend(
                [
                    "",
                    "模型引用句：",
                    "",
                ]
            )

            points = case[
                "citation_diagnostic"
            ]["cited_sentence_points"]

            provided = {
                d["doc_id"]: d
                for d in case[
                    "provided_documents"
                ]
            }

            if not points:
                lines.append(
                    "无有效引用句。"
                )

            for point in points:
                doc_id = point["doc_id"]
                index = point["sentence_id"]

                text = provided[doc_id][
                    "sentences"
                ][index]["text"]

                lines.append(
                    f"- 文档{doc_id}，"
                    f"句{index}：{text}"
                )

            lines.extend(
                [
                    "",
                    "金标准rationale（与模型输入分开列出）：",
                    "",
                ]
            )

            if not case[
                "gold_evidence_documents"
            ]:
                lines.append(
                    "NEI没有正向证据标注。"
                )

            for document in case[
                "gold_evidence_documents"
            ]:
                lines.append(
                    f"- 文档{document['doc_id']}："
                    f"{document['title']}；"
                    "进入Dense上下文="
                    f"{document['included_in_dense_context']}"
                )

                for rationale in document[
                    "gold_rationales"
                ]:
                    for index in rationale:
                        text = document[
                            "sentences"
                        ][index]["text"]

                        lines.append(
                            f"  - 句{index}：{text}"
                        )

            lines.extend(
                [
                    "",
                    "Dense提供的完整摘要：",
                    "",
                ]
            )

            for document in case[
                "provided_documents"
            ]:
                lines.extend(
                    [
                        (
                            "#### 文档 "
                            f"{document['doc_id']}："
                            f"{document['title']}"
                        ),
                        "",
                    ]
                )

                for sentence in document[
                    "sentences"
                ]:
                    text = str(
                        sentence["text"]
                    ).replace("\n", " ")

                    lines.append(
                        f"[{sentence['sentence_id']}] "
                        f"{text}"
                    )

                    lines.append("")

            lines.extend(
                [
                    "人工核对记录：",
                    "",
                    "- 引用是否支持模型结论：UNREVIEWED",
                    "- 是否存在疑似标注问题：UNREVIEWED",
                    "- 错误类型：UNREVIEWED",
                    "- 判断理由：",
                    "",
                ]
            )

    return "\n".join(lines) + "\n"


# ============================================================
# 5. 主程序
# ============================================================

def main():
    args = parse_arguments()

    output_dir = (
        RESULTS_DIR / "error_analysis"
    )

    prefix = args.responses.stem

    cases_path = (
        output_dir
        / f"{prefix}_dense_cases.jsonl"
    )

    summary_path = (
        output_dir
        / f"{prefix}_audit_summary.json"
    )

    review_path = (
        output_dir
        / f"{prefix}_manual_review.md"
    )

    for path in (
        cases_path,
        summary_path,
        review_path,
    ):
        if (
            path.exists()
            and not args.overwrite
        ):
            raise FileExistsError(
                f"文件已存在：{path}。"
                "为保留人工批注，默认不覆盖。"
            )

    documents = load_jsonl(
        CORPUS_PATH
    )

    corpus = {
        str(document["doc_id"]): document
        for document in documents
    }

    if len(corpus) != len(documents):
        raise ValueError(
            "corpus中存在重复doc_id。"
        )

    cases, summary = build_analysis(
        load_jsonl(args.evaluation),
        load_jsonl(args.responses),
        corpus,
    )

    summary["source_responses"] = str(
        args.responses.resolve()
    )

    summary["source_evaluation"] = str(
        args.evaluation.resolve()
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    save_jsonl(
        cases,
        cases_path,
    )

    save_json(
        summary,
        summary_path,
    )

    with review_path.open(
        "w",
        encoding="utf-8",
    ) as file:
        file.write(
            render_review(
                cases,
                args.examples_per_group,
            )
        )

    print(
        "Dense错误分析材料整理完成"
    )

    print(
        f"全部claim数：{summary['claim_count']}"
    )

    print(
        "标签不一致数："
        f"{summary['dense_label_disagreement_count']}"
    )

    print(
        "重点核对案例数："
        f"{summary['focus_case_count']}"
    )

    for group, description in (
        GROUP_NAMES.items()
    ):
        print(
            f"  {description}："
            f"{summary['group_counts'][group]}"
        )

    print(
        "Oracle标签一致、"
        "Dense漏检且标签不一致的候选："
        f"{summary['oracle_correct_dense_miss_wrong_candidate_ids']}"
    )

    print(
        "引用与标注重合不等于引用语义正确，"
        "仍需人工核对。"
    )

    print("结果文件：")

    for path in (
        cases_path,
        summary_path,
        review_path,
    ):
        print(f"  {path}")


if __name__ == "__main__":
    main()