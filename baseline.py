#!/usr/bin/env python3
"""CLI for the Tianchi table-QA baseline using Alibaba Cloud Qwen APIs."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import fields
import json
import os
from pathlib import Path
import shutil
import sys

from tableqa.cloud import QwenClient
from tableqa.documents import document_info, source_path
from tableqa.pipeline import Config, run
from tableqa.schema import json_loads
from tableqa.xlsxio import read_questions, validate_submission


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("必须大于 0")
    return number


def nonnegative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("必须不小于 0")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description="天池 532510 Qwen3.8-Flash baseline")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "run", "validate"):
        command = sub.add_parser(name)
        command.add_argument("--tests", type=Path, default=Path("data/tests.xlsx"), help="题目 Excel")
        command.add_argument("--sheet", help="题目所在工作表；默认第一个工作表")
        command.add_argument("--dataset-fixes", type=Path, default=Path("configs/downloaded_data_fixes.json"),
                             help="独立的文件名/题型修正配置，不改写原题目表")
        if name in ("inspect", "run"):
            command.add_argument("--files", type=Path, default=Path("data/files"), help="files 目录")
        if name == "validate":
            command.add_argument("--submission", type=Path, default=Path("submission.xlsx"))
    command = sub.choices["run"]
    command.add_argument("--output-dir", type=Path, default=Path("runs/qwen3_8_flash"))
    command.add_argument("--base-url", default=os.getenv("DASHSCOPE_BASE_URL", ""),
                         help="百炼 compatible-mode/v1 地址，或环境变量 DASHSCOPE_BASE_URL")
    command.set_defaults(model="qwen3.8-flash")
    command.add_argument("--workers", type=positive, default=2)
    command.add_argument("--dpi", type=positive, default=180)
    command.add_argument("--max-side", type=positive, default=2400)
    command.add_argument("--max-pixels", type=positive, default=5760000)
    command.add_argument("--max-pages", type=positive, default=24)
    command.add_argument("--max-tokens", type=positive, default=16384)
    command.add_argument("--repair-attempts", type=nonnegative, default=1)
    command.add_argument("--recovery-attempts", type=nonnegative, default=1,
                         help="格式修复或弃答后，改用纯文本/占用网格模式的额外请求数，最多3次")
    command.add_argument("--recovery-first", action="store_true",
                         help="复用非空成功答案，对未答题直接使用纯文本/占用网格恢复模式")
    command.add_argument("--api-retries", type=nonnegative, default=3)
    command.add_argument("--timeout", type=positive, default=180)
    command.add_argument("--max-requests", type=positive, default=2000,
                         help="本次进程实际 HTTP 调用上限，含错误重试与格式修复")
    command.add_argument("--limit", type=positive, help="只处理前 N 题，其他题仍保留空答案行")
    command.add_argument("--refresh", action="store_true", help="忽略已成功的答案缓存")
    args = parser.parse_args(argv)
    try:
        fixes = json_loads(args.dataset_fixes.read_text(encoding="utf-8")) if args.dataset_fixes else None
        questions = read_questions(args.tests, args.sheet, fixes)
        if args.command == "inspect":
            docs = []
            for name in dict.fromkeys(q.file_name for q in questions):
                path = source_path(args.files, name)
                docs.append({"file_name": name, **document_info(path)})
            result = {"question_count": len(questions), "types": dict(Counter(q.question_type for q in questions)),
                      "documents": docs, "api_calls": 0}
        elif args.command == "validate":
            result = validate_submission(questions, args.submission)
        else:
            if args.tests.resolve() == (args.output_dir / "submission.xlsx").resolve():
                raise ValueError("输出文件不能覆盖题目 Excel")
            if args.recovery_attempts > 3:
                raise ValueError("recovery-attempts 上限3")
            if args.workers > 32 or args.repair_attempts > 5 or args.max_pixels > 16777216:
                raise ValueError("workers 上限32，repair-attempts 上限5，max-pixels 上限16777216")
            config = Config(**{field.name: getattr(args, field.name) for field in fields(Config)})
            client = QwenClient(os.getenv("DASHSCOPE_API_KEY", ""), args.base_url, args.model,
                                timeout=args.timeout, retries=args.api_retries,
                                max_requests=args.max_requests)
            report = run(questions, args.files, args.output_dir, config, client,
                         limit=args.limit, refresh=args.refresh, recovery_attempts=args.recovery_attempts,
                         recovery_first=args.recovery_first)
            result = {k: report[k] for k in ("total_questions", "selected_questions", "statuses",
                                            "api_requests_this_run", "usage_this_run", "submission_check")}
            result["output"] = str((args.output_dir / "submission.xlsx").resolve())
            if (report["selected_questions"] == report["total_questions"]
                    and report["submission_check"]["nonempty"] == report["total_questions"]
                    and not report["statuses"].get("failed")):
                generated = args.output_dir / "submission.xlsx"
                published = Path("submission.xlsx")
                if generated.resolve() != published.resolve():
                    shutil.copy2(generated, published)
                result["submission"] = str(published.resolve())
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 2 if report["statuses"].get("failed") else 0
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
