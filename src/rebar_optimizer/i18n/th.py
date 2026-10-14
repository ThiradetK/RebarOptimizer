"""Thai user-facing text: the only place issue codes become messages (D10)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from rebar_optimizer.domain.errors import Issue, IssueCode, Severity

COLUMN_LABELS: dict[str, str] = {
    "Size": "Size (ขนาด)",
    "No": "No (หมายเลข)",
    "Length": "Length (ความยาว)",
    "Quantity": "Quantity (จำนวน)",
}

SEVERITY_LABELS: dict[Severity, str] = {
    Severity.ERROR: "ข้อผิดพลาด",
    Severity.WARNING: "คำเตือน",
}

MESSAGES: dict[IssueCode, str] = {
    # reader: file level
    IssueCode.FILE_NOT_FOUND: "ไม่พบไฟล์ {path} กรุณาตรวจสอบว่าเลือกไฟล์ถูกต้อง",
    IssueCode.FILE_NOT_XLSX: (
        "ไฟล์ {path} ไม่ใช่ไฟล์ Excel แบบ .xlsx กรุณาเปิดใน Excel แล้วเลือก 'บันทึกเป็น' ชนิด .xlsx ก่อน"
    ),
    IssueCode.FILE_INVALID: (
        "เปิดไฟล์ {path} ไม่ได้ ไฟล์อาจเสียหายหรือไม่ใช่ .xlsx จริง กรุณาเปิดใน Excel แล้วบันทึกเป็น .xlsx ใหม่"
    ),
    IssueCode.FILE_LOCKED: ("เปิดไฟล์ {path} ไม่ได้ เพราะมีโปรแกรมอื่นใช้งานอยู่ กรุณาปิดไฟล์แล้วลองใหม่"),
    IssueCode.MISSING_COLUMNS: (
        "ไม่พบคอลัมน์ {columns} — แถวที่ 1 ต้องเป็นหัวตาราง "
        "Size, No, Length, Quantity (หรือ ขนาด, หมายเลข, ความยาว, จำนวน)"
    ),
    IssueCode.DUPLICATE_COLUMN: "มีคอลัมน์ {column} มากกว่า 1 คอลัมน์ กรุณาเหลือไว้คอลัมน์เดียว",
    IssueCode.NO_DATA: "ไม่พบข้อมูลในชีตแรกของไฟล์",
    # reader: row level
    IssueCode.EMPTY_VALUE: "แถว {row}: ช่อง {column} ว่าง",
    IssueCode.INVALID_TEXT: 'แถว {row}: ค่าในช่อง {column} ใช้ไม่ได้ ("{value}")',
    IssueCode.LENGTH_NOT_NUMBER: ('แถว {row}: Length (ความยาว) ต้องเป็นตัวเลขหน่วยเมตร แต่พบ "{value}"'),
    IssueCode.LENGTH_NOT_POSITIVE: "แถว {row}: Length (ความยาว) ต้องมากกว่า 0 แต่พบ {value}",
    IssueCode.LENGTH_ROUNDS_TO_ZERO: "แถว {row}: ความยาว {value} ม. สั้นกว่า 1 มม.",
    IssueCode.LENGTH_SUB_MM: (
        "แถว {row}: ความยาว {value} ม. ละเอียดกว่า 1 มม. โปรแกรมใช้ค่า {length_m} ม. แทน"
    ),
    IssueCode.QUANTITY_INVALID: (
        'แถว {row}: Quantity (จำนวน) ต้องเป็นจำนวนเต็มมากกว่า 0 แต่พบ "{value}"'
    ),
    # reader: cross-row
    IssueCode.LENGTH_MAYBE_MM: (
        "ความยาวทุกแถวมีค่าตั้งแต่ 100 ขึ้นไป หน่วยอาจเป็นมิลลิเมตร ไม่ใช่เมตร "
        "กรุณาตรวจสอบ (คอลัมน์ Length ต้องเป็นเมตร)"
    ),
    IssueCode.SIZE_AMBIGUOUS_PREFIX: (
        'พบ Size "{size}" และ {similar} ในไฟล์เดียวกัน ซึ่งอาจเป็นเหล็กขนาดเดียวกัน '
        "โปรแกรมจะคำนวณแยกกัน ถ้าเป็นขนาดเดียวกันกรุณาแก้ให้ตรงกัน"
    ),
    IssueCode.DUP_NO_DIFF_SIZE: (
        "No {no} ถูกใช้ในหลาย Size ({sizes}) ที่แถว {rows} — No ต้องไม่ซ้ำกันข้าม Size"
    ),
    IssueCode.DUP_NO_DIFF_LENGTH: (
        "No {no} (Size {size}) ซ้ำกันแต่ความยาวต่างกัน ({lengths_m} ม.) ที่แถว {rows}"
    ),
    IssueCode.DUP_NO_MERGED: (
        "No {no} (Size {size}) ซ้ำกันที่แถว {rows} และความยาวเท่ากัน จึงรวมจำนวนเป็น {quantity} ชิ้น"
    ),
    # settings
    IssueCode.STOCK_EMPTY: "กรุณากรอกความยาวเหล็กเส้น (stock) อย่างน้อย 1 ค่า",
    IssueCode.STOCK_INVALID_NUMBER: 'ความยาวเหล็กเส้น "{text}" ไม่ใช่ตัวเลข',
    IssueCode.STOCK_NOT_POSITIVE: "ความยาวเหล็กเส้นต้องมากกว่า 0 (พบ {length_m} ม.)",
    IssueCode.STOCK_DUPLICATE: "ความยาวเหล็กเส้น {length_m} ม. ซ้ำกัน",
    IssueCode.KERF_OUT_OF_RANGE: (
        "ความกว้างใบตัดต้องอยู่ระหว่าง {min_mm}–{max_mm} มม. (พบ {kerf_mm} มม.)"
    ),
    IssueCode.TIME_LIMIT_NOT_POSITIVE: "เวลาคำนวณสูงสุดต้องมากกว่า 0 วินาที (พบ {value})",
    IssueCode.MAX_PATTERNS_NOT_POSITIVE: "จำนวน pattern สูงสุดต้องมากกว่า 0 (พบ {value})",
    IssueCode.NUM_WORKERS_NOT_POSITIVE: "จำนวน thread ต้องมากกว่า 0 (พบ {value})",
    # output
    IssueCode.OUTPUT_LOCKED: ("บันทึกไฟล์ไม่ได้ เพราะไฟล์ {name} เปิดค้างอยู่ กรุณาปิดไฟล์ {name} แล้วลองใหม่"),
    IssueCode.OUTPUT_FAILED: "บันทึกไฟล์ {path} ไม่ได้: {reason}",
}


def format_metres(length_mm: int) -> str:
    """``3500`` -> ``"3.500"``."""
    return f"{length_mm / 1000:.3f}"


def _format_value(value: object) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return format(value, ".10g")
    if isinstance(value, list | tuple):
        return ", ".join(_format_value(v) for v in value)
    return str(value)


def _template_params(params: Mapping[str, object]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in params.items():
        if key == "column" and isinstance(value, str):
            result[key] = COLUMN_LABELS.get(value, value)
        elif key == "columns" and isinstance(value, list | tuple):
            result[key] = ", ".join(COLUMN_LABELS.get(str(v), str(v)) for v in value)
        else:
            result[key] = _format_value(value)
        if key.endswith("_mm"):
            metres_key = key[: -len("_mm")] + "_m"
            if isinstance(value, int) and not isinstance(value, bool):
                result[metres_key] = format_metres(value)
            elif isinstance(value, list | tuple):
                result[metres_key] = ", ".join(
                    format_metres(v) if isinstance(v, int) else str(v) for v in value
                )
    return result


def format_issue(issue: Issue) -> str:
    """Thai message for ``issue`` (no severity prefix)."""
    return MESSAGES[issue.code].format_map(_template_params(issue.params))


def format_issue_with_severity(issue: Issue) -> str:
    return f"[{SEVERITY_LABELS[issue.severity]}] {format_issue(issue)}"


def format_issues(issues: Iterable[Issue]) -> list[str]:
    """Errors first, then warnings, each with a severity prefix."""
    ordered = sorted(issues, key=lambda i: 0 if i.is_error else 1)
    return [format_issue_with_severity(i) for i in ordered]
