# Rebar Cutting Optimizer v2

โปรแกรม Desktop (Windows) สำหรับวางแผนตัดเหล็กเส้นให้คุ้มที่สุด
(One-dimensional Cutting Stock Problem แบบหลายความยาว stock)

> สถานะ: อยู่ระหว่างพัฒนา — ดูแผนงานใน [PLAN.md](PLAN.md)

## ติดตั้งสำหรับนักพัฒนา

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
```

## ตรวจคุณภาพโค้ด

```bash
pytest
ruff check
mypy
```
