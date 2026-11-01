"""Read input workbooks; write the two-column submission as literal OpenXML."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile
import os
import tempfile

from openpyxl import load_workbook

from .schema import (AnswerError, check_excel_text, json_loads, validate_answer_format,
                     validate_structure)


@dataclass(frozen=True)
class Question:
    id: str | int
    file_name: str
    question_type: str
    question: str
    table_hint: str = ""
    answer_format: str = ""


def id_key(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or str(value) == "":
        raise ValueError(f"id 必须是非空文本或整数，实际为 {type(value).__name__}")
    return str(value)


def read_rows(path: Path, required: set[str], sheet: str | None = None) -> list[dict]:
    workbook = load_workbook(path, read_only=True, data_only=False)
    try:
        worksheet = workbook[sheet] if sheet else workbook.worksheets[0]
        rows = worksheet.iter_rows()
        header = next(rows, None)
        if not header:
            raise ValueError("Excel 为空")
        names = [str(c.value).strip() if c.value is not None else "" for c in header]
        nonempty_names = [n for n in names if n]
        if len(set(nonempty_names)) != len(nonempty_names):
            raise ValueError("Excel 有重复字段名")
        if not required.issubset(names):
            raise ValueError(f"Excel 缺少字段: {sorted(required - set(names))}")
        result, seen = [], set()
        for number, row in enumerate(rows, 2):
            if all(c.value is None for c in row):
                continue
            for name, cell in zip(names, row):
                if name in required and cell.data_type in ("f", "e"):
                    raise ValueError(f"第 {number} 行 {name} 必须是值，不能是公式或错误值")
            item = {name: cell.value for name, cell in zip(names, row) if name}
            key = id_key(item.get("id"))
            if key in seen:
                raise ValueError(f"重复 id: {key}")
            seen.add(key)
            result.append(item)
        if not result:
            raise ValueError("Excel 没有数据行")
        return result
    finally:
        workbook.close()


def read_questions(path: Path, sheet: str | None = None, fixes: dict | None = None) -> list[Question]:
    fixes = {} if fixes is None else fixes
    if not isinstance(fixes, dict) or set(fixes) - {"schema_version", "notes", "file_aliases", "question_overrides"}:
        raise ValueError("dataset-fixes 必须是包含 file_aliases/question_overrides 的对象")
    if fixes.get("schema_version", 1) != 1:
        raise ValueError("不支持的 dataset-fixes schema_version")
    aliases, overrides = fixes.get("file_aliases", {}), fixes.get("question_overrides", {})
    if not isinstance(aliases, dict) or not isinstance(overrides, dict):
        raise ValueError("file_aliases 与 question_overrides 必须是对象")
    if not all(isinstance(k, str) and isinstance(v, str) and v for k, v in aliases.items()):
        raise ValueError("file_aliases 只能映射文件名字符串")
    required = {"id", "file_name", "question_type", "question"}
    result, seen = [], set()
    for row in read_rows(path, required, sheet):
        key = id_key(row["id"])
        seen.add(key)
        if key in overrides:
            correction = overrides[key]
            if not isinstance(correction, dict) or set(correction) - {"question_type", "answer_format", "expected_question"}:
                raise ValueError(f"id={key} 的修正只允许题型和答案格式元数据")
            if correction.get("expected_question", row["question"]) != row["question"]:
                raise ValueError(f"id={key} 的题目与修正配置不匹配，请检查数据版本")
            row.update({k: v for k, v in correction.items() if k != "expected_question"})
        for field in ("file_name", "question_type", "question"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise ValueError(f"id={row['id']} 的 {field} 必须是非空文本")
        question_type = row["question_type"].strip().lower()
        if question_type not in {"structure", "extract", "thinking"}:
            raise ValueError(f"id={row['id']} 有未知题型: {question_type}")
        name = row["file_name"].strip()
        result.append(Question(row["id"], aliases.get(name, name), question_type,
                               row["question"].strip(), str(row.get("table_hint") or "").strip(),
                               str(row.get("answer_format") or "").strip()))
    if set(overrides) - seen:
        raise ValueError(f"修正配置引用了不存在的 id: {sorted(set(overrides) - seen)}")
    return result


NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
ET.register_namespace("", NS)


def write_submission(path: Path, questions: list[Question], answers: dict[str, str]) -> None:
    """No formula interpretation or automatic truncation. Original ID types survive."""
    tag = lambda name: f"{{{NS}}}{name}"
    worksheet = ET.Element(tag("worksheet"))
    ET.SubElement(worksheet, tag("dimension"), ref=f"A1:B{len(questions) + 1}")
    views = ET.SubElement(worksheet, tag("sheetViews"))
    view = ET.SubElement(views, tag("sheetView"), workbookViewId="0", showGridLines="0")
    ET.SubElement(view, tag("pane"), ySplit="1", topLeftCell="A2", activePane="bottomLeft", state="frozen")
    cols = ET.SubElement(worksheet, tag("cols"))
    ET.SubElement(cols, tag("col"), min="1", max="1", width="20", customWidth="1")
    ET.SubElement(cols, tag("col"), min="2", max="2", width="100", customWidth="1")
    data = ET.SubElement(worksheet, tag("sheetData"))

    def add_row(number, values):
        row = ET.SubElement(data, tag("row"), r=str(number))
        for col, value in zip(("A", "B"), values):
            if type(value) is int:
                cell = ET.SubElement(row, tag("c"), r=f"{col}{number}", t="n")
                ET.SubElement(cell, tag("v")).text = str(value)
            else:
                text = check_excel_text(str(value))
                cell = ET.SubElement(row, tag("c"), r=f"{col}{number}", t="inlineStr")
                string = ET.SubElement(cell, tag("is"))
                ET.SubElement(string, tag("t"), {"{http://www.w3.org/XML/1998/namespace}space": "preserve"}).text = text

    add_row(1, ("id", "answer"))
    for number, question in enumerate(questions, 2):
        add_row(number, (question.id, answers.get(id_key(question.id), "")))
    members = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        "_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="submission" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": ET.tostring(worksheet, encoding="utf-8", xml_declaration=True),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".submission-", suffix=".xlsx", dir=path.parent)
    os.close(fd)
    try:
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as archive:
            for name, content in members.items():
                archive.writestr(name, content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_submission(questions: list[Question], path: Path) -> dict:
    rows = read_rows(path, {"id", "answer"})
    expected = {id_key(q.id): q for q in questions}
    submitted = {id_key(row["id"]): row for row in rows}
    if set(expected) != set(submitted):
        raise ValueError(f"id 集合不一致: 缺失={sorted(set(expected) - set(submitted))[:10]}, "
                         f"多余={sorted(set(submitted) - set(expected))[:10]}")
    blanks = 0
    for key, question in expected.items():
        row = submitted[key]
        if type(row["id"]) is not type(question.id):
            raise ValueError(f"id={key} 的类型发生变化")
        answer = "" if row["answer"] is None else str(row["answer"])
        check_excel_text(answer)
        if not answer.strip():
            blanks += 1
            continue
        if question.question_type == "structure":
            validate_structure(json_loads(answer))
        elif question.answer_format.lower() not in {"string", "number", "json", "json_array"} and answer.strip().startswith(("[", "{")):
            if not isinstance(json_loads(answer), list):
                raise AnswerError(f"id={key} 的多值答案必须是 JSON 数组")
        validate_answer_format(answer, question.question_type, question.answer_format)
    return {"rows": len(rows), "nonempty": len(rows) - blanks, "blank": blanks,
            "format_valid": True}
