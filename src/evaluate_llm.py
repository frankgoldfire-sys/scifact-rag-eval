from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from run_bm25 import (
    PROJECT_ROOT,
    RESULTS_DIR,
    load_jsonl,
    save_csv,
    save_json,
)


# ============================================================
# 1. 默认设置
# ============================================================

LABELS = (
    "SUPPORT",
    "CONTRADICT",
    "NOT_ENOUGH_INFO",
)

INVALID = "INVALID"

CONDITIONS = (
    "oracle_cited",
    "retrieved_bm25",
    "retrieved_dense",
    "retrieved_hybrid",
    "no_evidence",
    "distractor_dense",
)

TAG = "train_pilot_per_label10_k5_seed20261013"

DEFAULT_EVALUATION = (
    PROJECT_ROOT
    / "data"
    / "prepared"
    / f"{TAG}_evaluation.jsonl"
)


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="评测已保存的大模型回答，不调用API。"
    )

    parser.add_argument(
        "--responses",
        type=Path,
        required=True,
    )

    parser.add_argument(
        "--evaluation",
        type=Path,
        default=DEFAULT_EVALUATION,
    )

    return parser.parse_args()


# ============================================================
# 2. Accuracy、Macro-F1和混淆矩阵
# ============================================================

def calculate_classification_metrics(
    gold_labels,
    predicted_labels,
):
    if (
        not gold_labels
        or len(gold_labels) != len(predicted_labels)
    ):
        raise ValueError(
            "标签数组为空或长度不一致。"
        )

    # 行：标准答案；列：模型预测。
    # INVALID保留在分母中，作为错误预测。
    confusion = {
        gold: {
            pred: 0
            for pred in (*LABELS, INVALID)
        }
        for gold in LABELS
    }

    for gold, pred in zip(
        gold_labels,
        predicted_labels,
    ):
        if (
            gold not in LABELS
            or pred not in (*LABELS, INVALID)
        ):
            raise ValueError(
                "出现未知标签。"
            )

        confusion[gold][pred] += 1

    per_class = {}

    for label in LABELS:
        tp = confusion[label][label]

        fp = sum(
            confusion[other][label]
            for other in LABELS
            if other != label
        )

        fn = sum(
            confusion[label][other]
            for other in (*LABELS, INVALID)
            if other != label
        )

        precision = (
            tp / (tp + fp)
            if tp + fp
            else 0.0
        )

        recall = (
            tp / (tp + fn)
            if tp + fn
            else 0.0
        )

        f1 = (
            2 * tp / (2 * tp + fp + fn)
            if 2 * tp + fp + fn
            else 0.0
        )

        per_class[label] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": sum(
                confusion[label].values()
            ),
        }

    correct_count = sum(
        confusion[label][label]
        for label in LABELS
    )

    return {
        "claim_count": len(gold_labels),
        "correct_count": correct_count,
        "accuracy": (
            correct_count / len(gold_labels)
        ),
        "macro_f1": (
            sum(
                per_class[label]["f1"]
                for label in LABELS
            ) / len(LABELS)
        ),
        "per_class": per_class,
        "confusion_matrix": confusion,
    }


# ============================================================
# 3. 对齐回答与标准答案
# ============================================================

def build_scored_rows(
    evaluation,
    journal,
):
    evaluation_map = {
        row["request_id"]: row
        for row in evaluation
    }

    if (
        not evaluation
        or len(evaluation_map) != len(evaluation)
    ):
        raise ValueError(
            "评测输入为空或存在重复request_id。"
        )

    config_hashes = {
        row["config_hash"]
        for row in journal
    }

    if len(config_hashes) != 1:
        raise ValueError(
            "响应文件为空或混入多种实验配置。"
        )

    final_responses = {}
    api_error_count = 0

    for row in journal:
        rid = row["request_id"]

        if rid not in evaluation_map:
            raise ValueError(
                f"响应文件包含评测输入之外的请求：{rid}"
            )

        # API错误是尝试记录，不是最终预测。
        if row["event"] == "api_error":
            api_error_count += 1
            continue

        if row["event"] not in (
            "response",
            "reused",
        ):
            raise ValueError(
                f"未知日志事件：{row['event']}"
            )

        if rid in final_responses:
            raise ValueError(
                f"同一请求存在多个最终回答：{rid}"
            )

        final_responses[rid] = row

    missing = sorted(
        set(evaluation_map)
        - set(final_responses)
    )

    if missing:
        raise ValueError(
            f"还有{len(missing)}份输入没有回答。"
            "请先断点续跑。"
            f"前几个缺失请求：{missing[:6]}"
        )

    claim_conditions = {}

    for row in evaluation:
        cid = str(row["claim_id"])

        claim_conditions.setdefault(
            cid,
            set(),
        ).add(row["condition"])

    if any(
        values != set(CONDITIONS)
        for values in claim_conditions.values()
    ):
        raise ValueError(
            "部分claim缺少预期的六种条件。"
        )

    if (
        len(evaluation)
        != len(claim_conditions) * len(CONDITIONS)
    ):
        raise ValueError(
            "claim与证据条件的数量不一致。"
        )

    scored = []

    for gold in evaluation:
        response = final_responses[
            gold["request_id"]
        ]

        if (
            str(response["claim_id"])
            != str(gold["claim_id"])
        ):
            raise ValueError(
                "响应与评测文件的claim_id不一致。"
            )

        prediction = response.get(
            "prediction"
        )

        prediction = (
            prediction
            if isinstance(prediction, dict)
            else {}
        )

        label = prediction.get("label")

        label_valid = (
            isinstance(label, str)
            and label in LABELS
        )

        predicted_label = (
            label
            if label_valid
            else INVALID
        )

        validation_errors = (
            response.get("validation_errors")
            or []
        )

        basis = prediction.get(
            "decision_basis"
        )

        basis = (
            basis
            if isinstance(basis, str)
            else INVALID
        )

        citations = prediction.get(
            "citations"
        )

        citations = (
            citations
            if isinstance(citations, list)
            else []
        )

        raw_response = (
            response.get("raw_response")
            or {}
        )

        raw_usage = (
            raw_response.get("usage")
            or {}
        )

        charged_usage = (
            response.get("usage")
            or {}
        )

        if gold["has_gold_evidence"]:
            retrieval_state = (
                "annotated_gold_present"
                if gold["gold_document_hit"]
                else "annotated_gold_absent"
            )
        else:
            retrieval_state = (
                "NEI_no_positive_gold"
            )

        scored.append(
            {
                "request_id": gold["request_id"],
                "claim_id": gold["claim_id"],
                "claim": gold["claim"],
                "condition": gold["condition"],
                "gold_label": gold["gold_label"],
                "predicted_label": predicted_label,
                "label_valid": label_valid,
                "correct": (
                    predicted_label
                    == gold["gold_label"]
                ),
                "format_and_ids_valid": (
                    not validation_errors
                ),
                "validation_errors": (
                    validation_errors
                ),
                "decision_basis": basis,
                "has_gold_evidence": (
                    gold["has_gold_evidence"]
                ),
                "gold_document_hit": (
                    gold["gold_document_hit"]
                ),
                "gold_document_recall": (
                    gold["gold_document_recall"]
                ),
                "retrieval_state": retrieval_state,
                "context_document_count": (
                    gold["context_document_count"]
                ),
                "context_document_ids": (
                    gold["context_document_ids"]
                ),
                "citations": citations,
                "explanation": prediction.get(
                    "explanation",
                    "",
                ),
                "response_event": response["event"],
                "returned_model": raw_response.get(
                    "model"
                ),
                "system_fingerprint": (
                    raw_response.get(
                        "system_fingerprint"
                    )
                ),
                "prompt_tokens": raw_usage.get(
                    "prompt_tokens"
                ),
                "completion_tokens": raw_usage.get(
                    "completion_tokens"
                ),
                "actual_reported_total_tokens": (
                    charged_usage.get("total_tokens")
                    if response["event"] == "response"
                    else 0
                ),
                "latency_seconds": response.get(
                    "latency_seconds"
                ),
            }
        )

    return (
        scored,
        final_responses,
        api_error_count,
    )


# ============================================================
# 4. 按证据条件汇总
# ============================================================

def summarize(scored):
    summary = []
    details = {}

    for condition in CONDITIONS:
        rows = [
            row
            for row in scored
            if row["condition"] == condition
        ]

        metrics = (
            calculate_classification_metrics(
                [
                    row["gold_label"]
                    for row in rows
                ],
                [
                    row["predicted_label"]
                    for row in rows
                ],
            )
        )

        summary.append(
            {
                "condition": condition,
                "claim_count": (
                    metrics["claim_count"]
                ),
                "correct_count": (
                    metrics["correct_count"]
                ),
                "accuracy": metrics["accuracy"],
                "macro_f1": metrics["macro_f1"],
                "invalid_label_count": sum(
                    not row["label_valid"]
                    for row in rows
                ),
                "format_and_ids_error_count": sum(
                    not row["format_and_ids_valid"]
                    for row in rows
                ),
                "api_response_count": sum(
                    row["response_event"] == "response"
                    for row in rows
                ),
                "reused_response_count": sum(
                    row["response_event"] == "reused"
                    for row in rows
                ),
            }
        )

        details[condition] = {
            **metrics,
            "self_reported_decision_basis_counts": (
                dict(
                    Counter(
                        row["decision_basis"]
                        for row in rows
                    )
                )
            ),
        }

    # 只分析三种实际检索器。
    # 只包含SUPPORT/CONTRADICT且有金标准文档的claim。
    # 分组结果描述关联，不构成因果证明。
    propagation = []

    for condition in (
        "retrieved_bm25",
        "retrieved_dense",
        "retrieved_hybrid",
    ):
        evidence_rows = [
            row
            for row in scored
            if (
                row["condition"] == condition
                and row["has_gold_evidence"]
            )
        ]

        for hit in (True, False):
            rows = [
                row
                for row in evidence_rows
                if row["gold_document_hit"] is hit
            ]

            count = len(rows)

            correct_count = sum(
                row["correct"]
                for row in rows
            )

            propagation.append(
                {
                    "condition": condition,
                    "gold_document_hit": hit,
                    "claim_count": count,
                    "correct_count": correct_count,
                    "error_count": (
                        count - correct_count
                    ),
                    "accuracy": (
                        correct_count / count
                        if count
                        else None
                    ),
                    "nei_prediction_count": sum(
                        row["predicted_label"]
                        == "NOT_ENOUGH_INFO"
                        for row in rows
                    ),
                }
            )

    return (
        summary,
        details,
        propagation,
    )


# ============================================================
# 5. 主程序
# ============================================================

def main():
    args = parse_arguments()

    evaluation = load_jsonl(
    args.evaluation
    )

    split_names = {
    row["split"]
    for row in evaluation
    }

    if len(split_names) != 1:
       raise ValueError(
        "评测文件混入不同数据划分。"
    )

    split = next(iter(split_names))

    if split not in ("train", "dev"):
       raise ValueError(
         f"不支持的数据划分：{split}"
    )

    journal = load_jsonl(
        args.responses
    )

    (
        scored,
        final_responses,
        api_error_count,
    ) = build_scored_rows(
        evaluation,
        journal,
    )

    (
        summary,
        details,
        propagation,
    ) = summarize(scored)

    output_dir = RESULTS_DIR / "llm"

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    prefix = args.responses.stem

    summary_path = (
        output_dir
        / f"{prefix}_summary.csv"
    )

    per_claim_path = (
        output_dir
        / f"{prefix}_per_claim.csv"
    )

    propagation_path = (
        output_dir
        / f"{prefix}_retrieval_groups.csv"
    )

    metrics_path = (
        output_dir
        / f"{prefix}_metrics.json"
    )

    csv_rows = []

    for row in scored:
        csv_row = dict(row)

        for field in (
            "validation_errors",
            "context_document_ids",
            "citations",
        ):
            csv_row[field] = json.dumps(
                row[field],
                ensure_ascii=False,
            )

        csv_rows.append(csv_row)

    actual_rows = [
        row
        for row in final_responses.values()
        if row["event"] == "response"
    ]

    reported_token_totals = {
        name: sum(
            int(
                (row.get("usage") or {})
                .get(name, 0)
            )
            for row in actual_rows
        )
        for name in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
        )
    }

    configuration = next(
        iter(final_responses.values())
    )["configuration"]

    output = {
        "generated_at_utc": (
            datetime.now(timezone.utc)
            .isoformat()
        ),
        "split": split,
        "scope": (
            "held-out dev evaluation"
             if split == "dev"
                else "train pilot evaluation"
        ),
        "response_file": str(
            args.responses.resolve()
        ),
        "evaluation_file": str(
            args.evaluation.resolve()
        ),
        "configuration": configuration,
        "label_order": list(LABELS),
        "invalid_label_policy": (
            "retain in denominator; count as incorrect; "
            "macro-F1 averages three gold classes"
        ),
        "citation_error_policy": (
            "do not discard predictions solely because "
            "citations or output fields are invalid"
        ),
        "counts": {
            "claims": len(
                {
                    row["claim_id"]
                    for row in scored
                }
            ),
            "logical_responses": len(scored),
            "actual_api_responses": len(
                actual_rows
            ),
            "reused_responses": sum(
                row["response_event"] == "reused"
                for row in scored
            ),
            "api_error_events": (
                api_error_count
            ),
        },
        "reported_token_totals": (
            reported_token_totals
        ),
        "returned_models": sorted(
            {
                row["returned_model"]
                for row in scored
                if row["returned_model"]
            }
        ),
        "summary": summary,
        "condition_details": details,
        "retrieval_groups": propagation,
        "errors": [
            row
            for row in scored
            if not row["correct"]
        ],
    }

    save_csv(
        summary,
        summary_path,
    )

    save_csv(
        csv_rows,
        per_claim_path,
    )

    save_csv(
        propagation,
        propagation_path,
    )

    save_json(
        output,
        metrics_path,
    )

    print(
        f"大模型{split}评测完成"
    )

    print(
        f"claim数：{output['counts']['claims']}"
    )

    print(
        f"逻辑回答数：{len(scored)}"
    )

    print(
        "已恢复成功的API错误事件："
        f"{api_error_count}"
    )

    for row in summary:
        print(
            f"  {row['condition']}: "
            f"{row['correct_count']}/"
            f"{row['claim_count']}，"
            f"Accuracy={row['accuracy']:.4f}，"
            f"Macro-F1={row['macro_f1']:.4f}"
        )

    print()
    print(
        "有金标准文档claim的检索分组："
    )

    for row in propagation:
        accuracy = (
            f"{row['accuracy']:.4f}"
            if row["accuracy"] is not None
            else "N/A"
        )

        print(
            f"  {row['condition']}，"
            f"gold_hit={row['gold_document_hit']}，"
            f"n={row['claim_count']}，"
            f"Accuracy={accuracy}"
        )

    print(
        "累计API返回的Token统计："
        f"{reported_token_totals}"
    )

    print(
        "该统计排除复用，"
        "失败请求的未知扣费不包含在内。"
    )

    print("结果文件：")

    for path in (
        summary_path,
        per_claim_path,
        propagation_path,
        metrics_path,
    ):
        print(f"  {path}")


if __name__ == "__main__":
    main()