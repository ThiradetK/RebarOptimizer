"""Create a sample input workbook."""

from __future__ import annotations

from pathlib import Path

import openpyxl
from openpyxl.styles import Font

from rebar_optimizer.domain.errors import Issue, IssueCode, OutputWriteError

TEMPLATE_HEADERS = ("Size", "No", "Length", "Quantity")

SAMPLE_ROWS: tuple[tuple[str, str, float, int], ...] = (
    ("DB12", "1", 3.5, 12),
    ("DB12", "2", 2.1, 20),
    ("DB12", "3", 5.75, 8),
    ("DB12", "4", 1.2, 30),
    ("DB16", "5", 6.3, 10),
    ("DB16", "6", 4.2, 14),
    ("DB16", "7", 2.85, 16),
    ("RB9", "8", 1.1, 40),
    ("RB9", "9", 0.85, 60),
)

INSTRUCTIONS: tuple[str, ...] = (
    "วิธีกรอกข้อมูล",
    "1. กรอกรายการชิ้นงานในชีตแรก โดยแถวที่ 1 เป็นหัวตาราง",
    "2. Size = ขนาดเหล็ก เช่น DB12, RB9",
    "3. No = หมายเลขชิ้นงาน ห้ามซ้ำกันข้าม Size",
    "4. Length = ความยาวชิ้นงาน หน่วยเมตร เช่น 3.5",
    "5. Quantity = จำนวนชิ้น เป็นจำนวนเต็ม",
    "6. ใช้หัวตารางภาษาไทยได้: ขนาด, หมายเลข, ความยาว, จำนวน",
    "7. คอลัมน์อื่นที่เพิ่มมา (เช่น หมายเหตุ) จะถูกคัดลอกไปยังผลลัพธ์ แต่ไม่ใช้คำนวณ",
)


def write_template(path: str | Path) -> Path:
    """Write the sample workbook to ``path`` and return the path."""
    path = Path(path)
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "รายการชิ้นงาน"
    sheet.append(TEMPLATE_HEADERS)
    for row in SAMPLE_ROWS:
        sheet.append(row)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for row_cells in sheet.iter_rows(min_row=2, min_col=3, max_col=3):
        for cell in row_cells:
            cell.number_format = "0.000"
    for column, width in zip("ABCD", (10, 10, 12, 12), strict=True):
        sheet.column_dimensions[column].width = width
    sheet.freeze_panes = "A2"

    notes = workbook.create_sheet("คำอธิบาย")
    for line in INSTRUCTIONS:
        notes.append((line,))
    notes["A1"].font = Font(bold=True)
    notes.column_dimensions["A"].width = 80

    try:
        workbook.save(path)
    except PermissionError as exc:
        raise OutputWriteError(
            [Issue.error(IssueCode.OUTPUT_LOCKED, path=str(path), name=path.name)]
        ) from exc
    except OSError as exc:
        raise OutputWriteError(
            [Issue.error(IssueCode.OUTPUT_FAILED, path=str(path), reason=str(exc))]
        ) from exc
    return path
