"""Validate model output; evaluate only numeric arithmetic, never model code."""

from __future__ import annotations

import ast
import json
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from itertools import islice


class AnswerError(ValueError):
    pass


def json_loads(text: str):
    def reject_constant(value):
        raise AnswerError(f"非法 JSON 数值: {value}")

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise AnswerError(f"重复 JSON 字段: {key}")
            result[key] = value
        return result

    try:
        return json.loads(text, parse_constant=reject_constant, object_pairs_hook=unique_keys)
    except (ValueError, TypeError) as exc:
        raise AnswerError(f"JSON 解析失败: {exc}") from exc


def compact_json(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise AnswerError(f"不能序列化为合法 JSON: {exc}") from exc


def check_excel_text(text: str) -> str:
    # Excel counts UTF-16 code units. Reject instead of silently truncating JSON.
    if len(text.encode("utf-16-le")) // 2 > 32767:
        raise AnswerError("答案超过 Excel 单元格 32767 字符限制，不能截断后提交")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]", text):
        raise AnswerError("答案含有 XML 不支持的控制字符")
    return text


def validate_structure(value: dict) -> dict:
    if not isinstance(value, dict):
        raise AnswerError("结构答案必须是对象")
    for key in ("row_count", "col_count"):
        if type(value.get(key)) is not int or not 1 <= value[key] <= 100000:
            raise AnswerError(f"{key} 必须是正整数")
    cells = value.get("cells")
    if not isinstance(cells, list) or not cells or len(cells) > 100000:
        raise AnswerError("cells 必须是非空列表，且最多 100000 个单元格")
    rows, cols = value["row_count"], value["col_count"]
    normalized = []
    rectangles = []
    for cell in cells:
        if not isinstance(cell, dict) or not isinstance(cell.get("text"), str):
            raise AnswerError("每个单元格必须含字符串 text")
        for key in ("row", "col", "rowspan", "colspan"):
            if type(cell.get(key)) is not int:
                raise AnswerError(f"单元格 {key} 必须是整数")
        r, c, h, w = (cell[k] for k in ("row", "col", "rowspan", "colspan"))
        if r < 0 or c < 0 or h < 1 or w < 1 or r + h > rows or c + w > cols:
            raise AnswerError(f"单元格范围越界: row={r}, col={c}, rowspan={h}, colspan={w}")
        rectangles.append((r, c, r + h, c + w))
        normalized.append({"text": cell["text"].strip(), "row": r, "col": c,
                           "rowspan": h, "colspan": w})
    # Sweep rows: do not allocate a row_count * col_count occupancy matrix.
    active = []
    for rect in sorted(rectangles):
        r, c, end_r, end_c = rect
        active = [a for a in active if a[2] > r]
        if any(c < a[3] and a[1] < end_c for a in active):
            raise AnswerError(f"单元格有重叠或重复的合并覆盖位置: row={r}, col={c}")
        active.append(rect)
    normalized.sort(key=lambda cell: (cell["row"], cell["col"]))
    # Partial structure tasks deliberately do not require complete grid coverage.
    return {"row_count": rows, "col_count": cols, "cells": normalized}


def _structure_from_grid(value, *, require_complete=False):
    """Derive spans from unique ownership; reject non-rectangular merged cells."""
    if not isinstance(value, dict):
        raise AnswerError("占用网格答案必须为对象")
    rows, cols = value.get("row_count"), value.get("col_count")
    if (type(rows) is not int or type(cols) is not int or rows < 1 or cols < 1
            or rows * cols > 100000):
        raise AnswerError("网格行列数必须为正整数，且总位置数不能超过100000")
    grid, texts = value.get("grid"), value.get("texts")
    if not isinstance(grid, list) or len(grid) != rows or not isinstance(texts, dict):
        raise AnswerError("grid 行数必须等于 row_count，texts 必须为对象")
    positions = {}
    for r, row in enumerate(grid):
        if isinstance(row, dict):
            expected = {str(c) for c in range(cols)}
            if set(row) != expected:
                raise AnswerError(f"grid 第{r}行列号不全：缺少{sorted(expected-set(row))}，多余{sorted(set(row)-expected)}")
            row = [row[str(c)] for c in range(cols)]
        if not isinstance(row, list) or len(row) != cols:
            actual = len(row) if isinstance(row, list) else type(row).__name__
            raise AnswerError(f"grid 第{r}行长度为{actual}，必须等于 col_count={cols}，不能补删数据来凑数")
        for c, owner in enumerate(row):
            if owner is None:
                if require_complete:
                    raise AnswerError(f"完整表格的网格位置({r},{c})不能为null")
                continue
            if not isinstance(owner, str) or not owner or not isinstance(texts.get(owner), str):
                raise AnswerError(f"网格位置({r},{c})的ID={owner!r}未正确引用texts。真实空白格也须独立ID和空文本；局部范围外用null")
            positions.setdefault(owner, []).append((r, c))
    if not positions or set(positions) != set(texts):
        raise AnswerError("texts 必须与 grid 使用的单元格ID严格对应")
    cells = []
    for owner, points in positions.items():
        r0, r1 = min(p[0] for p in points), max(p[0] for p in points)
        c0, c1 = min(p[1] for p in points), max(p[1] for p in points)
        if len(points) != (r1-r0+1) * (c1-c0+1):
            raise AnswerError(f"单元格ID={owner}的占用位置不是实心矩形，请重新核对合并格")
        cells.append({"text": texts[owner], "row": r0, "col": c0,
                      "rowspan": r1-r0+1, "colspan": c1-c0+1})
    return validate_structure({"row_count": rows, "col_count": cols, "cells": cells})


def structure_from_grid(value, *, require_complete=False):
    """Validate the grid and add Flash's ID/shape diagnostics for model repair."""
    try:
        return _structure_from_grid(value, require_complete=require_complete)
    except AnswerError as error:
        if isinstance(value, dict) and isinstance(value.get("texts"), dict) and isinstance(value.get("grid"), list):
            used = set()
            positions = defaultdict(list)
            for r, row in enumerate(value["grid"]):
                entries = row.items() if isinstance(row, dict) else enumerate(row) if isinstance(row, list) else []
                for column, owner in entries:
                    if isinstance(owner, str):
                        used.add(owner)
                        if str(column).isdigit():
                            positions[owner].append((r, int(column)))
            supplied = set(value["texts"])
            missing, unused = sorted(used - supplied), sorted(supplied - used)
            if missing or unused:
                raise AnswerError(
                    f"{error}；grid引用但texts缺少的ID={missing[:30]}（共{len(missing)}个）；"
                    f"texts声明但grid未使用的ID={unused[:30]}（共{len(unused)}个）。"
                    "请依据原图核对缺漏并重新输出完整JSON，确保texts键集合和grid非空ID集合完全一致。"
                ) from error
            nonrectangular = []
            for owner, occupied in positions.items():
                top = min(r for r, c in occupied)
                left = min(c for r, c in occupied)
                bottom = max(r for r, c in occupied)
                right = max(c for r, c in occupied)
                occupied_set = set(occupied)
                holes = list(islice(((r, c) for r in range(top, bottom + 1) for c in range(left, right + 1)
                                     if (r, c) not in occupied_set), 40))
                if holes:
                    nonrectangular.append({"id": owner, "occupied": occupied[:40],
                                           "bounding_rectangle": [top, left, bottom, right],
                                           "missing_positions": holes[:40]})
            if nonrectangular:
                raise AnswerError(
                    f"{error}；非矩形占用诊断（行列从0开始）={json.dumps(nonrectangular[:10],ensure_ascii=False)}。"
                    "请依据原图核对单元格归属，不能为了通过校验机械填满矩形；重新输出完整JSON。"
                ) from error
        raise


def decimal_expression(expression: str) -> Decimal:
    """Allow decimal literals, (), unary +/- and + - * / only."""
    if not isinstance(expression, str) or len(expression) > 2048:
        raise AnswerError("数值表达式必须为不超过 2048 字符的字符串")
    expression = expression.strip()
    if not expression:
        raise AnswerError("计算 expression 为空；不需要四则运算时 calculations 必须为 []，否则请填写数值表达式")
    if re.search(r"[^0-9.eE+*/()\s-]", expression):
        raise AnswerError("表达式仅允许数字、括号、+ - * /，不允许代码、名称或函数")
    try:
        tree = ast.parse(expression, mode="eval")
        if sum(1 for _ in ast.walk(tree)) > 300:
            raise AnswerError("表达式过于复杂")

        def calc(node):
            if isinstance(node, ast.Constant) and type(node.value) in (int, float):
                number = Decimal(ast.get_source_segment(expression, node))
                if not number.is_finite() or abs(number) > Decimal("1e100"):
                    raise AnswerError("表达式数值超出范围")
                return number
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                value = calc(node.operand)
                return -value if isinstance(node.op, ast.USub) else value
            if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
                left, right = calc(node.left), calc(node.right)
                if isinstance(node.op, ast.Add):
                    result = left + right
                elif isinstance(node.op, ast.Sub):
                    result = left - right
                elif isinstance(node.op, ast.Mult):
                    result = left * right
                else:
                    if right == 0:
                        raise AnswerError("表达式除以零")
                    result = left / right
                if not result.is_finite() or abs(result) > Decimal("1e100"):
                    raise AnswerError("计算结果超出范围")
                return result
            raise AnswerError("表达式包含不支持的运算")

        with localcontext() as ctx:
            ctx.prec = 128
            return calc(tree.body)
    except (SyntaxError, InvalidOperation, RecursionError, OverflowError) as exc:
        raise AnswerError(f"无效表达式: {exc}") from exc


def apply_calculations(answer, calculations):
    if not isinstance(calculations, list):
        raise AnswerError("calculations 必须是列表")
    if len(calculations) > 1000:
        raise AnswerError("calculations 过长")
    used = set()
    for item in calculations:
        if not isinstance(item, dict) or "index" not in item:
            raise AnswerError("每个计算项必须包含 index")
        index = item["index"]
        if isinstance(answer, list):
            if type(index) is not int or not 0 <= index < len(answer):
                raise AnswerError("数组计算项 index 越界")
        elif index is not None:
            raise AnswerError("单值计算项 index 必须为 null")
        if index in used:
            raise AnswerError("同一答案位置不能重复计算")
        used.add(index)
        value = decimal_expression(item.get("expression"))
        places = item.get("decimals")
        if places is not None and (type(places) is not int or not 0 <= places <= 12):
            raise AnswerError("decimals 必须是 0 到 12 的整数或 null")
        suffix = item.get("suffix", "")
        if suffix not in ("", "%"):
            raise AnswerError("计算 suffix 仅支持空字符串或 %，单位换算请写入表达式")
        with localcontext() as ctx:
            ctx.prec = 128
            if places is not None:
                value = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
                text = format(value, f".{places}f")
            else:
                text = format(value, "f")
                if "." in text:
                    text = text.rstrip("0").rstrip(".")
        if value == 0 and text.startswith("-"):
            text = text[1:]
        text += suffix
        if isinstance(answer, list):
            answer[index] = text
        else:
            answer = text
    return answer


def clean_value(value):
    if value is None:
        return ""
    if isinstance(value, bool) or isinstance(value, dict):
        raise AnswerError("提取/推理答案只能是值或数组，不能是对象或布尔值")
    if isinstance(value, list):
        return [clean_value(v) for v in value]
    if isinstance(value, (int, float)):
        # compact_json also rejects NaN and Infinity.
        compact_json(value)
        return value
    if not isinstance(value, str):
        raise AnswerError("不支持的答案类型")
    value = re.sub(r"\s+", " ", value).strip()
    # Remove thousands separators ONLY when the entire value is numeric.
    numeric = r"[+-]?(?:\d{1,3}(?:[,，]\d{3})+)(?:\.\d+)?%?"
    if re.fullmatch(numeric, value):
        value = value.replace(",", "").replace("，", "")
    return value


def validate_answer_format(text: str, question_type: str, answer_format: str = "") -> None:
    if not text or question_type == "structure":
        return
    kind = answer_format.strip().lower()
    if kind == "number":
        if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", text):
            raise AnswerError("answer_format=number 需要单个数字，不带单位或百分号")
        try:
            value = Decimal(text)
        except InvalidOperation as exc:
            raise AnswerError("answer_format=number 需要单个数字，不带单位或百分号") from exc
        if not value.is_finite():
            raise AnswerError("数字答案必须为有限数值")
    if kind in {"json_array", "json"}:
        value = json_loads(text)
        if kind == "json_array" and not isinstance(value, list):
            raise AnswerError("answer_format=json_array 需要 JSON 数组")

        def check_array_items(item):
            if isinstance(item, list):
                for child in item:
                    check_array_items(child)
            elif item is None or isinstance(item, (dict, bool)):
                raise AnswerError("数组只允许数值、文本或子数组；空值请使用空字符串")
            elif isinstance(item, (int, float)):
                compact_json(item)

        check_array_items(value)


def normalize_response(raw: str, question_type: str, answer_format: str = "") -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", raw, flags=re.S | re.I)
        if not match:
            raise AnswerError("输出含未闭合代码块")
        raw = match.group(1)
    payload = json_loads(raw)
    if not isinstance(payload, dict) or "answer" not in payload:
        raise AnswerError('模型必须返回包含 "answer" 的 JSON 对象')
    answer = payload["answer"]
    if answer is None or answer == "":
        return ""
    kind = answer_format.strip().lower()
    if question_type != "structure" and kind == "json_array" and isinstance(answer, str):
        answer = json_loads(answer)
    if question_type != "structure" and kind == "json_array" and not isinstance(answer, list):
        raise AnswerError("answer_format=json_array 需要数组 answer")
    if question_type != "structure" and kind in {"string", "number"} and isinstance(answer, list):
        raise AnswerError(f"answer_format={kind} 需要单个值")
    if question_type == "structure":
        if isinstance(answer, str):
            answer = json_loads(answer)
        text = compact_json(validate_structure(answer))
    else:
        answer = clean_value(answer)
        if question_type == "thinking":
            answer = apply_calculations(answer, payload.get("calculations", []))
        text = compact_json(answer) if isinstance(answer, list) else str(answer)
        if kind == "json":
            try:
                json_loads(text)
            except AnswerError:
                text = compact_json(answer)
    validate_answer_format(text, question_type, answer_format)
    return check_excel_text(text)
