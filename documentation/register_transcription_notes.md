# Register photo transcription — Zeishan Petroleum Service (PSO), dated 06-10-2026

Source photo: `workspace/user/media_library/image/2c/2c733c4e4ce2f11474c3b5984ee9a87542df3a416d4aa6a2245a74b42657efe0.png`
Transcribed: 2026-10-15 by research stage. Certainty per section below.

## HIGH certainty (arithmetically self-verified)

**Nozzle readings (bottom table)** — every current−previous equals the register's
own sale row, and product sums equal the register's سیل column:

| Nozzle | Product | Previous | Current | Sale (L) |
|---|---|---|---|---|
| 1 PG+ | petrol | 84,938 | 84,985 | 47 |
| 2 PG+ | petrol | 313,261 | 313,711 | 450 |
| 3 HSD | diesel | 569,441 | 569,549 | 108 |
| 4 HSD | diesel | 374,803 | 374,860 | 57 |

Petrol: 47 + 450 = **497 L** = register's پیٹرول سیل. Diesel: 108 + 57 = **165 L** = register's ڈیزل سیل.
Business date: **2026-10-06** (تاریخ 06-10-2026 in header). Station: ذیشان پیٹرولیم سروس (Zeishan Petroleum Service), PSO branding.

## MEDIUM certainty (stock table, middle)

- Diesel: opening 2,225 + delivery 1,000 = 3,225 total; sale 165; dip 616; stock-per-dip 3,260; difference **35** (= 3,260 − 3,225, exact — corroborates the read).
- Petrol: delivery 1,000; total 1,014 (implies opening ≈ 14 L — unusually low, confirm); sale 497; dip 260; stock-per-dip 955; difference ≈ 59 (cell hard to read).

## LOW certainty (rates & cash amounts — handwriting)

The rate line looked like "497 × ~400" with petrol amount 198,800, diesel amount
66,163, and نوزل سیل رقم (nozzle sale total) 265,963. None of this reconciles
cleanly with the official notified rates, so **do not use as ground truth**.
Top cash-summary table (سابقہ کیش 412,519 / آمد / وصولی 635,800 / اخری بیلنس 898,880)
is likewise only partially legible — left for the user to confirm if needed.

## Reliable rate reference (for price_history)

Official OGRA/Petroleum Division notified rates effective **2026-10-06**
(the register's date): petrol **Rs 393.64/L**, HSD **Rs 397.76/L**
(source: Petroleum Division notification via inpk.pk).
At those rates: petrol 497 L → Rs 195,639.08; diesel 165 L → Rs 65,630.40;
total Rs 261,269.48.

Pakistan now revises fuel prices **daily** (seven-day average mechanism; frozen on
public holidays/weekends — nation.com.pk, 2026-10-13). Current notified rates
effective 2026-10-15: petrol Rs 393.86/L, HSD Rs 396.65/L
(sources: Aaj News english.aaj.tv, UrduPoint, both 2026-10-14).

## Suggested use (for the main agent)

- Add a "real register day" fixture to Phase 6 reporting/export tests:
  4 nozzles, 2026-10-06, 497 L petrol + 165 L diesel at the official 2026-10-06
  rates — exercises the 21 reports and Urdu PDF/XLSX exports against the user's
  actual data shape, alongside the synthetic 800/300 L scenarios.
- Ask the user to confirm the uncertain rates/amounts before using them anywhere.
- The daily price-revision mechanism means price_history will gain a row most
  working days — worth a note in the user guide.
