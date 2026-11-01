from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import time
import uuid

from .cloud import CloudError, StopRun
from .documents import Document, image_content, prepare_document, source_path
from .prompts import PROMPT_HASH, question_prompt
from .recovery import FEATURE_HINT, RECOVERY_HASH, mode_for, normalize_recovery, recovery_prompt
from .schema import AnswerError, compact_json, normalize_response
from .xlsxio import Question, id_key, validate_submission, write_submission


REPAIR_GUIDANCE = (
    "请根据原始页面和题目重新核对，保留完整答案内容，只返回完整、合法的 JSON 对象。"
    "所有字符串、数组和对象必须闭合；字符串内的英文双引号、反斜线和换行必须正确转义。"
    "answer_format=number 时，answer 必须是单个不带单位或百分号的数字，"
    "calculations 中的 suffix 必须为空字符串。"
    "计算 expression 只允许无千分位分隔符的数字、括号和 + - * /；"
    "单位换算写成数字四则运算，禁止变量、百分号、函数和代码。"
    "不需要四则运算时 calculations 必须为 []，不要添加空 expression 的计算项。"
    "结构恢复时逐格核对行列与跨度，合并格只保留左上角单元格，"
    "不要输出合并覆盖位置的占位格，不得让任何两个单元格的覆盖区域重叠。"
)
REPAIR_HASH = hashlib.sha256(REPAIR_GUIDANCE.encode()).hexdigest()


@dataclass(frozen=True)
class Config:
    model: str = "qwen3.8-flash"
    base_url: str = ""
    dpi: int = 180
    max_side: int = 2400
    max_pixels: int = 5760000
    max_pages: int = 24
    max_tokens: int = 16384
    workers: int = 2
    repair_attempts: int = 1


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
                             encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def fingerprint(question: Question, document: Document, config: Config) -> str:
    values = {"question": asdict(question), "document_sha256": document.sha256,
              "render_version": document.images[0].parent.name,
              "prompt_sha256": PROMPT_HASH, "config": asdict(config), "pipeline": 2}
    # Worker count changes throughput only, not the generated answer.
    values["config"].pop("workers")
    return hashlib.sha256(compact_json(values).encode()).hexdigest()


def previous_structure_context(question: Question, output_dir: Path, key: str) -> list[dict]:
    """Carry the previous failed response into a resumed grid-recovery request."""
    failure = output_dir / "failures" / f"{key}.json"
    if question.question_type != "structure" or not failure.is_file():
        return []
    responses = list((output_dir / "responses").glob(f"{key}-*.json"))
    if not responses:
        return []
    try:
        latest = max(responses, key=lambda path: path.stat().st_mtime_ns)
        response = json.loads(latest.read_text(encoding="utf-8"))
        previous = response["choices"][0]["message"]["content"]
        error = json.loads(failure.read_text(encoding="utf-8")).get("error", "结构校验失败")
        if isinstance(previous, str) and previous:
            return [
                {"role": "assistant", "content": previous},
                {"role": "user", "content": "上一次运行的完整输出未通过程序校验：" + error
                 + "。请继续依据原图修正并返回完整JSON；不得删减题目指定的内容。"},
            ]
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        pass
    return []


def solve(question: Question, document: Document, config: Config, client,
          output_dir: Path, refresh: bool = False, recovery_attempts: int = 1,
          recovery_first: bool = False) -> dict:
    started = time.monotonic()
    key = fingerprint(question, document, config)
    record_path = output_dir / "records" / f"{key}.json"
    if record_path.exists() and not refresh:
        try:
            cached = json.loads(record_path.read_text(encoding="utf-8"))
            if cached["fingerprint"] == key and cached["status"] == "ok" and cached.get("answer"):
                return {**cached, "cached": True}
        except (ValueError, KeyError, TypeError):
            pass
    prior_structure_context = previous_structure_context(question, output_dir, key)
    result = {"id": question.id, "question_type": question.question_type,
              "fingerprint": key, "cached": False, "answer": "", "status": "failed"}
    messages = [{"role": "user", "content": [
        {"type": "text", "text": question_prompt(question)},
        *image_content(document, config.max_pixels)]}]
    attempts = []

    def finish(answer, mode="json"):
        result.update(answer=answer, status="ok" if answer else "abstained",
                      seconds=round(time.monotonic()-started, 3), attempts=len(attempts),
                      model_returned=attempts[-1].get("model", config.model), generation_mode=mode)
        result.pop("error", None)
        if len(attempts) > 1:
            result["repair_prompt_sha256"] = REPAIR_HASH
        if mode != "json":
            result["recovery_prompt_sha256"] = RECOVERY_HASH
        atomic_json(record_path, result)
        return result

    base_attempts = 0 if recovery_first and recovery_attempts and mode_for(question) else config.repair_attempts+1
    for attempt in range(base_attempts):
        try:
            response = client.complete(messages, config.max_tokens)
            attempts.append(response)
            # Each response is written before parsing, so malformed outputs are reviewable.
            atomic_json(output_dir / "responses" / f"{key}-{uuid.uuid4().hex}.json", response)
            try:
                choice = response["choices"][0]
                raw = choice["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise CloudError("API 响应缺少 choices[0].message.content") from exc
            if choice.get("finish_reason") == "length":
                raise AnswerError("模型输出被截断，请增大 --max-tokens 或缩小题目指定的恢复范围")
            if choice.get("finish_reason") != "stop" or not isinstance(raw, str):
                raise AnswerError(f"模型未完整返回答案: finish_reason={choice.get('finish_reason')}")
            answer = normalize_response(raw, question.question_type, question.answer_format)
            if answer:
                return finish(answer)
            if attempt < config.repair_attempts:
                raise AnswerError("模型返回空答案，请重新核对所有页面、表头和相关单元格。" +
                                  (FEATURE_HINT if "是否" in question.question else "无法确定时仍可留空。"))
            if not recovery_attempts or mode_for(question) is None:
                return finish("")
            result["error"] = "模型在普通模式下返回空答案"
        except AnswerError as exc:
            result["error"] = str(exc)
            if attempt < config.repair_attempts:
                # Keep image evidence in the original message. Never repair without it.
                if attempts and isinstance(attempts[-1].get("choices"), list):
                    previous = attempts[-1]["choices"][0].get("message", {}).get("content")
                    if isinstance(previous, str):
                        messages.append({"role": "assistant", "content": previous})
                messages.append({"role": "user", "content": "上次输出未通过程序校验：" + str(exc)
                                 + "。目标 answer_format=" + question.answer_format + "。" + REPAIR_GUIDANCE})
                continue
        except (CloudError, ValueError, OSError) as exc:
            result["error"] = str(exc)
        break
    recovery_mode = mode_for(question)
    stopped = getattr(client, "stopped", None)
    if recovery_mode and recovery_attempts and not (stopped and stopped.is_set()):
        messages = [{"role": "user", "content": [
            {"type": "text", "text": recovery_prompt(question, recovery_mode, document)},
            *image_content(document, config.max_pixels)]}]
        if recovery_mode == "grid":
            messages.extend(prior_structure_context)
        for attempt in range(recovery_attempts):
            try:
                response = client.complete(messages, config.max_tokens, json_mode=recovery_mode != "text")
                attempts.append(response)
                atomic_json(output_dir / "responses" / f"{key}-{uuid.uuid4().hex}.json", response)
                choice = response["choices"][0]
                raw = choice["message"]["content"]
                if choice.get("finish_reason") != "stop" or not isinstance(raw, str):
                    raise AnswerError("替代输出模式未完整返回，不能使用截断结果")
                answer = normalize_recovery(raw, question, recovery_mode)
                if answer or attempt == recovery_attempts-1:
                    return finish(answer, recovery_mode)
                raise AnswerError("模型仍返回空答案，请重新核对图像，无法确定时仍可留空")
            except (AnswerError, CloudError, ValueError, OSError, KeyError, IndexError, TypeError) as exc:
                result["error"] = f"{recovery_mode} 恢复失败: {exc}"
                if isinstance(exc, StopRun):
                    break
                if attempt < recovery_attempts-1:
                    if attempts:
                        previous = attempts[-1].get("choices", [{}])[0].get("message", {}).get("content")
                        if isinstance(previous, str):
                            messages.append({"role": "assistant", "content": previous})
                    messages.append({"role": "user", "content": str(exc) + "。请依据原图重新生成完整答案。"})
    result["seconds"] = round(time.monotonic() - started, 3)
    result["attempts"] = len(attempts)
    # Failures deliberately do not become successful resume-cache records.
    atomic_json(output_dir / "failures" / f"{key}.json", result)
    return result


def run(questions: list[Question], files_dir: Path, output_dir: Path, config: Config,
        client, *, limit: int | None = None, refresh: bool = False, recovery_attempts: int = 1,
        recovery_first: bool = False, progress=print) -> dict:
    started = time.monotonic()
    output_dir.mkdir(parents=True, exist_ok=True)
    selected = questions if limit is None else questions[:limit]
    documents, file_errors = {}, {}
    for name in dict.fromkeys(q.file_name for q in selected):
        try:
            path = source_path(files_dir, name)
            documents[name] = prepare_document(path, output_dir / "pages", config.dpi,
                                               config.max_side, config.max_pages)
        except (ValueError, OSError, RuntimeError) as exc:
            file_errors[name] = str(exc)

    answers, results = {}, []
    # Even an interrupted --limit run keeps one row for EVERY official question.
    write_submission(output_dir / "submission.xlsx", questions, answers)
    with ThreadPoolExecutor(max_workers=config.workers) as executor:
        futures = {}
        for question in selected:
            if question.file_name in file_errors:
                result = {"id": question.id, "status": "failed", "answer": "",
                          "error": file_errors[question.file_name], "cached": False,
                          "question_type": question.question_type}
                results.append(result)
                progress(f"id={question.id}: failed ({result['error']})")
            else:
                futures[executor.submit(solve, question, documents[question.file_name],
                                        config, client, output_dir, refresh, recovery_attempts,
                                        recovery_first)] = question
        for future in as_completed(futures):
            question = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {"id": question.id, "status": "failed", "answer": "", "cached": False,
                          "question_type": question.question_type,
                          "error": f"{type(exc).__name__}: {exc}"}
            results.append(result)
            answers[id_key(question.id)] = result["answer"]
            write_submission(output_dir / "submission.xlsx", questions, answers)
            with (output_dir / "events.jsonl").open("a", encoding="utf-8") as log:
                log.write(compact_json(result) + "\n")
            extra = " (cache)" if result["cached"] else ""
            if result["status"] == "failed":
                extra += " " + result.get("error", "")
            progress(f"[{len(results)}/{len(selected)}] id={question.id}: {result['status']}{extra}")
    order = {id_key(q.id): i for i, q in enumerate(questions)}
    results.sort(key=lambda result: order[id_key(result["id"])])
    counts = Counter(result["status"] for result in results)
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "total_questions": len(questions), "selected_questions": len(selected),
        "unselected_questions": len(questions) - len(selected), "statuses": dict(counts),
        "cached_questions": sum(bool(result["cached"]) for result in results),
        "api_requests_this_run": client.request_count, "usage_this_run": client.usage,
        "seconds": round(time.monotonic() - started, 3),
        "config": asdict(config), "prompt_sha256": PROMPT_HASH,
        "repair_prompt_sha256": REPAIR_HASH,
        "recovery_prompt_sha256": RECOVERY_HASH, "recovery_attempts": recovery_attempts,
        "recovery_first": recovery_first,
        "submission_check": validate_submission(questions, output_dir / "submission.xlsx"),
        "results": results,
    }
    atomic_json(output_dir / "run_report.json", report)
    return report
