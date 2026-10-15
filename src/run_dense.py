from __future__ import annotations

import argparse
import json
import platform
from collections import defaultdict
from datetime import datetime, timezone
from importlib.metadata import (
    PackageNotFoundError,
    version,
)
from pathlib import Path
from typing import Any

import numpy as np
from sentence_transformers import (
    SentenceTransformer,
)

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
)


# ============================================================
# 1. 稠密检索设置
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[1]

MODEL_NAME = "BAAI/bge-small-en-v1.5"

QUERY_INSTRUCTION = (
    "Represent this sentence for searching "
    "relevant passages: "
)

DEFAULT_BATCH_SIZE = 32
DEFAULT_RANKING_DEPTH = 100

CACHE_DIR = (
    PROJECT_ROOT
    / "cache"
    / "dense"
)

MODEL_CACHE_DIR = (
    CACHE_DIR / "models"
)

CORPUS_EMBEDDINGS_PATH = (
    CACHE_DIR
    / "bge_small_en_v1_5_corpus.npy"
)

CORPUS_CACHE_METADATA_PATH = (
    CACHE_DIR
    / "bge_small_en_v1_5_corpus_metadata.json"
)


# ============================================================
# 2. 命令行参数
# ============================================================

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "在SciFact上运行BGE稠密文档检索。"
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
            "当前阶段请使用train。"
        ),
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="文本向量编码批大小。",
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
        "--device",
        choices=[
            "cpu",
            "cuda",
        ],
        default="cpu",
        help=(
            "模型运行设备。"
            "为保证初次运行稳定，默认使用CPU。"
        ),
    )

    arguments = parser.parse_args()

    if arguments.batch_size <= 0:
        parser.error(
            "--batch-size必须是正整数。"
        )

    if arguments.ranking_depth <= 0:
        parser.error(
            "--ranking-depth必须是正整数。"
        )

    return arguments


# ============================================================
# 3. 查询包版本
# ============================================================

def get_package_version(
    package_name: str,
) -> str:
    try:
        return version(package_name)
    except PackageNotFoundError:
        return "not-installed"


# ============================================================
# 4. 保存和读取向量缓存
# ============================================================

def save_numpy_array(
    array: np.ndarray,
    path: Path,
) -> None:
    temporary_path = path.with_suffix(
        path.suffix + ".tmp"
    )

    with temporary_path.open(
        mode="wb",
    ) as file:
        np.save(
            file,
            array,
            allow_pickle=False,
        )

    temporary_path.replace(path)


def load_cache_metadata(
    path: Path,
) -> dict[str, Any] | None:
    if not path.exists():
        return None

    with path.open(
        mode="r",
        encoding="utf-8",
    ) as file:
        data = json.load(file)

    if not isinstance(data, dict):
        return None

    return data


def load_or_encode_corpus(
    model: SentenceTransformer,
    document_ids: list[str],
    document_texts: list[str],
    batch_size: int,
) -> tuple[np.ndarray, bool]:
    """
    返回：
    - 文档向量矩阵；
    - 是否使用了已有缓存。

    文档向量只需要计算一次。之后重复运行时，
    可以直接从cache目录加载。
    """
    metadata = load_cache_metadata(
        CORPUS_CACHE_METADATA_PATH
    )

    cache_is_compatible = (
        CORPUS_EMBEDDINGS_PATH.exists()
        and metadata is not None
        and metadata.get("model_name")
        == MODEL_NAME
        and metadata.get("document_ids")
        == document_ids
        and metadata.get("document_text")
        == "title + abstract"
        and metadata.get(
            "normalize_embeddings"
        )
        is True
    )

    if cache_is_compatible:
        print("发现兼容的语料库向量缓存。")
        print("正在加载缓存……")

        embeddings = np.load(
            CORPUS_EMBEDDINGS_PATH,
            allow_pickle=False,
        )

        expected_shape = (
            len(document_ids),
            model.get_embedding_dimension(),
        )

        if embeddings.shape == expected_shape:
            return (
                embeddings.astype(
                    np.float32,
                    copy=False,
                ),
                True,
            )

        print(
            "缓存维度与当前模型不一致，"
            "将重新计算。"
        )

    print("正在编码5,183篇论文摘要……")
    print(
        "第一次运行会花费一些时间，"
        "以后将直接读取缓存。"
    )

    embeddings = model.encode_document(
        document_texts,
        batch_size=batch_size,
        show_progress_bar=True,
        convert_to_numpy=True,
        normalize_embeddings=True,
    )

    embeddings = embeddings.astype(
        np.float32,
        copy=False,
    )

    CACHE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    save_numpy_array(
        array=embeddings,
        path=CORPUS_EMBEDDINGS_PATH,
    )

    cache_metadata = {
        "model_name": MODEL_NAME,
        "document_text": (
            "title + abstract"
        ),
        "normalize_embeddings": True,
        "embedding_dimension": int(
            embeddings.shape[1]
        ),
        "document_count": len(
            document_ids
        ),
        "document_ids": document_ids,
    }

    save_json(
        cache_metadata,
        CORPUS_CACHE_METADATA_PATH,
    )

    return embeddings, False


# ============================================================
# 5. 主程序
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

    document_ids: list[str] = []
    document_texts: list[str] = []

    for document in corpus_records:
        document_ids.append(
            str(document["doc_id"])
        )

        document_texts.append(
            build_document_text(
                document
            )
        )

    if len(document_ids) != len(
        set(document_ids)
    ):
        raise ValueError(
            "语料库中存在重复doc_id。"
        )

    print()
    print("正在加载稠密检索模型：")
    print(f"  {MODEL_NAME}")
    print(
        "第一次运行会从Hugging Face"
        "下载模型文件。"
    )

    model = SentenceTransformer(
        MODEL_NAME,
        device=arguments.device,
        cache_folder=str(
            MODEL_CACHE_DIR
        ),
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

    print("正在计算余弦相似度……")

    # 文档向量和查询向量都已归一化，
    # 因此点积等于余弦相似度。
    similarity_matrix = (
        query_embeddings
        @ document_embeddings.T
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

    print("正在生成检索排名……")

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

        scores = similarity_matrix[
            claim_number - 1
        ]

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
                "retrieval_method": (
                    "dense_bge_small_en_v1_5"
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
        "retrieval_method": (
            "dense_bge_small_en_v1_5"
        ),
        "split": split,
        "generated_at_utc": (
            datetime.now(timezone.utc)
            .isoformat()
        ),
        "configuration": {
            "model_name": MODEL_NAME,
            "document_text": (
                "title + abstract"
            ),
            "query_instruction": (
                QUERY_INSTRUCTION
            ),
            "similarity": (
                "cosine_similarity"
            ),
            "normalize_embeddings": True,
            "embedding_dimension": int(
                embedding_dimension
            ),
            "model_max_sequence_length": (
                model.max_seq_length
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
        / f"dense_{split}_rankings.jsonl"
    )

    per_claim_path = (
        RESULTS_DIR
        / f"dense_{split}_per_claim.csv"
    )

    metrics_path = (
        RESULTS_DIR
        / f"dense_{split}_metrics.json"
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
    print("稠密检索完成")
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