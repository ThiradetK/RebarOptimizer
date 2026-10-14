from __future__ import annotations

from rebar_optimizer.domain.errors import Issue, IssueCode
from rebar_optimizer.i18n import th


def test_every_issue_code_has_a_message() -> None:
    assert set(th.MESSAGES) == set(IssueCode)


def test_format_issues_puts_errors_first() -> None:
    issues = [
        Issue.warning(IssueCode.LENGTH_MAYBE_MM),
        Issue.error(IssueCode.EMPTY_VALUE, row=7, column="Quantity"),
    ]
    assert th.format_issues(issues) == [
        "[ข้อผิดพลาด] แถว 7: ช่อง Quantity (จำนวน) ว่าง",
        "[คำเตือน] ความยาวทุกแถวมีค่าตั้งแต่ 100 ขึ้นไป หน่วยอาจเป็นมิลลิเมตร ไม่ใช่เมตร "
        "กรุณาตรวจสอบ (คอลัมน์ Length ต้องเป็นเมตร)",
    ]


def test_mm_params_get_metre_variants() -> None:
    issue = Issue.warning(IssueCode.LENGTH_SUB_MM, row=4, value=3.5004, length_mm=3500)
    assert th.format_issue(issue) == (
        "แถว 4: ความยาว 3.5004 ม. ละเอียดกว่า 1 มม. โปรแกรมใช้ค่า 3.500 ม. แทน"
    )


def test_output_locked_names_the_file() -> None:
    issue = Issue.error(IssueCode.OUTPUT_LOCKED, path="C:/x/plan.xlsx", name="plan.xlsx")
    assert "กรุณาปิดไฟล์ plan.xlsx แล้วลองใหม่" in th.format_issue(issue)
