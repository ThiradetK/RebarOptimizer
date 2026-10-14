# Rebar Cutting Optimizer v2

โปรแกรม Desktop (Windows) สำหรับวางแผนตัดเหล็กเส้นให้คุ้มที่สุด
(One-dimensional Cutting Stock Problem แบบหลายความยาว stock)

> สถานะ: อยู่ระหว่างพัฒนา — ดูแผนงานใน [PLAN.md](PLAN.md)

## ใช้งานผ่าน CLI

```bash
python -m rebar_optimizer --make-template template.xlsx
python -m rebar_optimizer template.xlsx --stock 10,12 -o cutting_plan.xlsx
python -m rebar_optimizer template.xlsx --stock 10,12 --kerf-mm 3 --single-length --time-limit 30
python -m rebar_optimizer --selftest
```

exit code: 0 = สำเร็จ, 1 = input/ตั้งค่าผิด, 2 = มีชิ้นวางไม่ได้ (ยังเขียนไฟล์), 3 = error อื่น

## ความหมายของสถานะ solver

| สถานะ | ความหมาย |
|---|---|
| `OPTIMAL` | พิสูจน์แล้วว่าใช้เหล็กรวมน้อยที่สุด (gap 0%) |
| `FEASIBLE` | คำตอบดี แต่หมดเวลาก่อนพิสูจน์ — ดู gap % ว่าห่างจากค่าที่ดีที่สุดที่เป็นไปได้เท่าไร |
| `HEURISTIC` | รูปแบบการตัดมีมากเกินเพดาน จึงใช้ column generation ใกล้เคียงดีที่สุด (ดู gap %) |
| `GREEDY` | ใช้วิธีเร็ว (ILP ไม่ได้ผลที่ดีกว่า, รูปแบบการตัดเกินเพดาน, หรือวิธีเร็วได้ค่าเท่ากับ lower bound แล้ว) |

**gap %** = (ผล − lower bound) / ผล ถ้าเป็น 0% แปลว่าไม่มีทางใช้เหล็กน้อยกว่านี้ได้

ลำดับการคำนวณต่อ Size: greedy (Best-Fit Decreasing) → ถ้ายังไม่ถึง lower bound
สร้างรูปแบบการตัด (pattern) ที่ maximal ทั้งหมด → CP-SAT → เลือกแผนที่ใช้เหล็กรวมน้อยกว่า
ผลสุดท้ายจึงไม่แย่กว่า greedy เสมอ ถ้ารูปแบบการตัดเกิน `max_patterns` (50,000)
จะใช้ column generation (LP + knapsack pricing) แล้วให้ CP-SAT เลือกจากรูปแบบที่สร้างมา
ถ้าผลที่ได้เท่ากับ lower bound จาก LP ก็ถือว่าพิสูจน์แล้วว่าดีที่สุดและแสดงเป็น `OPTIMAL`

## Benchmark: greedy เทียบกับ greedy + ILP

ชุดข้อมูลสังเคราะห์ `medium` (5 Size × 15 ความยาว, 500 ชิ้น, seed คงที่),
stock 10/12 ม., time limit 30 วินาทีต่อ Size, CP-SAT 8 workers

```bash
python -m benchmarks.run medium --time-limit 30
```

| Size | ชิ้น | ความยาว | Greedy (m) | ILP (m) | ลดลง | สถานะ | gap | เวลา (s) |
|---|---:|---:|---:|---:|---:|---|---:|---:|
| DB12 | 100 | 15 | 468.0 | 466.0 | 0.4% | OPTIMAL | 0.00% | 0.1 |
| DB16 | 100 | 15 | 544.0 | 544.0 | 0.0% | OPTIMAL | 0.00% | 0.1 |
| DB20 | 100 | 15 | 538.0 | 538.0 | 0.0% | OPTIMAL | 0.00% | 0.1 |
| DB25 | 100 | 15 | 340.0 | 334.0 | 1.8% | OPTIMAL | 0.00% | 6.6 |
| RB9 | 100 | 15 | 286.0 | 280.0 | 2.1% | OPTIMAL | 0.00% | 1.9 |
| **รวม** | | | 2176.0 | 2162.0 | 0.6% | | | |

ผลรวมจากการรันหลายแบบ (ทุก Size ได้ `OPTIMAL` ภายในเวลาที่กำหนด, Size ที่ช้าสุด 6.8 วินาที):

| ชุดทดสอบ | Greedy (m) | ILP (m) | ลดลง |
|---|---:|---:|---:|
| seed 0 | 2176.0 | 2162.0 | 0.6% |
| seed 1 | 2308.0 | 2296.0 | 0.5% |
| seed 2 | 2174.0 | 2132.0 | 1.9% |
| seed 0, kerf 3 มม. | 2244.0 | 2190.0 | 2.4% |
| seed 0, บังคับความยาวเดียว | 2238.0 | 2234.0 | 0.2% |
| seed 3, stock 6/9/12 ม., kerf 5 มม. | 2112.0 | 2085.0 | 1.3% |

### ชุดใหญ่ (column generation)

ชุด `large` (5 Size × 40 ความยาว, 2,000 ชิ้น) รูปแบบการตัดเกิน 50,000 แบบในเกือบทุก Size
จึงใช้ column generation

```bash
python -m benchmarks.run large --time-limit 30
```

| Size | ชิ้น | ความยาว | Greedy (m) | ผล (m) | ลดลง | สถานะ | gap | เวลา (s) |
|---|---:|---:|---:|---:|---:|---|---:|---:|
| DB12 | 400 | 40 | 1448.0 | 1426.0 | 1.5% | OPTIMAL | 0.00% | 0.8 |
| DB16 | 400 | 40 | 1580.0 | 1564.0 | 1.0% | HEURISTIC | 0.13% | 0.7 |
| DB20 | 400 | 40 | 1384.0 | 1372.0 | 0.9% | OPTIMAL | 0.00% | 0.7 |
| DB25 | 400 | 40 | 1388.0 | 1364.0 | 1.7% | OPTIMAL | 0.00% | 0.9 |
| RB9 | 400 | 40 | 1714.0 | 1684.0 | 1.8% | OPTIMAL | 0.00% | 0.6 |
| **รวม** | | | 7514.0 | 7410.0 | 1.4% | | | |

| ชุดทดสอบ (large) | Greedy (m) | ผล (m) | ลดลง | Size ที่ช้าสุด (s) | gap สูงสุด |
|---|---:|---:|---:|---:|---:|
| seed 0 | 7514.0 | 7410.0 | 1.4% | 0.9 | 0.13% |
| seed 1 | 8234.0 | 8106.0 | 1.6% | 0.8 | 0.00% |
| seed 2, kerf 3 มม. | 8284.0 | 8134.0 | 1.8% | 0.9 | 0.00% |
| seed 0, บังคับความยาวเดียว | 7530.0 | 7440.0 | 1.2% | 19.7 | 0.13% |
| seed 3, stock 6/9/12 ม., kerf 5 มม. | 8091.0 | 7974.0 | 1.4% | 16.2 | 0.00% |

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
