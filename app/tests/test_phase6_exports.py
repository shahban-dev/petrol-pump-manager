"""Phase 6 export engine tests (PDF / XLSX / CSV).

Builds temporary databases only (the shared ``db``/``db_file`` fixtures),
posts a small realistic day through the existing services where a
committed-records guarantee is under test, and otherwise constructs
:class:`ReportResult` directly — the export engine's frozen input contract
(``app/models/report.py``), independent of ``reporting_service``.
"""

from __future__ import annotations

import csv
import sqlite3
from decimal import Decimal
from pathlib import Path

import arabic_reshaper
import pytest
from bidi.algorithm import get_display
from openpyxl import load_workbook
from pypdf import PdfReader

from app.models.report import ReportColumn, ReportResult
from app.services import errors
from app.services.report_export import (
    export_report_csv,
    export_report_pdf,
    export_report_xlsx,
)

# ---------------------------------------------------------------------------
# ReportResult factories (frozen contract — constructed directly)
# ---------------------------------------------------------------------------

GEN_AT = "2026-10-15T11:30:00.000Z"
DAY = "2026-10-14"

_REPORT_SPECS = {
    "daily_register": {
        "title_en": "Daily Register",
        "title_ur": "روزنامچہ",
        "columns": [
            ReportColumn("nozzle", "Nozzle", "نوزل", kind="text"),
            ReportColumn("litres", "Litres", "لیٹر", kind="quantity"),
            ReportColumn("rate", "Rate", "ریٹ", kind="price"),
            ReportColumn("amount", "Amount", "رقم", kind="money"),
            ReportColumn("day", "Date", "تاریخ", kind="date"),
            ReportColumn("txns", "Txns", "لین دین", kind="number"),
        ],
        "rows": [
            {
                "nozzle": "1 PG+",
                "litres": 125_430,
                "rate": 2_659_630,
                "amount": 333_789_00,
                "day": DAY,
                "txns": 41,
            },
            {
                "nozzle": "3 HSD",
                "litres": 210_000,
                "rate": 2_625_000,
                "amount": 551_250_00,
                "day": DAY,
                "txns": 37,
            },
        ],
        "totals": {"litres": 335_430, "amount": 885_039_00, "txns": 78},
        "notes_en": ["Figures are taken from committed records only."],
        "notes_ur": ["تمام اعداد و شمار صرف حتمی ریکارڈ سے لیے گئے ہیں۔"],
    },
    "customer_statement": {
        "title_en": "Customer Statement",
        "title_ur": "گاہک کا گوشوارہ",
        "columns": [
            ReportColumn("customer", "Customer", "گاہک", kind="text"),
            ReportColumn("invoice", "Invoice", "انوائس", kind="text"),
            ReportColumn("debit", "Debit", "ڈیبٹ", kind="money"),
            ReportColumn("credit", "Credit", "کریڈٹ", kind="money"),
            ReportColumn("balance", "Balance", "بیلنس", kind="money"),
        ],
        "rows": [
            {
                "customer": "Ali Transport",
                "invoice": "INV-101",
                "debit": 150_000_00,
                "credit": 100_000_00,
                "balance": 50_000_00,
            },
            {
                "customer": "City Bus Service",
                "invoice": "INV-102",
                "debit": 75_250_00,
                "credit": 75_250_00,
                "balance": 0,
            },
        ],
        "totals": {"debit": 225_250_00, "credit": 175_250_00, "balance": 50_000_00},
        "notes_en": ["Aging buckets follow the credit policy."],
        "notes_ur": ["واجبات کی مدت کریڈٹ پالیسی کے مطابق ہے۔"],
    },
    "cashbook": {
        "title_en": "Cashbook",
        "title_ur": "کیش بک",
        "columns": [
            ReportColumn("narr", "Description", "تفصیل", kind="text"),
            ReportColumn("receipt", "Receipt", "وصولی", kind="money"),
            ReportColumn("payment", "Payment", "ادائیگی", kind="money"),
        ],
        "rows": [
            {"narr": "Opening balance", "receipt": 2_000_000_00, "payment": 0},
            {"narr": "Fuel sales — cash", "receipt": 885_039_00, "payment": 0},
            {"narr": "PSO depot payment", "receipt": 0, "payment": 1_200_000_00},
        ],
        "totals": {"receipt": 2_885_039_00, "payment": 1_200_000_00},
        "notes_en": ["Reversed entries appear as separate reversal lines."],
        "notes_ur": ["منسوخ شدہ اندراجات الگ سطروں میں ظاہر ہوتے ہیں۔"],
    },
    "pnl_daily": {
        "title_en": "Daily Profit & Loss",
        "title_ur": "یومیہ نفع و نقصان",
        "columns": [
            ReportColumn("head", "Head", "مد", kind="text"),
            ReportColumn("amount", "Amount", "رقم", kind="money"),
        ],
        "rows": [
            {"head": "Fuel sales", "amount": 885_039_00},
            {"head": "Fuel cost (COGS)", "amount": 812_400_00},
            {"head": "Gross profit", "amount": 72_639_00},
            {"head": "Expenses", "amount": 9_500_00},
            {"head": "Net profit", "amount": 63_139_00},
        ],
        "totals": {"amount": 63_139_00},
        "notes_en": ["Net profit = gross profit − expenses."],
        "notes_ur": ["خالص نفع = مجموعی نفع − اخراجات۔"],
    },
}


def make_result(report_id: str) -> ReportResult:
    spec = _REPORT_SPECS[report_id]
    return ReportResult(
        report_id=report_id,
        title_en=spec["title_en"],
        title_ur=spec["title_ur"],
        generated_at_utc=GEN_AT,
        business_date=DAY,
        closing_revision=2,
        period_label_en="14 Oct 2026",
        period_label_ur="14 اکتوبر 2026",
        columns=list(spec["columns"]),
        rows=[dict(r) for r in spec["rows"]],
        totals=dict(spec["totals"]),
        notes_en=list(spec["notes_en"]),
        notes_ur=list(spec["notes_ur"]),
    )


# ---------------------------------------------------------------------------
# 1. every report × every language × every format generates a real file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("report_id", sorted(_REPORT_SPECS))
@pytest.mark.parametrize("lang", ["en", "ur"])
def test_all_formats_generate_nonempty_files(tmp_path: Path, report_id: str, lang: str):
    result = make_result(report_id)
    pdf = export_report_pdf(result, tmp_path / f"{report_id}_{lang}.pdf", lang=lang)
    xlsx = export_report_xlsx(result, tmp_path / f"{report_id}_{lang}.xlsx", lang=lang)
    csv_path = export_report_csv(result, tmp_path / f"{report_id}_{lang}.csv", lang=lang)
    for path in (pdf, xlsx, csv_path):
        assert path.is_file(), path
        assert path.stat().st_size > 0, path


# ---------------------------------------------------------------------------
# 2. Urdu PDF: real embedded glyphs, not tofu
# ---------------------------------------------------------------------------


def _shaped(text: str) -> str:
    return get_display(arabic_reshaper.reshape(text))


def test_urdu_pdf_embeds_font_and_renders_urdu(tmp_path: Path):
    result = make_result("daily_register")
    pdf_path = export_report_pdf(result, tmp_path / "ur.pdf", lang="ur")

    raw = pdf_path.read_bytes()
    # subset font embedded (FontFile2), not a referenced system font
    assert b"/FontFile2" in raw
    assert b"NotoSansArabic" in raw

    reader = PdfReader(str(pdf_path))
    assert len(reader.pages) >= 1
    text = "\n".join(p.extract_text() or "" for p in reader.pages)

    # shaped (presentation-form) glyphs present — .notdef tofu would extract blank
    n_present = sum(1 for ch in text if 0xFB50 <= ord(ch) <= 0xFEFF)
    assert n_present > 20, f"expected shaped Urdu glyphs, found {n_present}"

    # The painted glyph order in the content stream is the bidi visual order
    # (verified against the font ToUnicode map during development); pypdf's
    # extractor emits that painted run reversed, hence the [::-1] here.
    assert _shaped("روزنامچہ")[::-1] in text
    assert _shaped("نوزل")[::-1] in text
    # Western digits kept (paper-register convention), not Eastern Arabic digits
    assert "2026" in text
    assert "۲" not in text and "۴" not in text


def test_english_pdf_content(tmp_path: Path):
    result = make_result("daily_register")
    pdf_path = export_report_pdf(result, tmp_path / "en.pdf", lang="en")
    text = "\n".join(p.extract_text() or "" for p in PdfReader(str(pdf_path)).pages)
    assert "Daily Register" in text
    assert "Business date: 14-10-2026" in text
    assert "Closing revision: 2" in text
    assert "PKR" in text and "333,789.00" in text  # money: paisa -> PKR, never float
    assert "125.430 L" in text  # quantity: ml -> litres, 3 dp
    assert "2,659.6300 PKR/L" not in text  # sanity: units are 0.0001 PKR, not PKR
    assert "265.9630 PKR/L" in text  # price: 0.0001 units -> 4 dp
    assert "Total" in text
    assert "Page 1" in text


# ---------------------------------------------------------------------------
# 3. XLSX: typed numeric cells, RTL sheet view, mirrored columns
# ---------------------------------------------------------------------------


def _find_row(ws, label: str):
    for row in ws.iter_rows(values_only=True):
        if row[0] == label:
            return row
    raise AssertionError(f"row starting with {label!r} not found")


def test_xlsx_money_cells_are_typed_numerics(tmp_path: Path):
    result = make_result("daily_register")
    xlsx_path = export_report_xlsx(result, tmp_path / "en.xlsx", lang="en")
    ws = load_workbook(str(xlsx_path)).active

    totals = _find_row(ws, "Total")
    # EN column order: nozzle | litres | rate | amount | day | txns
    amount_cell = None
    for row in ws.iter_rows():
        if row[0].value == "Total":
            amount_cell = row[3]
            break
    assert amount_cell is not None
    assert isinstance(amount_cell.value, (int, float)), type(amount_cell.value)
    # exact Decimal comparison — value written from integer paisa, never float
    assert Decimal(str(amount_cell.value)) == Decimal("885039.00")
    assert amount_cell.number_format == '"PKR" #,##0.00'

    # a quantity cell is numeric with 3-dp format
    litres_cell = None
    for row in ws.iter_rows():
        if row[0].value == "1 PG+":
            litres_cell = row[1]
            break
    assert litres_cell is not None
    assert Decimal(str(litres_cell.value)) == Decimal("125.430")
    assert litres_cell.number_format == "#,##0.000"

    # a price cell is numeric with 4-dp format
    assert Decimal(str(row[2].value)) == Decimal("265.9630")
    assert row[2].number_format == "#,##0.0000"


def test_xlsx_urdu_rtl_and_mirrored_columns(tmp_path: Path):
    result = make_result("daily_register")
    ur = load_workbook(str(export_report_xlsx(result, tmp_path / "ur.xlsx", lang="ur"))).active
    en = load_workbook(str(export_report_xlsx(result, tmp_path / "en.xlsx", lang="en"))).active

    assert ur.sheet_view.rightToLeft is True
    assert en.sheet_view.rightToLeft in (None, False)

    def header_labels(ws):
        for row in ws.iter_rows(values_only=True):
            if row[0] == "Nozzle":  # EN header row
                return list(row)
            if row[0] == _shaped("لین دین"):  # UR header row (mirrored: last logical first)
                return list(row)
        raise AssertionError("header row not found")

    en_headers = header_labels(en)
    ur_headers = header_labels(ur)
    assert en_headers[0] == "Nozzle"
    # mirrored order: reversed logical columns
    assert ur_headers[0] == _shaped("لین دین")
    assert ur_headers[-1] == _shaped("نوزل")


# ---------------------------------------------------------------------------
# 4. CSV: BOM, parseable, human-readable values
# ---------------------------------------------------------------------------


def test_csv_bom_and_values(tmp_path: Path):
    result = make_result("daily_register")
    csv_path = export_report_csv(result, tmp_path / "en.csv", lang="en")

    raw = csv_path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "CSV must be UTF-8 with BOM"

    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    flat = [" | ".join(r) for r in rows]
    blob = "\n".join(flat)
    assert "Daily Register" in blob
    assert "PKR 333,789.00" in blob
    assert "125.430 L" in blob
    assert "14-10-2026" in blob
    # totals row present
    assert any(r[0] == "Total" and "PKR 885,039.00" in r for r in rows if r)


def test_csv_urdu_values(tmp_path: Path):
    result = make_result("cashbook")
    csv_path = export_report_csv(result, tmp_path / "ur.csv", lang="ur")
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        blob = fh.read()
    assert _shaped("کیش بک") in blob
    assert _shaped("کل") in blob  # totals label
    assert "PKR 2,885,039.00" in blob  # Western digits inside Urdu CSV


# ---------------------------------------------------------------------------
# 5. validation: bad input raises, never a partial file
# ---------------------------------------------------------------------------


def test_export_validation_errors(tmp_path: Path):
    good = make_result("cashbook")
    dest = tmp_path / "out.pdf"

    with pytest.raises(errors.ValidationError):
        export_report_pdf(good, dest, lang="xx")
    with pytest.raises(errors.ValidationError):
        export_report_pdf("not-a-result", dest)  # type: ignore[arg-type]
    with pytest.raises(errors.ValidationError):
        export_report_xlsx(good, tmp_path / "no_such_dir" / "x.xlsx")
    empty_cols = make_result("cashbook")
    object.__setattr__(empty_cols, "columns", [])
    with pytest.raises(errors.ValidationError):
        export_report_csv(empty_cols, tmp_path / "x.csv")

    bad_kind_cols = [ReportColumn("x", "X", "X", kind="bogus")]
    bad = make_result("cashbook")
    object.__setattr__(bad, "columns", bad_kind_cols)
    with pytest.raises(errors.ValidationError):
        export_report_pdf(bad, tmp_path / "x.pdf")

    # float money must fail loud (never silently formatted)
    floaty = make_result("cashbook")
    rows = [dict(r) for r in floaty.rows]
    rows[0] = dict(rows[0], receipt=10.5)
    object.__setattr__(floaty, "rows", rows)
    with pytest.raises(errors.ValidationError):
        export_report_xlsx(floaty, tmp_path / "x.xlsx")
    with pytest.raises(errors.ValidationError):
        export_report_csv(floaty, tmp_path / "x.csv")

    assert not dest.exists(), "failed export must not leave a partial file"
    assert not (tmp_path / "x.xlsx").exists()
    assert not (tmp_path / "x.csv").exists()


# ---------------------------------------------------------------------------
# 6. station name comes from app_settings
# ---------------------------------------------------------------------------


def test_station_name_from_app_settings(db: sqlite3.Connection, tmp_path: Path):
    db.execute(
        "INSERT INTO app_settings (key, value) VALUES ('station_name', 'PSO Model Town')"
    )
    db.commit()
    result = make_result("pnl_daily")
    pdf_path = export_report_pdf(result, tmp_path / "s.pdf", lang="en", conn=db)
    text = "\n".join(p.extract_text() or "" for p in PdfReader(str(pdf_path)).pages)
    assert "PSO Model Town" in text

    # explicit override wins over app_settings
    pdf_path2 = export_report_pdf(
        result, tmp_path / "s2.pdf", lang="en", conn=db, station_name="Custom Name"
    )
    text2 = "\n".join(p.extract_text() or "" for p in PdfReader(str(pdf_path2)).pages)
    assert "Custom Name" in text2


# ---------------------------------------------------------------------------
# 7. exports reflect committed records only
# ---------------------------------------------------------------------------

COMMIT_DAY = "2026-10-12"
_NOZZLES = ("1 PG+", "2 PG+", "3 HSD", "4 HSD")


def _nozzle_readings_result(db_file: Path, business_date: str) -> ReportResult:
    """Build a nozzle-readings ReportResult reading through a FRESH connection.

    Only committed rows are visible here — exactly what the export engine sees.
    """
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT n.code AS code, r.reading_ml AS ml"
            " FROM nozzle_readings r JOIN nozzles n ON n.id = r.nozzle_id"
            " WHERE r.business_date = ? ORDER BY n.code",
            (business_date,),
        ).fetchall()
    finally:
        conn.close()
    total_ml = sum(int(r["ml"]) for r in rows)
    return ReportResult(
        report_id="nozzle_readings",
        title_en="Nozzle Readings",
        title_ur="نوزل ریڈنگ",
        generated_at_utc="2026-10-15T11:30:00.000Z",
        business_date=business_date,
        columns=[
            ReportColumn("nozzle", "Nozzle", "نوزل", kind="text"),
            ReportColumn("reading", "Reading", "ریڈنگ", kind="quantity"),
        ],
        rows=[{"nozzle": r["code"], "reading": int(r["ml"])} for r in rows],
        totals={"reading": total_ml},
    )


def test_export_reflects_committed_records_only(
    db_file: Path, phase3_services, tmp_path: Path
):
    from .conftest import seed_first_day

    svc = phase3_services
    seed_first_day(svc, COMMIT_DAY, opening_cash_paisa=2_000_000)
    for code in _NOZZLES:
        svc.nozzles.record_reading(COMMIT_DAY, code, "120")  # 120 L each, committed

    # An UNCOMMITTED reading on a second connection must stay invisible.
    writer = sqlite3.connect(str(db_file))
    writer.execute("BEGIN IMMEDIATE")
    try:
        writer.execute(
            "INSERT INTO nozzle_readings"
            " (nozzle_id, business_date, reading_kind, reading_ml, created_by)"
            " VALUES ((SELECT id FROM nozzles WHERE code = '1 PG+'),"
            " ?, 'segment', 999999000, 'sneaky')",
            (COMMIT_DAY,),
        )
        result = _nozzle_readings_result(db_file, COMMIT_DAY)

        xlsx_path = export_report_xlsx(result, tmp_path / "committed.xlsx", lang="en")
        ws = load_workbook(str(xlsx_path)).active
        totals = _find_row(ws, "Total")
        assert Decimal(str(totals[1])) == Decimal("480.000")  # 4 × 120 L committed
        # the uncommitted 999,999 L segment must not leak in (would read 1000479.000)

        csv_path = export_report_csv(result, tmp_path / "committed.csv", lang="en")
        blob = csv_path.read_text(encoding="utf-8-sig")
        assert "999,999" not in blob and "999999" not in blob
    finally:
        writer.rollback()
        writer.close()

    # after rollback the ledger is unchanged: export matches the ledger exactly
    conn = sqlite3.connect(str(db_file))
    try:
        ledger_total = conn.execute(
            "SELECT COALESCE(SUM(reading_ml), 0) FROM nozzle_readings WHERE business_date = ?",
            (COMMIT_DAY,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert ledger_total == 480_000
    assert result.totals["reading"] == ledger_total
