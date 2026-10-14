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

STATUS_LABELS: dict[str, str] = {
    "OPTIMAL": "OPTIMAL (ดีที่สุดแล้ว)",
    "FEASIBLE": "FEASIBLE (ดี แต่หมดเวลาก่อนพิสูจน์ว่าดีที่สุด)",
    "HEURISTIC": "HEURISTIC (ใกล้เคียงดีที่สุด)",
    "GREEDY": "GREEDY (วิธีเร็ว)",
}

STOCK_MODE_LABELS: dict[str, str] = {
    "mixed": "ผสมความยาวได้",
    "single": "บังคับความยาวเดียว",
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
    # planning
    IssueCode.PIECES_UNPLACED: (
        "มีชิ้นงาน {count} ชิ้นที่วางไม่ได้ เพราะยาวกว่าเหล็กเส้นที่ยาวที่สุด (No {nos}) "
        "กรุณาตรวจสอบความยาวชิ้นงานหรือเพิ่มความยาวเหล็กเส้น"
    ),
    IssueCode.UNPLACED_TOO_LONG: "ยาวกว่าเหล็กเส้นที่ยาวที่สุด ({longest_stock_m} ม.)",
    IssueCode.PATTERN_LIMIT_EXCEEDED: (
        "Size {size}: รูปแบบการตัดมีมากกว่า {max_patterns} แบบ จึงใช้ผลจากวิธีเร็ว (GREEDY) ซึ่งอาจยังไม่ดีที่สุด"
    ),
    IssueCode.TIME_LIMIT_HIT: (
        "Size {size}: คำนวณครบเวลา {seconds} วินาทีก่อนพิสูจน์ได้ว่าดีที่สุด "
        "ผลที่ได้ใช้ได้ แต่ถ้าคำนวณใหม่อาจได้ผลต่างจากเดิมเล็กน้อย"
    ),
    IssueCode.WEIGHT_UNKNOWN: (
        'คำนวณน้ำหนักของ Size "{size}" ไม่ได้ เพราะไม่พบตัวเลขขนาดท้ายชื่อ (เช่น DB12) จึงเว้นช่องน้ำหนักว่างไว้'
    ),
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


# --- output workbook --------------------------------------------------------------

NONE_TEXT = "ไม่มี"
CHECK_OK = "✓"
CHECK_FAIL = "✗"
TOTAL_TEXT = "รวม"

SHEET_NAMES: dict[str, str] = {
    "plan": "แผนการตัด",
    "details": "รายละเอียดการตัด",
    "summary": "สรุปต่อ Size",
    "purchase": "รายการสั่งซื้อ",
    "remnants": "เศษคงเหลือ",
    "unplaced": "วางไม่ได้",
    "check": "ตรวจสอบจำนวน",
    "status": "ตั้งค่าและสถานะ",
    "input": "ข้อมูล Input",
}

HEADERS: dict[str, tuple[str, ...]] = {
    "plan": (
        "Size",
        "แบบที่",
        "จำนวนเส้นที่ตัดแบบนี้",
        "ความยาว stock (m)",
        "รายการชิ้น",
        "จำนวนชิ้นต่อเส้น",
        "ความยาวชิ้นรวม (m)",
        "kerf loss (m)",
        "เศษ (m)",
        "เศษนำไปใช้ได้",
        "ชิ้นยาวสุดที่ตัดจากเศษได้ (m)",
    ),
    "details": (
        "Size",
        "แบบที่",
        "stock (m)",
        "No",
        "Length (m)",
        "ชิ้นต่อเส้น",
        "จำนวนเส้น",
        "ชิ้นรวม",
    ),
    "summary": (
        "Size",
        "จำนวนเส้นแยกตาม stock",
        "จำนวนเส้นรวม",
        "stock รวม (m)",
        "ชิ้นงานรวม (m)",
        "เศษใช้ต่อได้รวม (m)",
        "Waste รวม (m)",
        "kerf รวม (m)",
        "% utilization",
        "น้ำหนักซื้อ (kg)",
        "น้ำหนักเศษใช้ต่อได้ (kg)",
        "น้ำหนัก Waste (kg)",
        "สถานะ solver",
        "lower bound (m)",
        "gap %",
    ),
    "purchase": ("Size", "ความยาว stock (m)", "จำนวนเส้น", "ความยาวรวม (m)", "น้ำหนัก (kg)"),
    "remnants": ("Size", "ความยาวเศษ (m)", "จำนวน", "ชิ้นยาวสุดที่ตัดได้ (m)"),
    "unplaced": ("Size", "No", "Length (m)", "Quantity", "เหตุผล"),
    "check": ("Size", "No", "จำนวน Input", "จำนวนที่ตัด", "จำนวนที่วางไม่ได้", "ผล"),
    "status": ("หัวข้อ", "ค่า"),
}

INPUT_ROW_HEADER = "แถวใน Excel"

STATUS_LABELS_SHEET: dict[str, str] = {
    "input_file": "ไฟล์ input",
    "created_at": "วันเวลาที่คำนวณ",
    "version": "เวอร์ชันโปรแกรม",
    "stock_lengths": "ความยาว stock (m)",
    "stock_mode": "โหมดความยาว stock",
    "kerf": "ความกว้างใบตัด (มม.)",
    "time_limit": "เวลาคำนวณสูงสุดต่อ Size (วินาที)",
    "seed": "random seed",
    "worst_status": "สถานะแย่สุดของทุก Size",
    "warnings": "คำเตือน",
}

SUBTOTAL_TEXT = "รวม {size}"

# --- CLI text ---------------------------------------------------------------------

CLI_TEXT: dict[str, str] = {
    "description": "วางแผนตัดเหล็กเส้นให้คุ้มที่สุด",
    "help_input": "ไฟล์ Excel รายการชิ้นงาน (.xlsx)",
    "help_stock": "ความยาวเหล็กเส้นที่มีขาย หน่วยเมตร คั่นด้วยจุลภาค เช่น 10,12",
    "help_kerf": "ความกว้างใบตัด (มม.) ค่าเริ่มต้น 0",
    "help_single": "บังคับใช้ความยาวเหล็กเส้นเดียวต่อ Size",
    "help_time_limit": "เวลาคำนวณสูงสุดต่อ Size (วินาที)",
    "help_template": "สร้างไฟล์ input ตัวอย่างแล้วจบ",
    "help_selftest": "แก้โจทย์ตัวอย่างในตัวเพื่อตรวจว่าโปรแกรมทำงานได้",
    "need_input": "กรุณาระบุไฟล์ input และ --stock",
    "template_written": "สร้างไฟล์ตัวอย่างแล้ว: {path}",
    "progress": "[{index}/{total}] กำลังคำนวณ Size {size} ...",
    "size_header": "Size {size} — สถานะ {status}",
    "single_stock": "  ความยาวเหล็กเส้นที่เลือก: {stock_m} ม.",
    "stock_count": "  สั่งซื้อ: {items}",
    "stock_item": "{stock_m} ม. × {count} เส้น",
    "totals": (
        "  เหล็กรวม {stock_m} ม. | ชิ้นงานรวม {pieces_m} ม. | เศษใช้ต่อได้ {reusable_m} ม. | "
        "Waste {waste_m} ม. | kerf {kerf_m} ม. | ใช้ประโยชน์ {utilization:.1f}%"
    ),
    "bound": "  lower bound {bound_m} ม. | gap {gap:.1f}%",
    "pattern": "  แบบที่ {index}: {stock_m} ม. × {count} เส้น → {cuts} | เศษ {remnant_m} ม.{tag}",
    "reusable_tag": " (ใช้ต่อได้ ตัด {longest_m} ม. ได้)",
    "unplaced_header": "ชิ้นงานที่วางไม่ได้:",
    "unplaced_item": "  Size {size} No {no} ยาว {length_m} ม. × {quantity} ชิ้น — {reason}",
    "grand_total": "รวมทุก Size: เหล็ก {bars} เส้น ยาวรวม {stock_m} ม.",
    "help_output": "ไฟล์ Excel ผลลัพธ์ (.xlsx)",
    "output_written": "บันทึกไฟล์แล้ว: {path}",
    "warnings_header": "คำเตือน:",
    "errors_header": "พบปัญหา:",
    "cancelled": "ยกเลิกการคำนวณแล้ว",
    "unexpected": "เกิดข้อผิดพลาดที่ไม่คาดคิด รายละเอียดอยู่ในไฟล์ log: {log_path}",
    "selftest_ok": "selftest ผ่าน",
    "selftest_failed": "selftest ไม่ผ่าน รายละเอียดอยู่ในไฟล์ log: {log_path}",
}


def format_cuts(cuts: Iterable[tuple[str, int, int]]) -> str:
    """``[("5", 3500, 2), ("8", 2100, 1)]`` -> ``"No.5=3.500 x 2, No.8=2.100"``."""
    parts = []
    for no, length_mm, count in cuts:
        text = f"No.{no}={format_metres(length_mm)}"
        parts.append(text if count == 1 else f"{text} x {count}")
    return ", ".join(parts)
