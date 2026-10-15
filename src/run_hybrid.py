from __future__ import annotations

import argparse
import platform
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
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
    save_jsonl,
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


# ============================================================
# 1. 默认参数
# ============================================================

DEFAULT_BATCH_SIZE = 32
DEFAULT_RANKING_DEPTH = 100

DEFAULT_K1 = 1.5
DEFAULT_B = 0.75

# 注意：这是RRF平滑常数，
# 与Recall@K中的K不是同一个概念。
DEFAULT_RRF_CONSTANT = 60

DEFAULT_BM25_WEIGHT = 1.0
DEFAULT_DENSE_WEIGHT = 1.0


# ============================================================
# 2. 命令行参数
# ============================================================

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "使用RRF融合BM25与BGE稠密检索结果。"
        )
    )

    parser.add_argument(
        "--split",
        choices=["train", "dev", "test"],
        default="train",
        help="需要检索的数据划分。",
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="稠密模型的文本编码批大小。",
    )

    parser.add_argument(
        "--ranking-depth",
        type=int,
        default=DEFAULT_RANKING_DEPTH,
        help="为每条claim保存多少篇检索结果。",
    )

    parser.add_argument(
        "--device",
        choices=["cpu", "cuda"],
        default="cpu",
        help="稠密模型运行设备。",
    )

    parser.add_argument(
        "--rrf-constant",
        type=int,
        default=DEFAULT_RRF_CONSTANT,
        help="RRF公式中的平滑常数。",
    )

    parser.add_argument(
        "--bm25-weight",
        type=float,
        default=DEFAULT_BM25_WEIGHT,
        help="BM25排名在RRF中的权重。",
    )

    parser.add_argument(
        "--dense-weight",
        type=float,
        default=DEFAULT_DENSE_WEIGHT,
        help="稠密排名在RRF中的权重。",
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

    if arguments.batch_size <= 0:
        parser.error("--batch-size必须是正整数。")

    if arguments.ranking_depth <= 0:
        parser.error("--ranking-depth必须是正整数。")

    if arguments.rrf_constant < 0:
        parser.error("--rrf-constant不能小于0。")

    if arguments.bm25_weight < 0:
        parser.error("--bm25-weight不能小于0。")

    if arguments.dense_weight < 0:
        parser.error("--dense-weight不能小于0。")

    if (
        arguments.bm25_weight == 0
        and arguments.dense_weight == 0
    ):
        parser.error(
            "BM25权重和稠密检索权重不能同时为0。"
        )

    if arguments.k1 < 0:
        parser.error("--k1不能小于0。")

    if not 0 <= arguments.b <= 1:
        parser.error("--b必须位于0到1之间。")

    return arguments


# ============================================================
# 3. 将排序结果转换为名次数组
# ============================================================

def convert_indices_to_ranks(
    ranked_indices: np.ndarray,
) -> np.ndarray:
    """
    输入：
        按相关程度从高到低排列的文档下标。

    输出：
        每篇文档对应的排名，排名从1开始。

    例如：
        ranked_indices = [2, 0, 1]
        返回 ranks = [2, 3, 1]
    """
    ranks = np.empty(
        len(ranked_indices),
        dtype=np.int32,
    )

    ranks[ranked_indices] = np.arange(
        1,
        len(ranked_indices) + 1,
        dtype=np.int32,
    )

    return ranks


# ============================================================
# 4. 主程序
# ============================================================

def main() -> None:
    arguments = parse_arguments()
    split = arguments.split

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

    print("正在读取SciFact语料库……")

    corpus_records = load_jsonl(
        CORPUS_PATH
    )

    claims = load_jsonl(
        CLAIM_PATHS[split]
    )

    document_count = len(corpus_records)

    if arguments.ranking_depth > document_count:
        raise ValueError(
            "--ranking-depth不能超过"
            f"语料库文档数{document_count}。"
        )

    print(
        f"语料库文档数：{document_count}"
    )

    print(
        f"{split} claim数：{len(claims)}"
    )

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

    # --------------------------------------------------------
    # 建立BM25索引
    # --------------------------------------------------------

    print("正在建立BM25索引……")

    bm25 = BM25Okapi(
        tokenized_corpus,
        k1=arguments.k1,
        b=arguments.b,
    )

    # --------------------------------------------------------
    # 加载稠密模型与文档向量
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
        "模型最大输入长度："
        f"{model.max_seq_length}"
    )

    print(
        "向量维度："
        f"{embedding_dimension}"
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
    # 编码全部claim
    # --------------------------------------------------------

    print()
    print("正在编码claim……")

    original_claim_texts = [
        str(claim["claim"])
        for claim in claims
    ]

    instructed_query_texts = [
        QUERY_INSTRUCTION + claim_text
        for claim_text in original_claim_texts
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

    # 查询向量和文档向量均已归一化，
    # 所以点积等于余弦相似度。
    dense_similarity_matrix = (
        query_embeddings
        @ document_embeddings.T
    )

    # --------------------------------------------------------
    # 对每条claim进行RRF融合
    # --------------------------------------------------------

    rankings_output: list[
        dict[str, Any]
    ] = []

    per_claim_output: list[
        dict[str, Any]
    ] = []

    label_counts: dict[str, int] = (
        defaultdict(int)
    )

    print("正在融合BM25与稠密检索排名……")

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

        # ----------------------------------------------------
        # BM25完整排名
        # ----------------------------------------------------

        query_tokens = tokenize(
            claim_text
        )

        bm25_scores = bm25.get_scores(
            query_tokens
        )

        bm25_ranked_indices = np.argsort(
            -bm25_scores,
            kind="stable",
        )

        bm25_ranks = convert_indices_to_ranks(
            bm25_ranked_indices
        )

        # ----------------------------------------------------
        # 稠密检索完整排名
        # ----------------------------------------------------

        dense_scores = dense_similarity_matrix[
            claim_number - 1
        ]

        dense_ranked_indices = np.argsort(
            -dense_scores,
            kind="stable",
        )

        dense_ranks = convert_indices_to_ranks(
            dense_ranked_indices
        )

        # ----------------------------------------------------
        # Reciprocal Rank Fusion
        # ----------------------------------------------------

        rrf_scores = (
            arguments.bm25_weight
            / (
                arguments.rrf_constant
                + bm25_ranks
            )
            + arguments.dense_weight
            / (
                arguments.rrf_constant
                + dense_ranks
            )
        )

        hybrid_ranked_indices = np.argsort(
            -rrf_scores,
            kind="stable",
        )

        complete_ranked_document_ids = [
            document_ids[int(index)]
            for index in hybrid_ranked_indices
        ]

        top_indices = hybrid_ranked_indices[
            :arguments.ranking_depth
        ]

        retrieved_documents = [
            {
                "rank": rank,
                "doc_id": (
                    document_ids[int(index)]
                ),
                "rrf_score": float(
                    rrf_scores[int(index)]
                ),
                "bm25_rank": int(
                    bm25_ranks[int(index)]
                ),
                "dense_rank": int(
                    dense_ranks[int(index)]
                ),
                "bm25_score": float(
                    bm25_scores[int(index)]
                ),
                "dense_score": float(
                    dense_scores[int(index)]
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
                "retrieval_method": (
                    "hybrid_rrf_bm25_dense"
                ),
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
            "gold_document_count": len(
                gold_document_ids
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

    # --------------------------------------------------------
    # 汇总结果
    # --------------------------------------------------------

    aggregate = aggregate_metrics(
        per_claim_results=per_claim_output,
        top_ks=DEFAULT_TOP_KS,
    )

    evaluated_claim_count = sum(
        row["has_gold_evidence"]
        for row in per_claim_output
    )

    metrics_output = {
        "retrieval_method": (
            "hybrid_rrf_bm25_dense"
        ),
        "split": split,
        "generated_at_utc": (
            datetime.now(timezone.utc)
            .isoformat()
        ),
        "configuration": {
            "fusion_method": (
                "reciprocal_rank_fusion"
            ),
            "rrf_constant": (
                arguments.rrf_constant
            ),
            "bm25_weight": (
                arguments.bm25_weight
            ),
            "dense_weight": (
                arguments.dense_weight
            ),
            "bm25_k1": arguments.k1,
            "bm25_b": arguments.b,
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
            "top_ks": list(
                DEFAULT_TOP_KS
            ),
            "saved_ranking_depth": (
                arguments.ranking_depth
            ),
            "used_corpus_cache": (
                used_corpus_cache
            ),
        },
        "environment": {
            "python": (
                platform.python_version()
            ),
            "numpy": get_package_version(
                "numpy"
            ),
            "rank_bm25": get_package_version(
                "rank-bm25"
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
        "counts": {
            "corpus_documents": (
                document_count
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
        / f"hybrid_{split}_rankings.jsonl"
    )

    per_claim_path = (
        RESULTS_DIR
        / f"hybrid_{split}_per_claim.csv"
    )

    metrics_path = (
        RESULTS_DIR
        / f"hybrid_{split}_metrics.json"
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
    print("混合检索完成")
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
    print("RRF配置：")

    print(
        "  rrf_constant: "
        f"{arguments.rrf_constant}"
    )

    print(
        "  bm25_weight: "
        f"{arguments.bm25_weight}"
    )

    print(
        "  dense_weight: "
        f"{arguments.dense_weight}"
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