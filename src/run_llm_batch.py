from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError

from dotenv import load_dotenv

from run_bm25 import (
    PROJECT_ROOT,
    RESULTS_DIR,
    load_jsonl,
)

from run_llm import (
    ENDPOINT,
    append_record,
    fingerprint,
    make_canonical_messages,
    post_once,
    select_requests,
    utc_now,
    validate_answer,
)


# 与train试验保持一致。
MAX_TOKENS = 1024


# ============================================================
# 1. 参数
# ============================================================

def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "分批运行已准备的大模型实验输入，"
            "默认只预检查。"
        )
    )

    parser.add_argument(
        "--input",
        type=Path,
        required=True,
    )

    parser.add_argument(
        "--evaluation",
        type=Path,
        required=True,
    )

    parser.add_argument(
        "--batch-claims",
        type=int,
        default=30,
    )

    parser.add_argument(
        "--max-api-calls",
        type=int,
        default=200,
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
        "--seed",
        type=int,
        default=20261013,
    )

    parser.add_argument(
        "--execute",
        action="store_true",
    )

    args = parser.parse_args()

    if min(
        args.batch_claims,
        args.max_api_calls,
        args.max_attempts,
    ) <= 0:
        parser.error(
            "数量参数必须大于0。"
        )

    if args.timeout <= 0:
        parser.error(
            "--timeout必须大于0。"
        )

    if not args.input.name.endswith(
        "_requests.jsonl"
    ):
        parser.error(
            "--input应指向以"
            "_requests.jsonl结尾的输入文件。"
        )

    return args


# ============================================================
# 2. 选择尚未完成的claim
# ============================================================

def choose_batch(
    all_requests,
    completed,
    batch_claims,
):
    unfinished = []
    seen = set()

    for row in all_requests:
        cid = str(row["claim_id"])

        if (
            row["request_id"] not in completed
            and cid not in seen
        ):
            seen.add(cid)
            unfinished.append(cid)

    chosen = set(
        unfinished[:batch_claims]
    )

    return [
        row
        for row in all_requests
        if str(row["claim_id"]) in chosen
    ]


# ============================================================
# 3. 读取已有进度
# ============================================================

def load_progress(
    path,
    config_hash,
    request_ids,
):
    completed = {}
    cache = {}

    journal = (
        load_jsonl(path)
        if path.exists()
        else []
    )

    for row in journal:
        if row["config_hash"] != config_hash:
            raise ValueError(
                "输出文件混入不同配置。"
            )

        if row["request_id"] not in request_ids:
            raise ValueError(
                "输出文件含有当前数据集之外的请求。"
            )

        if row["event"] == "api_error":
            continue

        if row["event"] not in (
            "response",
            "reused",
        ):
            raise ValueError(
                "输出文件含有未知事件。"
            )

        if row["request_id"] in completed:
            raise ValueError(
                "同一输入存在多个最终回答，"
                "请检查日志。"
            )

        completed[
            row["request_id"]
        ] = row

        if row["event"] == "response":
            cache[
                row["payload_hash"]
            ] = row

    return completed, cache


# ============================================================
# 4. 主程序
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
        args.input
    )

    evaluation = load_jsonl(
        args.evaluation
    )

    # 先检查整个数据集，再按未完成claim分批。
    # 标准答案不会加入API消息。
    all_requests = select_requests(
        requests,
        evaluation,
        per_label=1,
        use_all=True,
        seed=args.seed,
    )

    splits = {
        row["split"]
        for row in evaluation
    }

    if len(splits) != 1:
        raise ValueError(
            "输入中混入不同split。"
        )

    split = next(iter(splits))

    request_ids = {
        row["request_id"]
        for row in all_requests
    }

    # 生成参数与train试验完全相同。
    # 批大小和调用上限不影响API内容，
    # 因此不加入配置指纹。
    config = {
        "endpoint": ENDPOINT,
        "requested_model": model,
        "thinking": {
            "type": "disabled"
        },
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
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

    tag = args.input.name.removesuffix(
        "_requests.jsonl"
    )

    output_dir = RESULTS_DIR / "llm"

    output_path = (
        output_dir
        / (
            f"{tag}_{config_hash[:12]}"
            "_responses.jsonl"
        )
    )

    completed, cache = load_progress(
        output_path,
        config_hash,
        request_ids,
    )

    selected = choose_batch(
        all_requests,
        completed,
        args.batch_claims,
    )

    hashes = {
        row["request_id"]: fingerprint(
            {
                "config_hash": config_hash,
                "messages": row["messages"],
            }
        )
        for row in selected
    }

    pending = [
        row
        for row in selected
        if row["request_id"] not in completed
    ]

    needed = len(
        {
            hashes[row["request_id"]]
            for row in pending
        } - set(cache)
    )

    print(
        f"数据划分：{split}"
    )

    print(
        f"请求模型：{model}"
    )

    print(
        "思考模式：disabled；"
        "temperature：0；max_tokens：1024"
    )

    print(
        "整个数据集claim数："
        f"{len({row['claim_id'] for row in all_requests})}"
    )

    print(
        f"全部逻辑输入数：{len(all_requests)}"
    )

    print(
        f"累计已保存回答数：{len(completed)}"
    )

    print(
        "本批claim数："
        f"{len({row['claim_id'] for row in selected})}"
    )

    print(
        f"本批待完成逻辑输入数：{len(pending)}"
    )

    print(
        f"本批待调用的不同输入数：{needed}"
    )

    print(
        "本轮API尝试上限："
        f"{args.max_api_calls}（包括重试）"
    )

    print(
        f"输出文件：{output_path}"
    )

    if not args.execute:
        print(
            "预检查结束，没有调用API。"
            "加--execute才会执行。"
        )
        return

    if not pending:
        print(
            "整个数据集已经完成，无需再次调用。"
        )
        return

    api_key = os.getenv(
        "DEEPSEEK_API_KEY",
        "",
    ).strip()

    if not api_key:
        raise ValueError(
            "没有读取到DEEPSEEK_API_KEY。"
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
        pending,
        start=1,
    ):
        rid = row["request_id"]

        messages = make_canonical_messages(
            row["messages"]
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

        # 相同输入复用已经保存的回答。
        if payload_hash in cache:
            origin = cache[payload_hash]

            prediction, errors = validate_answer(
                origin["raw_response"],
                messages,
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
                f"[{number}/{len(pending)}] "
                f"复用：{rid}"
            )

            continue

        if api_calls >= args.max_api_calls:
            print(
                "达到本轮API尝试上限，"
                "剩余输入下次继续。"
            )
            break

        payload = {
            "model": model,
            "messages": messages,
            "thinking": {
                "type": "disabled"
            },
            "temperature": 0,
            "max_tokens": MAX_TOKENS,
            "response_format": {
                "type": "json_object"
            },
            "stream": False,
        }

        for attempt in range(
            1,
            args.max_attempts + 1,
        ):
            if api_calls >= args.max_api_calls:
                break

            api_calls += 1

            print(
                f"[{number}/{len(pending)}] "
                f"调用：{rid}，尝试{attempt}，"
                f"本轮API尝试{api_calls}"
            )

            http_status = None

            try:
                response, latency = post_once(
                    api_key,
                    payload,
                    args.timeout,
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
                prediction, errors = validate_answer(
                    response,
                    messages,
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
                    "latency_seconds": latency,
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
                    if isinstance(prediction, dict)
                    else None
                )

                print(
                    f"  label={label}；"
                    f"检查={'通过' if not errors else errors}；"
                    f"耗时={latency:.2f}s"
                )

                break

            append_record(
                output_path,
                {
                    **metadata,
                    "event": "api_error",
                    "attempt": attempt,
                    "http_status": http_status,
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
                    "停止调用，请先检查参数、"
                    "Key、余额或权限。"
                )

            if (
                http_status is not None
                and http_status not in (408, 429)
                and http_status < 500
            ):
                break

            if (
                attempt < args.max_attempts
                and api_calls < args.max_api_calls
            ):
                time.sleep(
                    min(
                        2 ** attempt,
                        8,
                    )
                )

    unfinished_claims = {
        str(row["claim_id"])
        for row in all_requests
        if row["request_id"] not in completed
    }

    total_claim_count = len(
        {
            str(row["claim_id"])
            for row in all_requests
        }
    )

    print()
    print("本批运行结束")

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
        "累计已保存回答数："
        f"{len(completed)}/{len(all_requests)}"
    )

    print(
        "全部六种条件均已完成的claim数："
        f"{total_claim_count - len(unfinished_claims)}"
    )

    print(
        "本轮API返回的Token统计："
        f"{token_totals}"
    )

    print(
        "失败请求的未知扣费"
        "不包含在Token统计内。"
    )

    print(
        f"结果文件：{output_path}"
    )

    if unfinished_claims:
        print(
            "再次执行相同命令，"
            "会继续处理未完成claim。"
        )


if __name__ == "__main__":
    main()