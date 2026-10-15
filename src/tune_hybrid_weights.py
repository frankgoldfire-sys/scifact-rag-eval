from __future__ import annotations

import argparse
import platform
from datetime import datetime, timezone
from typing import Any

import numpy as np
from rank_bm25 import BM25Okapi
from sentence_transformers import SentenceTransformer

from run_bm25 import (
    CLAIM_PATHS,
    CORPUS_PATH,
    DEFAULT_TOP_KS,
    RESULTS_DIR,
    aggregate_metrics,
    build_document_text,
    calculate_query_metrics,
    get_label_and_gold_documents,
    load_jsonl,
    save_csv,
    save_json,
    tokenize,
)

from run_dense import (
    CACHE_DIR,
    MODEL_CACHE_DIR,
    MODEL_NAME,
    QUERY_INSTRUCTION,
    get_package_version,
    load_or_encode_corpus,
)

from run_hybrid import (
    convert_indices_to_ranks,
)


# ============================================================
# 1. 权重搜索设置
# ============================================================

# 只调整BM25权重，稠密检索权重固定为1。
BM25_WEIGHT_GRID = (
    0.0,
    0.25,
    0.50,
    0.75,
    1.0,
)

DENSE_WEIGHT = 1.0
RRF_CONSTANT = 60

BM25_K1 = 1.5
BM25_B = 0.75

DEFAULT_BATCH_SIZE = 32

# 预先规定主要选择指标，避免看完结果后
# 再随意挑选对某个方法最有利的指标。
PRIMARY_SELECTION_METRIC = "recall_at_5"
SECONDARY_SELECTION_METRIC = "ndcg_at_5"
THIRD_SELECTION_METRIC = "reciprocal_rank"


# ============================================================
# 2. 命令行参数
# ============================================================

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "在SciFact train上搜索RRF融合权重。"
        )
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="稠密模型编码批大小。",
    )

    parser.add_argument(
        "--device",
        choices=["cpu", "cuda"],
        default="cpu",
        help="稠密模型运行设备。",
    )

    arguments = parser.parse_args()

    if arguments.batch_size <= 0:
        parser.error(
            "--batch-size必须是正整数。"
        )

    return arguments


# ============================================================
# 3. 主程序
# ============================================================

def main() -> None:
    arguments = parse_arguments()

    # 权重只能在train上选择。
    split = "train"

    RESULTS_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    CACHE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    MODEL_CACHE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    print("正在读取SciFact train数据……")

    corpus_records = load_jsonl(
        CORPUS_PATH
    )

    all_claims = load_jsonl(
        CLAIM_PATHS[split]
    )

    print(
        f"语料库文档数：{len(corpus_records)}"
    )

    print(
        f"train claim总数：{len(all_claims)}"
    )

    # --------------------------------------------------------
    # 只保留有金标准证据的claim
    # --------------------------------------------------------

    evaluable_claims: list[
        dict[str, Any]
    ] = []

    gold_document_sets: list[
        set[str]
    ] = []

    for claim in all_claims:
        (
            gold_label,
            gold_document_ids,
        ) = get_label_and_gold_documents(
            claim=claim,
            split=split,
        )

        if not gold_document_ids:
            continue

        evaluable_claims.append(
            {
                "claim_id": claim["id"],
                "claim": str(claim["claim"]),
                "gold_label": gold_label,
            }
        )

        gold_document_sets.append(
            gold_document_ids
        )

    print(
        "参与权重选择的claim数："
        f"{len(evaluable_claims)}"
    )

    print(
        "排除的NEI claim数："
        f"{len(all_claims) - len(evaluable_claims)}"
    )

    # --------------------------------------------------------
    # 构建文档数据
    # --------------------------------------------------------

    document_ids: list[str] = []
    document_texts: list[str] = []
    tokenized_corpus: list[list[str]] = []

    print("正在构建文档集合……")

    for document in corpus_records:
        doc_id = str(document["doc_id"])

        document_text = build_document_text(
            document
        )

        document_ids.append(doc_id)
        document_texts.append(document_text)

        tokenized_corpus.append(
            tokenize(document_text)
        )

    if len(document_ids) != len(
        set(document_ids)
    ):
        raise ValueError(
            "语料库中存在重复doc_id。"
        )

    document_count = len(document_ids)
    claim_count = len(evaluable_claims)

    # --------------------------------------------------------
    # BM25
    # --------------------------------------------------------

    print("正在建立BM25索引……")

    bm25 = BM25Okapi(
        tokenized_corpus,
        k1=BM25_K1,
        b=BM25_B,
    )

    # --------------------------------------------------------
    # 稠密模型
    # --------------------------------------------------------

    print()
    print("正在加载稠密检索模型：")
    print(f"  {MODEL_NAME}")

    model = SentenceTransformer(
        MODEL_NAME,
        device=arguments.device,
        cache_folder=str(MODEL_CACHE_DIR),
    )

    embedding_dimension = (
        model.get_embedding_dimension()
    )

    print(
        f"运行设备：{model.device}"
    )

    print(
        f"向量维度：{embedding_dimension}"
    )

    (
        document_embeddings,
        used_corpus_cache,
    ) = load_or_encode_corpus(
        model=model,
        document_ids=document_ids,
        document_texts=document_texts,
        batch_size=arguments.batch_size,
    )

    # --------------------------------------------------------
    # 编码参与评测的claim
    # --------------------------------------------------------

    print()
    print("正在编码有金标准证据的claim……")

    instructed_query_texts = [
        QUERY_INSTRUCTION
        + claim["claim"]
        for claim in evaluable_claims
    ]

    query_embeddings = model.encode_query(
        instructed_query_texts,
        batch_size=arguments.batch_size,
        show_progress_bar=True,
        convert_to_numpy=True,
        normalize_embeddings=True,
    )

    query_embeddings = (
        query_embeddings.astype(
            np.float32,
            copy=False,
        )
    )

    print("正在计算稠密余弦相似度……")

    dense_similarity_matrix = (
        query_embeddings
        @ document_embeddings.T
    )

    # --------------------------------------------------------
    # 预先计算每条claim的两种完整排名
    # --------------------------------------------------------

    print("正在计算BM25和稠密检索排名……")

    bm25_rank_matrix = np.empty(
        (
            claim_count,
            document_count,
        ),
        dtype=np.int32,
    )

    dense_rank_matrix = np.empty(
        (
            claim_count,
            document_count,
        ),
        dtype=np.int32,
    )

    for claim_index, claim in enumerate(
        evaluable_claims
    ):
        query_tokens = tokenize(
            claim["claim"]
        )

        bm25_scores = bm25.get_scores(
            query_tokens
        )

        bm25_ranked_indices = np.argsort(
            -bm25_scores,
            kind="stable",
        )

        bm25_rank_matrix[
            claim_index
        ] = convert_indices_to_ranks(
            bm25_ranked_indices
        )

        dense_scores = dense_similarity_matrix[
            claim_index
        ]

        dense_ranked_indices = np.argsort(
            -dense_scores,
            kind="stable",
        )

        dense_rank_matrix[
            claim_index
        ] = convert_indices_to_ranks(
            dense_ranked_indices
        )

        completed = claim_index + 1

        if (
            completed % 100 == 0
            or completed == claim_count
        ):
            print(
                f"  已完成："
                f"{completed}/{claim_count}"
            )

    # --------------------------------------------------------
    # 测试不同权重
    # --------------------------------------------------------

    print()
    print("正在测试不同RRF权重……")

    candidate_results: list[
        dict[str, Any]
    ] = []

    for bm25_weight in BM25_WEIGHT_GRID:
        print()
        print(
            "BM25:Dense = "
            f"{bm25_weight:.2f}:"
            f"{DENSE_WEIGHT:.2f}"
        )

        per_claim_results: list[
            dict[str, Any]
        ] = []

        for claim_index, claim in enumerate(
            evaluable_claims
        ):
            bm25_ranks = bm25_rank_matrix[
                claim_index
            ]

            dense_ranks = dense_rank_matrix[
                claim_index
            ]

            rrf_scores = (
                bm25_weight
                / (
                    RRF_CONSTANT
                    + bm25_ranks
                )
                + DENSE_WEIGHT
                / (
                    RRF_CONSTANT
                    + dense_ranks
                )
            )

            hybrid_ranked_indices = (
                np.argsort(
                    -rrf_scores,
                    kind="stable",
                )
            )

            ranked_document_ids = [
                document_ids[int(index)]
                for index
                in hybrid_ranked_indices
            ]

            query_metrics = (
                calculate_query_metrics(
                    gold_document_ids=(
                        gold_document_sets[
                            claim_index
                        ]
                    ),
                    ranked_document_ids=(
                        ranked_document_ids
                    ),
                    top_ks=DEFAULT_TOP_KS,
                )
            )

            per_claim_row: dict[
                str,
                Any,
            ] = {
                "claim_id": (
                    claim["claim_id"]
                ),
                "gold_label": (
                    claim["gold_label"]
                ),
                "has_gold_evidence": True,
                "gold_document_count": len(
                    gold_document_sets[
                        claim_index
                    ]
                ),
            }

            per_claim_row.update(
                query_metrics
            )

            per_claim_results.append(
                per_claim_row
            )

        aggregate = aggregate_metrics(
            per_claim_results=(
                per_claim_results
            ),
            top_ks=DEFAULT_TOP_KS,
        )

        candidate_row: dict[
            str,
            Any,
        ] = {
            "bm25_weight": bm25_weight,
            "dense_weight": DENSE_WEIGHT,
            "rrf_constant": RRF_CONSTANT,
        }

        candidate_row.update(aggregate)

        candidate_results.append(
            candidate_row
        )

        print(
            "  Recall@1: "
            f"{aggregate['recall_at_1']:.4f}"
        )

        print(
            "  Recall@5: "
            f"{aggregate['recall_at_5']:.4f}"
        )

        print(
            "  Recall@10: "
            f"{aggregate['recall_at_10']:.4f}"
        )

        print(
            "  MRR: "
            f"{aggregate['reciprocal_rank']:.4f}"
        )

        print(
            "  NDCG@5: "
            f"{aggregate['ndcg_at_5']:.4f}"
        )

    # --------------------------------------------------------
    # 根据预先规定的指标选择权重
    # --------------------------------------------------------

    selected_result = max(
        candidate_results,
        key=lambda row: (
            float(
                row[
                    PRIMARY_SELECTION_METRIC
                ]
            ),
            float(
                row[
                    SECONDARY_SELECTION_METRIC
                ]
            ),
            float(
                row[
                    THIRD_SELECTION_METRIC
                ]
            ),
        ),
    )

    # --------------------------------------------------------
    # 保存结果
    # --------------------------------------------------------

    csv_path = (
        RESULTS_DIR
        / "hybrid_weight_search_train.csv"
    )

    json_path = (
        RESULTS_DIR
        / "hybrid_weight_search_train.json"
    )

    save_csv(
        candidate_results,
        csv_path,
    )

    output = {
        "experiment": (
            "hybrid_rrf_weight_search"
        ),
        "split": split,
        "generated_at_utc": (
            datetime.now(timezone.utc)
            .isoformat()
        ),
        "selection_rule": {
            "primary_metric": (
                PRIMARY_SELECTION_METRIC
            ),
            "secondary_metric": (
                SECONDARY_SELECTION_METRIC
            ),
            "third_metric": (
                THIRD_SELECTION_METRIC
            ),
            "direction": "maximize",
        },
        "search_space": {
            "bm25_weights": list(
                BM25_WEIGHT_GRID
            ),
            "dense_weight": DENSE_WEIGHT,
            "rrf_constant": RRF_CONSTANT,
        },
        "selected_configuration": (
            selected_result
        ),
        "all_candidates": (
            candidate_results
        ),
        "configuration": {
            "bm25_k1": BM25_K1,
            "bm25_b": BM25_B,
            "dense_model": MODEL_NAME,
            "document_text": (
                "title + abstract"
            ),
            "query_instruction": (
                QUERY_INSTRUCTION
            ),
            "embedding_dimension": int(
                embedding_dimension
            ),
            "batch_size": (
                arguments.batch_size
            ),
            "device": str(model.device),
            "used_corpus_cache": (
                used_corpus_cache
            ),
        },
        "counts": {
            "corpus_documents": (
                document_count
            ),
            "all_train_claims": len(
                all_claims
            ),
            "evaluated_claims": (
                claim_count
            ),
            "excluded_nei_claims": (
                len(all_claims)
                - claim_count
            ),
        },
        "environment": {
            "python": (
                platform.python_version()
            ),
            "numpy": get_package_version(
                "numpy"
            ),
            "rank_bm25": (
                get_package_version(
                    "rank-bm25"
                )
            ),
            "sentence_transformers": (
                get_package_version(
                    "sentence-transformers"
                )
            ),
            "torch": get_package_version(
                "torch"
            ),
            "transformers": (
                get_package_version(
                    "transformers"
                )
            ),
        },
    }

    save_json(
        output,
        json_path,
    )

    print()
    print("=" * 70)
    print("RRF权重搜索完成")
    print("=" * 70)

    print(
        "主要选择指标："
        f"{PRIMARY_SELECTION_METRIC}"
    )

    print(
        "次要选择指标："
        f"{SECONDARY_SELECTION_METRIC}"
    )

    print()
    print("训练集上选出的配置：")

    print(
        "  bm25_weight: "
        f"{selected_result['bm25_weight']}"
    )

    print(
        "  dense_weight: "
        f"{selected_result['dense_weight']}"
    )

    print(
        "  rrf_constant: "
        f"{selected_result['rrf_constant']}"
    )

    print(
        "  Recall@5: "
        f"{selected_result['recall_at_5']:.4f}"
    )

    print(
        "  MRR: "
        f"{selected_result['reciprocal_rank']:.4f}"
    )

    print(
        "  NDCG@5: "
        f"{selected_result['ndcg_at_5']:.4f}"
    )

    print()
    print("结果文件：")
    print(f"  {csv_path}")
    print(f"  {json_path}")


if __name__ == "__main__":
    main()