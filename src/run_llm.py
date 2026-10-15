from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv

from run_bm25 import (
    PROJECT_ROOT,
    RESULTS_DIR,
    load_jsonl,
)


# ============================================================
# 1. 固定输入与接口
# ============================================================

TAG = "train_pilot_per_label10_k5_seed20261013"

INPUT_PATH = (
    PROJECT_ROOT
    / "data"
    / "prepared"
    / f"{TAG}_requests.jsonl"
)

EVALUATION_PATH = (
    PROJECT_ROOT
    / "data"
    / "prepared"
    / f"{TAG}_evaluation.jsonl"
)

ENDPOINT = (
    "https://api.deepseek.com/chat/completions"
)

LABELS = (
    "SUPPORT",
    "CONTRADICT",
    "NOT_ENOUGH_INFO",
)

BASES = (
    "provided_evidence",
    "model_knowledge",
    "insufficient_information",
)


# ============================================================
# 2. 通用工具
# ============================================================

def fingerprint(data) -> str:
    text = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
    )

    return hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()


def utc_now() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="运行DeepSeek train小样本实验。"
    )

    # 默认只检查，不调用API。
    parser.add_argument(
        "--execute",
        action="store_true",
    )

    parser.add_argument(
        "--all",
        action="store_true",
        dest="use_all",
    )

    parser.add_argument(
        "--claims-per-label",
        type=int,
        default=1,
    )

    # 包括重试在内的本轮API尝试次数上限。
    parser.add_argument(
        "--max-api-calls",
        type=int,
        default=18,
    )

    parser.add_argument(
        "--max-attempts",
        type=int,
        default=2,
    )

    parser.add_argument(
        "--timeout",
        type=float,
        default=60,
    )

    parser.add_argument(
        "--max-tokens",
        type=int,
        default=1024,
    )

    parser.add_argument(
        "--seed",
        type=int,
        default=20261013,
    )

    args = parser.parse_args()

    if min(
        args.claims_per_label,
        args.max_api_calls,
        args.max_attempts,
        args.max_tokens,
    ) <= 0:
        parser.error("数量参数必须大于0。")

    if args.timeout <= 0:
        parser.error("--timeout必须大于0。")

    return args


# ============================================================
# 3. 检查模型消息结构
# ============================================================

def make_canonical_messages(messages):
    if (
        not isinstance(messages, list)
        or len(messages) != 2
    ):
        raise ValueError(
            "预期每份输入有system和user两条消息。"
        )

    result = []

    for message, role in zip(
        messages,
        ("system", "user"),
    ):
        if (
            message.get("role") != role
            or not isinstance(
                message.get("content"),
                str,
            )
        ):
            raise ValueError(
                "消息role或content不合法。"
            )

        result.append(
            {
                "role": role,
                "content": message["content"],
            }
        )

    return result


# ============================================================
# 4. 选择小规模实验输入
# ============================================================

def select_requests(
    requests,
    evaluation,
    per_label,
    use_all,
    seed,
):
    request_map = {
        row["request_id"]: row
        for row in requests
    }

    if len(request_map) != len(requests):
        raise ValueError(
            "输入中有重复request_id。"
        )

    evaluation_map = {
        row["request_id"]: row
        for row in evaluation
    }

    if (
        len(evaluation_map) != len(evaluation)
        or set(request_map) != set(evaluation_map)
    ):
        raise ValueError(
            "输入与评测文件的request_id集合不一致。"
        )

    claim_labels = {}
    claim_counts = {}

    # 标准答案仅用于每类选择相同数量的claim，
    # 不会加入发送给API的messages。
    for row in evaluation:
        cid = str(row["claim_id"])
        label = row["gold_label"]

        if label not in LABELS:
            raise ValueError(
                "出现未知金标准标签。"
            )

        if (
            cid in claim_labels
            and claim_labels[cid] != label
        ):
            raise ValueError(
                "同一claim的标签不一致。"
            )

        claim_labels[cid] = label

        claim_counts[cid] = (
            claim_counts.get(cid, 0) + 1
        )

        messages = request_map[
            row["request_id"]
        ]["messages"]

        if fingerprint(messages) != fingerprint(
            make_canonical_messages(messages)
        ):
            raise ValueError(
                "输入messages包含非预期字段。"
            )

    if any(
        count != 6
        for count in claim_counts.values()
    ):
        raise ValueError(
            "每条claim应有六份输入。"
        )

    if use_all:
        chosen = set(claim_labels)
    else:
        chosen = set()

        for label in LABELS:
            candidates = sorted(
                (
                    cid
                    for cid, value
                    in claim_labels.items()
                    if value == label
                ),
                key=int,
            )

            if len(candidates) < per_label:
                raise ValueError(
                    f"{label}没有足够的claim。"
                )

            chosen.update(
                candidates[:per_label]
            )

    selected = [
        row
        for row in requests
        if str(row["claim_id"]) in chosen
    ]

    # 固定种子打乱条件顺序。
    random.Random(seed).shuffle(selected)

    return selected


# ============================================================
# 5. 检查回答格式和引用编号
# ============================================================

def validate_answer(
    response,
    messages,
):
    errors = []
    prediction = None

    try:
        choice = response["choices"][0]

        content = choice[
            "message"
        ]["content"]

        prediction = json.loads(content)

        if not isinstance(prediction, dict):
            raise ValueError(
                "回答不是JSON对象。"
            )

        if choice.get("finish_reason") != "stop":
            errors.append(
                "回答未正常结束，可能被截断。"
            )

    except (
        KeyError,
        IndexError,
        TypeError,
        ValueError,
    ):
        return None, [
            "无法解析完整JSON回答。"
        ]

    if prediction.get("label") not in LABELS:
        errors.append(
            "label不合法。"
        )

    basis = prediction.get(
        "decision_basis"
    )

    if basis not in BASES:
        errors.append(
            "decision_basis不合法。"
        )

    explanation = prediction.get(
        "explanation"
    )

    if (
        not isinstance(explanation, str)
        or not explanation.strip()
    ):
        errors.append(
            "缺少有效explanation。"
        )

    payload = json.loads(
        messages[1]["content"]
    )

    sentence_ids = {
        str(doc["doc_id"]): {
            s["sentence_id"]
            for s in doc["sentences"]
        }
        for doc in payload[
            "candidate_abstracts"
        ]
    }

    citations = prediction.get(
        "citations"
    )

    if not isinstance(citations, list):
        errors.append(
            "citations不是列表。"
        )

        return prediction, errors

    if (
        basis == "provided_evidence"
        and not citations
    ):
        errors.append(
            "声明使用提供的证据，却没有引用。"
        )

    if (
        basis in (
            "model_knowledge",
            "insufficient_information",
        )
        and citations
    ):
        errors.append(
            "该decision_basis应对应空引用列表。"
        )

    if (
        prediction.get("label")
        == "NOT_ENOUGH_INFO"
        and basis
        != "insufficient_information"
    ):
        errors.append(
            "NEI应标记为insufficient_information。"
        )

    for citation in citations:
        if not isinstance(citation, dict):
            errors.append(
                "引用项不是JSON对象。"
            )
            continue

        doc_id = citation.get("doc_id")

        ids = citation.get(
            "sentence_ids"
        )

        if (
            not isinstance(doc_id, str)
            or doc_id not in sentence_ids
        ):
            errors.append(
                "引用了未提供的文档ID。"
            )
            continue

        if (
            not isinstance(ids, list)
            or not ids
            or any(
                type(i) is not int
                or i not in sentence_ids[doc_id]
                for i in ids
            )
        ):
            errors.append(
                "引用句编号为空、类型不合法或越界。"
            )

    return prediction, errors


# ============================================================
# 6. 保存记录和调用接口
# ============================================================

def append_record(
    path: Path,
    row,
):
    # 每次返回立即追加并落盘。
    with path.open(
        "a",
        encoding="utf-8",
    ) as file:
        file.write(
            json.dumps(
                row,
                ensure_ascii=False,
            ) + "\n"
        )

        file.flush()
        os.fsync(file.fileno())


def post_once(
    api_key,
    payload,
    timeout,
):
    request = Request(
        ENDPOINT,
        data=json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={
            "Authorization": (
                f"Bearer {api_key}"
            ),
            "Content-Type": (
                "application/json"
            ),
        },
        method="POST",
    )

    started = time.perf_counter()

    with urlopen(
        request,
        timeout=timeout,
    ) as response:
        data = json.loads(
            response.read().decode("utf-8")
        )

    return (
        data,
        time.perf_counter() - started,
    )


# ============================================================
# 7. 主程序
# ============================================================

def main():
    args = parse_arguments()

    load_dotenv(
        PROJECT_ROOT / ".env",
        encoding="utf-8-sig",
        override=False,
    )

    model = os.getenv(
        "DEEPSEEK_MODEL",
        "deepseek-flash",
    ).strip()

    if not model:
        raise ValueError(
            "DEEPSEEK_MODEL不能为空。"
        )

    requests = load_jsonl(
        INPUT_PATH
    )

    evaluation = load_jsonl(
        EVALUATION_PATH
    )

    selected = select_requests(
        requests,
        evaluation,
        args.claims_per_label,
        args.use_all,
        args.seed,
    )

    config = {
        "endpoint": ENDPOINT,
        "requested_model": model,
        "thinking": {
            "type": "disabled"
        },
        "temperature": 0,
        "max_tokens": args.max_tokens,
        "response_format": {
            "type": "json_object"
        },
        "stream": False,
        "input_sha256": fingerprint(
            requests
        ),
        "validation_version": "v1",
    }

    config_hash = fingerprint(config)

    output_dir = (
        RESULTS_DIR / "llm"
    )

    output_path = (
        output_dir
        / (
            f"{TAG}_{config_hash[:12]}"
            "_responses.jsonl"
        )
    )

    # 已返回的响应，包括格式有问题的回答，
    # 不会自动重新询问。
    # API调用失败的输入可在下一次执行时重试。
    existing = (
        load_jsonl(output_path)
        if output_path.exists()
        else []
    )

    completed = {}
    cache = {}

    for row in existing:
        if (
            row["config_hash"]
            != config_hash
        ):
            raise ValueError(
                "输出中混入不同实验配置。"
            )

        if row["event"] in (
            "response",
            "reused",
        ):
            completed[
                row["request_id"]
            ] = row

            if row["event"] == "response":
                cache[
                    row["payload_hash"]
                ] = row

    hashes = {
        row["request_id"]: fingerprint(
            {
                "config_hash": (
                    config_hash
                ),
                "messages": (
                    row["messages"]
                ),
            }
        )
        for row in selected
    }

    pending = [
        row
        for row in selected
        if row["request_id"]
        not in completed
    ]

    needed = len(
        {
            hashes[row["request_id"]]
            for row in pending
        } - set(cache)
    )

    print(
        f"请求模型：{model}"
    )

    print(
        "思考模式：disabled；temperature：0"
    )

    print(
        "选中claim数："
        f"{len({row['claim_id'] for row in selected})}"
    )

    print(
        f"逻辑输入数：{len(selected)}"
    )

    print(
        "已保存回答数："
        f"{len(selected) - len(pending)}"
    )

    print(
        f"待调用的不同输入数：{needed}"
    )

    print(
        "本轮API尝试次数上限："
        f"{args.max_api_calls}（包括重试）"
    )

    print(
        f"输出文件：{output_path}"
    )

    if not args.execute:
        print(
            "预检查结束。加--execute才会"
            "发送请求并产生费用。"
        )
        return

    api_key = os.getenv(
        "DEEPSEEK_API_KEY",
        "",
    ).strip()

    if not api_key:
        raise ValueError(
            "没有读取到DEEPSEEK_API_KEY，"
            "请检查项目根目录的.env。"
        )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    api_calls = 0
    received = 0
    reused = 0
    invalid = 0

    token_totals = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }

    for number, row in enumerate(
        selected,
        start=1,
    ):
        rid = row["request_id"]

        if rid in completed:
            print(
                f"[{number}/{len(selected)}] "
                f"已保存，跳过：{rid}"
            )
            continue

        messages = (
            make_canonical_messages(
                row["messages"]
            )
        )

        payload_hash = hashes[rid]

        metadata = {
            "request_id": rid,
            "claim_id": row["claim_id"],
            "config_hash": config_hash,
            "configuration": config,
            "payload_hash": payload_hash,
            "recorded_at_utc": utc_now(),
        }

        if payload_hash in cache:
            origin = cache[payload_hash]

            prediction, errors = (
                validate_answer(
                    origin["raw_response"],
                    messages,
                )
            )

            saved = {
                **metadata,
                "event": "reused",
                "origin_request_id": (
                    origin["request_id"]
                ),
                "prediction": prediction,
                "validation_errors": errors,
                "raw_response": (
                    origin["raw_response"]
                ),
                # 复用不产生新的Token用量。
                "usage": None,
                "latency_seconds": None,
            }

            append_record(
                output_path,
                saved,
            )

            completed[rid] = saved
            reused += 1
            invalid += bool(errors)

            print(
                f"[{number}/{len(selected)}] "
                f"复用相同输入：{rid}"
            )

            continue

        if (
            api_calls
            >= args.max_api_calls
        ):
            print(
                "达到本轮API尝试上限，"
                "剩余输入保留待运行。"
            )
            break

        # API只接收messages和生成参数，
        # 不接收评测文件中的标准答案。
        payload = {
            "model": model,
            "messages": messages,
            "thinking": {
                "type": "disabled"
            },
            "temperature": 0,
            "max_tokens": args.max_tokens,
            "response_format": {
                "type": "json_object"
            },
            "stream": False,
        }

        for attempt in range(
            1,
            args.max_attempts + 1,
        ):
            if (
                api_calls
                >= args.max_api_calls
            ):
                break

            api_calls += 1

            print(
                f"[{number}/{len(selected)}] "
                f"调用：{rid}，尝试{attempt}，"
                f"本轮API尝试{api_calls}"
            )

            http_status = None

            try:
                response, latency = (
                    post_once(
                        api_key,
                        payload,
                        args.timeout,
                    )
                )

            except HTTPError as error:
                http_status = error.code
                error.close()

                failure = (
                    f"HTTP {http_status}"
                )

            except (
                URLError,
                TimeoutError,
                OSError,
                ValueError,
            ) as error:
                failure = (
                    "网络或响应解析错误："
                    f"{type(error).__name__}"
                )

            else:
                prediction, errors = (
                    validate_answer(
                        response,
                        messages,
                    )
                )

                usage = (
                    response.get("usage")
                    or {}
                )

                saved = {
                    **metadata,
                    "event": "response",
                    "prediction": prediction,
                    "validation_errors": errors,
                    "raw_response": response,
                    "usage": usage,
                    "latency_seconds": (
                        latency
                    ),
                }

                append_record(
                    output_path,
                    saved,
                )

                completed[rid] = saved
                cache[payload_hash] = saved

                received += 1
                invalid += bool(errors)

                for name in token_totals:
                    token_totals[name] += int(
                        usage.get(name, 0)
                    )

                label = (
                    prediction.get("label")
                    if isinstance(
                        prediction,
                        dict,
                    )
                    else None
                )

                print(
                    f"  label={label}；"
                    "格式/引用检查="
                    f"{'通过' if not errors else errors}；"
                    f"耗时={latency:.2f}s"
                )

                break

            # 保存调用错误，但不输出Key或请求头。
            append_record(
                output_path,
                {
                    **metadata,
                    "event": "api_error",
                    "attempt": attempt,
                    "http_status": (
                        http_status
                    ),
                    "error": failure,
                },
            )

            print(
                f"  {failure}"
            )

            if http_status in (
                400,
                401,
                402,
                403,
                404,
                422,
            ):
                raise SystemExit(
                    "停止调用。请先检查参数、Key、"
                    "账户余额或权限。"
                )

            if (
                http_status is not None
                and http_status
                not in (408, 429)
                and http_status < 500
            ):
                break

            if (
                attempt < args.max_attempts
                and api_calls
                < args.max_api_calls
            ):
                time.sleep(
                    min(
                        2 ** attempt,
                        8,
                    )
                )

    print()

    print(
        f"本轮API尝试次数：{api_calls}"
    )

    print(
        f"本轮收到回答数：{received}"
    )

    print(
        f"本轮复用回答数：{reused}"
    )

    print(
        "本轮格式/引用检查未通过数："
        f"{invalid}"
    )

    print(
        "选中输入已保存回答数："
        f"{sum(r['request_id'] in completed for r in selected)}"
    )

    print(
        "本轮API已返回的Token统计："
        f"{token_totals}"
    )

    print(
        "失败请求可能没有返回usage；"
        "这些统计不是账户完整扣费账单。"
    )

    print(
        f"结果文件：{output_path}"
    )


if __name__ == "__main__":
    main()