"""Phase 6 export engine: render a :class:`ReportResult` to PDF, XLSX or CSV.

Input contract: ``app.models.report.ReportResult`` — rows carry *raw* values
(money as integer paisa, quantities as integer milliliters, prices as scaled
integers). All formatting (Decimal conversion, rounding, digit choice) happens
here at the render boundary; **float is never used** for money/quantities —
values flow through :mod:`app.calculations.money` helpers only.

RTL / Urdu approach
-------------------
ReportLab (and openpyxl/CSV, which are plain text) have no Arabic
complex-text layout, so every Urdu string is pre-processed before it is
drawn or written:

1. ``arabic_reshaper.reshape`` — joins letters into their contextual
   presentation forms (isolated/initial/medial/final).
2. ``bidi.algorithm.get_display`` — reorders the shaped string into visual
   (left-to-right) order, so a plain LTR text run draws correctly.

Digits stay **Western** (0-9) in both languages — reshaper does not convert
them and the paper register the app replaces uses Western digits.

Layout mirroring in Urdu mode:

* the table column order is **reversed** (first logical column at the right),
* text columns align right, numeric kinds (money/quantity/price/number)
  stay right-aligned in *both* languages, ``center`` columns stay centered,
* XLSX sheets set ``sheet_view.rightToLeft = True``,
* the PDF footer page number is rendered in Urdu.

Fonts are bundled under ``app/resources/fonts/`` (Noto Sans Arabic Regular
+ Bold, OFL licensed) and registered with ReportLab at runtime — the Windows
installer target cannot be assumed to have any Urdu-capable system font.
The same family renders English too (it has full Latin coverage), so mixed
strings such as ``"PKR 1,234.56"`` inside Urdu text never switch fonts.

All three exporters validate their input, raise
:class:`~app.services.errors.ValidationError` on bad input, and never leave
partial files behind: output goes to a temp file in the destination directory
and is atomically renamed into place with :func:`os.replace`.
"""

from __future__ import annotations

import contextlib
import csv
import os
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import arabic_reshaper
from bidi.algorithm import get_display

from app.calculations.money import format_pkr, from_ml, from_paisa, from_price_10k
from app.models.report import ReportColumn, ReportResult
from app.services.errors import ValidationError

__all__ = [
    "export_report_pdf",
    "export_report_xlsx",
    "export_report_csv",
    "SUPPORTED_LANGS",
]

#: Languages accepted by the exporters.
SUPPORTED_LANGS = ("en", "ur")

#: Column kinds that are numeric and therefore right-aligned in both languages.
_NUMERIC_KINDS = frozenset({"money", "quantity", "price", "number"})

#: Kinds for which a totals-row value is rendered (others stay blank).
_TOTALLED_KINDS = frozenset({"money", "quantity", "price", "number"})

_FONT_DIR = Path(__file__).resolve().parents[1] / "resources" / "fonts"
_FONT_REGULAR = "NotoSansArabic"
_FONT_BOLD = "NotoSansArabic-Bold"

# ---------------------------------------------------------------------------
# validation + atomic write
# ---------------------------------------------------------------------------


def _validate(result: ReportResult, lang: str, dest: str | os.PathLike) -> Path:
    """Validate exporter inputs; return the destination as a Path."""
    if not isinstance(result, ReportResult):
        raise ValidationError(
            f"export expects a ReportResult, got {type(result).__name__}"
        )
    if lang not in SUPPORTED_LANGS:
        raise ValidationError(
            f"unsupported language {lang!r}; expected one of {SUPPORTED_LANGS}"
        )
    dest_path = Path(dest)
    if not dest_path.name:
        raise ValidationError("destination must include a file name")
    parent = dest_path.parent
    if not parent.exists():
        raise ValidationError(f"destination directory does not exist: {parent}")
    if not parent.is_dir() or dest_path.is_dir():
        raise ValidationError(f"destination is not a writable file path: {dest_path}")
    if not result.report_id or not isinstance(result.report_id, str):
        raise ValidationError("ReportResult.report_id must be a non-empty string")
    if not result.columns:
        raise ValidationError("ReportResult.columns must not be empty")
    valid_kinds = {"text", "money", "quantity", "price", "date", "number"}
    for col in result.columns:
        if not isinstance(col, ReportColumn):
            raise ValidationError("ReportResult.columns must hold ReportColumn items")
        if not col.key:
            raise ValidationError("ReportColumn.key must be a non-empty string")
        if col.kind not in valid_kinds:
            raise ValidationError(f"unknown column kind {col.kind!r} for {col.key!r}")
        if col.align not in ("left", "right", "center"):
            raise ValidationError(f"unknown column align {col.align!r} for {col.key!r}")
    if not isinstance(result.rows, list) or not all(
        isinstance(r, dict) for r in result.rows
    ):
        raise ValidationError("ReportResult.rows must be a list of dicts")
    if not isinstance(result.totals, dict):
        raise ValidationError("ReportResult.totals must be a dict")
    return dest_path


@contextlib.contextmanager
def _atomic_write(dest: Path):
    """Yield a temp path; atomically rename it onto *dest* on success.

    On any error the temp file is removed, so a failed export never leaves a
    partial file at the destination.
    """
    tmp = dest.parent / f".{dest.name}.{os.getpid()}.tmp"
    try:
        yield tmp
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    else:
        os.replace(tmp, dest)


# ---------------------------------------------------------------------------
# language / shaping helpers
# ---------------------------------------------------------------------------


def _shape(text: str, lang: str) -> str:
    """Prepare *text* for drawing/writing in *lang*.

    Urdu: reshape to presentation forms + reorder to visual order.
    English: returned unchanged.
    """
    if lang != "ur":
        return text
    return get_display(arabic_reshaper.reshape(text))


def _title(result: ReportResult, lang: str) -> str:
    title = result.title_ur if lang == "ur" else result.title_en
    if not title:
        raise ValidationError(f"ReportResult has no title for language {lang!r}")
    return title


def _label(col: ReportColumn, lang: str) -> str:
    return col.label_ur if lang == "ur" else col.label_en


def _period_label(result: ReportResult, lang: str) -> str:
    label = result.period_label_ur if lang == "ur" else result.period_label_en
    if label:
        return label
    if result.business_date:
        return _format_date(result.business_date)
    if result.date_from and result.date_to:
        return f"{_format_date(result.date_from)} – {_format_date(result.date_to)}"
    return ""


def _format_date(value: str | None) -> str:
    """'YYYY-MM-DD' -> 'DD-MM-YYYY' (display only)."""
    if not value:
        return ""
    parts = str(value).split("-")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return f"{parts[2]}-{parts[1]}-{parts[0]}"
    return str(value)


def _format_generated(value: str | None) -> str:
    """'YYYY-MM-DDTHH:MM:SS.SSSZ' -> 'YYYY-MM-DD HH:MM UTC' (display only)."""
    if not value:
        return ""
    text = str(value).replace("T", " ")
    if "." in text:
        text = text.split(".", 1)[0]
    text = text.rstrip("Z").strip()
    return f"{text} UTC" if text else ""


def _station_name(
    conn: sqlite3.Connection | None, station_name: str | None, lang: str
) -> str:
    """Station name: explicit arg > app_settings.station_name > generic fallback."""
    if station_name:
        return station_name
    if conn is not None:
        try:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = 'station_name'"
            ).fetchone()
        except Exception:  # noqa: BLE001 - settings read is best-effort
            row = None
        if row and row[0]:
            return str(row[0])
    return "پٹرول پمپ" if lang == "ur" else "Petrol Station"


def _display_columns(result: ReportResult, lang: str) -> list[ReportColumn]:
    """Columns in display order — mirrored (reversed) for Urdu RTL layout."""
    cols = list(result.columns)
    if lang == "ur":
        cols.reverse()
    return cols


def _cell_align(col: ReportColumn, lang: str) -> str:
    """Horizontal alignment for a column: numeric kinds always right."""
    if col.align == "center":
        return "center"
    if col.kind in _NUMERIC_KINDS:
        return "right"
    return "right" if lang == "ur" else "left"


# ---------------------------------------------------------------------------
# value formatting (render boundary — integers in, display strings out)
# ---------------------------------------------------------------------------


def _format_value(value, kind: str) -> str:
    """Format a raw report value for PDF/CSV display. Never uses float."""
    if value is None:
        return ""
    if kind == "money":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValidationError(f"money value must be int paisa, got {value!r}")
        return f"PKR {format_pkr(value)}"
    if kind == "quantity":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValidationError(f"quantity value must be int ml, got {value!r}")
        return f"{from_ml(value):,.3f} L"
    if kind == "price":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValidationError(f"price value must be int 0.0001-PKR units, got {value!r}")
        return f"{from_price_10k(value):,.4f} PKR/L"
    if kind == "number":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValidationError(f"number value must be int, got {value!r}")
        return f"{value:,}"
    if kind == "date":
        return _format_date(value)
    return str(value)


def _totals_label_column(columns: list[ReportColumn]) -> str | None:
    """Key of the column that carries the 'Total' label (first text column)."""
    for col in columns:
        if col.kind == "text":
            return col.key
    return columns[0].key if columns else None


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

_fonts_registered = False


def _ensure_pdf_fonts() -> None:
    """Register the bundled Noto Sans Arabic TTFs with ReportLab (once)."""
    global _fonts_registered
    if _fonts_registered:
        return
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    regular = _FONT_DIR / "NotoSansArabic-Regular.ttf"
    bold = _FONT_DIR / "NotoSansArabic-Bold.ttf"
    for path in (regular, bold):
        if not path.is_file():
            raise ValidationError(f"bundled font missing: {path}")
    pdfmetrics.registerFont(TTFont(_FONT_REGULAR, str(regular)))
    pdfmetrics.registerFont(TTFont(_FONT_BOLD, str(bold)))
    _fonts_registered = True


def _pdf_header_lines(
    result: ReportResult,
    lang: str,
    conn: sqlite3.Connection | None,
    station_name: str | None,
) -> list[str]:
    lines = [
        _station_name(conn, station_name, lang),
        _title(result, lang),
    ]
    period = _period_label(result, lang)
    if period:
        lines.append(period)
    meta_bits: list[str] = []
    if result.business_date:
        day_label = "کاروباری تاریخ" if lang == "ur" else "Business date"
        meta_bits.append(f"{day_label}: {_format_date(result.business_date)}")
    if result.closing_revision is not None:
        rev_label = "بندش نظرثانی" if lang == "ur" else "Closing revision"
        meta_bits.append(f"{rev_label}: {result.closing_revision}")
    if meta_bits:
        lines.append("  |  ".join(meta_bits))
    gen_label = "تیار کردہ" if lang == "ur" else "Generated"
    lines.append(f"{gen_label}: {_format_generated(result.generated_at_utc)}")
    return lines


def _pdf_footer(canvas, doc, lang: str) -> None:  # noqa: ANN001, ANN202
    canvas.saveState()
    canvas.setFont(_FONT_REGULAR, 8)
    if lang == "ur":
        text = _shape(f"صفحہ {doc.page}", "ur")
    else:
        text = f"Page {doc.page}"
    from reportlab.lib.pagesizes import A4

    canvas.drawCentredString(A4[0] / 2.0, 28, text)
    canvas.restoreState()


def export_report_pdf(
    result: ReportResult,
    dest: str | os.PathLike,
    *,
    lang: str = "en",
    conn: sqlite3.Connection | None = None,
    station_name: str | None = None,
) -> Path:
    """Render *result* to an A4 PDF at *dest*.

    ``lang`` is ``"en"`` or ``"ur"``. The station name comes from
    ``app_settings.station_name`` via *conn* (optional), overridable with
    *station_name*. Returns the destination Path.
    """
    from functools import partial

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    dest_path = _validate(result, lang, dest)
    _ensure_pdf_fonts()

    align_map = {"left": TA_LEFT, "right": TA_RIGHT, "center": TA_CENTER}
    base_style = ParagraphStyle(
        "base", fontName=_FONT_REGULAR, fontSize=9, leading=12, textColor=colors.black
    )
    header_style = ParagraphStyle(
        "header",
        parent=base_style,
        fontName=_FONT_BOLD,
        textColor=colors.white,
    )
    totals_style = ParagraphStyle(
        "totals", parent=base_style, fontName=_FONT_BOLD
    )

    story: list = []
    header_sizes = [15, 13, 10, 9, 9]
    for i, line in enumerate(_pdf_header_lines(result, lang, conn, station_name)):
        size = header_sizes[min(i, len(header_sizes) - 1)]
        style = ParagraphStyle(
            f"h{i}",
            parent=base_style,
            fontName=_FONT_BOLD if i < 2 else _FONT_REGULAR,
            fontSize=size,
            leading=size + 4,
            alignment=TA_CENTER,
        )
        story.append(Paragraph(_shape(line, lang), style))
    story.append(Spacer(1, 10))

    columns = _display_columns(result, lang)
    aligns = [_cell_align(c, lang) for c in columns]

    # column widths: text columns get the most room, then money/price/quantity
    # (kept wide enough that "PKR 1,234,567.00" does not wrap mid-value)
    avail = A4[0] - 72  # 36pt margins each side
    weight_for = {
        "text": 2.2,
        "money": 1.6,
        "price": 1.6,
        "quantity": 1.4,
        "date": 1.2,
        "number": 0.8,
    }
    weights = [weight_for[c.kind] for c in columns]
    total_w = sum(weights)
    col_widths = [avail * w / total_w for w in weights]

    def cell(text: str, style: ParagraphStyle, align: str) -> Paragraph:
        s = ParagraphStyle(f"c{id(text)}", parent=style, alignment=align_map[align])
        return Paragraph(_shape(text, lang), s)

    data: list[list] = [
        [cell(_label(c, lang), header_style, aligns[i]) for i, c in enumerate(columns)]
    ]
    for row in result.rows:
        data.append(
            [
                cell(_format_value(row.get(c.key), c.kind), base_style, aligns[i])
                for i, c in enumerate(columns)
            ]
        )
    totals_key = _totals_label_column(columns)
    total_label = "کل" if lang == "ur" else "Total"
    totals_row = []
    for i, c in enumerate(columns):
        if c.key == totals_key:
            totals_row.append(cell(total_label, totals_style, aligns[i]))
        elif c.kind in _TOTALLED_KINDS and c.key in result.totals:
            totals_row.append(
                cell(_format_value(result.totals[c.key], c.kind), totals_style, aligns[i])
            )
        else:
            totals_row.append(cell("", totals_style, aligns[i]))
    data.append(totals_row)

    table_style = TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2f4f4f")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#888888")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, 0), 6),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
            ("TOPPADDING", (0, 1), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 1), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("LINEABOVE", (0, -1), (-1, -1), 1.2, colors.black),
            ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#f4f4f4")]),
        ]
    )
    for i, a in enumerate(aligns):
        table_style.add("ALIGN", (i, 0), (i, -1), a.upper())
    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.setStyle(table_style)
    story.append(table)

    notes = result.notes_ur if lang == "ur" else result.notes_en
    if notes:
        story.append(Spacer(1, 12))
        notes_title = "نوٹس" if lang == "ur" else "Notes"
        story.append(
            Paragraph(
                _shape(notes_title, lang),
                ParagraphStyle("nh", parent=base_style, fontName=_FONT_BOLD, fontSize=10),
            )
        )
        bullet = "• "
        for note in notes:
            story.append(
                Paragraph(
                    _shape(bullet + str(note), lang),
                    ParagraphStyle(
                        f"n{id(note)}", parent=base_style, leftIndent=12, spaceBefore=2
                    ),
                )
            )

    with _atomic_write(dest_path) as tmp:
        doc = SimpleDocTemplate(
            str(tmp),
            pagesize=A4,
            leftMargin=36,
            rightMargin=36,
            topMargin=36,
            bottomMargin=44,
            title=_title(result, lang),
            author=_station_name(conn, station_name, lang),
        )
        footer = partial(_pdf_footer, lang=lang)
        doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return dest_path


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------


def _xlsx_typed(value, kind: str):
    """Return ``(cell_value, number_format)`` for an XLSX cell.

    Money/quantity/price become real numerics (Decimal, never float-derived);
    dates become real dates; everything else stays text.
    """
    if value is None:
        return None, None
    if kind == "money":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValidationError(f"money value must be int paisa, got {value!r}")
        return from_paisa(value), '"PKR" #,##0.00'
    if kind == "quantity":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValidationError(f"quantity value must be int ml, got {value!r}")
        return from_ml(value), '#,##0.000'
    if kind == "price":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValidationError(f"price value must be int 0.0001-PKR units, got {value!r}")
        return from_price_10k(value), '#,##0.0000'
    if kind == "number":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValidationError(f"number value must be int, got {value!r}")
        return value, '#,##0'
    if kind == "date":
        try:
            return date.fromisoformat(str(value)), 'DD-MM-YYYY'
        except ValueError:
            return str(value), None
    return str(value), None


def export_report_xlsx(
    result: ReportResult,
    dest: str | os.PathLike,
    *,
    lang: str = "en",
    conn: sqlite3.Connection | None = None,
    station_name: str | None = None,
) -> Path:
    """Render *result* to an XLSX workbook at *dest*.

    Money/quantity/price/number/date cells are typed (numeric/date), never
    formatted strings. Urdu sheets get a right-to-left sheet view and
    mirrored column order. Returns the destination Path.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    dest_path = _validate(result, lang, dest)

    wb = Workbook()
    ws = wb.active
    ws.title = result.report_id[:31]
    if lang == "ur":
        ws.sheet_view.rightToLeft = True

    rtl = lang == "ur"
    base_align = Alignment(horizontal="right" if rtl else "left", vertical="top", wrap_text=True)
    header_font = Font(bold=True, size=11)
    title_font = Font(bold=True, size=14)
    header_fill = PatternFill("solid", fgColor="D9D9D9")
    totals_font = Font(bold=True)
    thin = Side(style="thin", color="888888")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    columns = _display_columns(result, lang)
    ncols = len(columns)

    def align_for(col: ReportColumn) -> Alignment:
        a = _cell_align(col, lang)
        return Alignment(
            horizontal={"left": "left", "right": "right", "center": "center"}[a],
            vertical="top",
            wrap_text=True,
        )

    row_idx = 1

    def header_block_row(text: str, font: Font, size_row: bool = False) -> None:
        nonlocal row_idx
        ws.merge_cells(start_row=row_idx, start_column=1, end_row=row_idx, end_column=ncols)
        c = ws.cell(row=row_idx, column=1, value=_shape(text, lang))
        c.font = font
        c.alignment = Alignment(horizontal="center" if not rtl else "center", vertical="center")
        row_idx += 1

    header_block_row(_station_name(conn, station_name, lang), title_font)
    header_block_row(_title(result, lang), Font(bold=True, size=12))
    period = _period_label(result, lang)
    if period:
        header_block_row(period, Font(size=10))
    meta_bits: list[str] = []
    if result.business_date:
        day_label = "کاروباری تاریخ" if lang == "ur" else "Business date"
        meta_bits.append(f"{day_label}: {_format_date(result.business_date)}")
    if result.closing_revision is not None:
        rev_label = "بندش نظرثانی" if lang == "ur" else "Closing revision"
        meta_bits.append(f"{rev_label}: {result.closing_revision}")
    if meta_bits:
        header_block_row("  |  ".join(meta_bits), Font(size=9))
    gen_label = "تیار کردہ" if lang == "ur" else "Generated"
    header_block_row(f"{gen_label}: {_format_generated(result.generated_at_utc)}", Font(size=9))
    row_idx += 1  # blank separator

    # table header
    for i, col in enumerate(columns):
        c = ws.cell(row=row_idx, column=i + 1, value=_shape(_label(col, lang), lang))
        c.font = header_font
        c.fill = header_fill
        c.alignment = align_for(col)
        c.border = border
    row_idx += 1

    # data rows
    for row in result.rows:
        for i, col in enumerate(columns):
            value, fmt = _xlsx_typed(row.get(col.key), col.kind)
            c = ws.cell(row=row_idx, column=i + 1, value=value)
            if fmt:
                c.number_format = fmt
            c.alignment = align_for(col)
            c.border = border
        row_idx += 1

    # totals row
    totals_key = _totals_label_column(columns)
    total_label = "کل" if lang == "ur" else "Total"
    for i, col in enumerate(columns):
        if col.key == totals_key:
            c = ws.cell(row=row_idx, column=i + 1, value=_shape(total_label, lang))
        elif col.kind in _TOTALLED_KINDS and col.key in result.totals:
            value, fmt = _xlsx_typed(result.totals[col.key], col.kind)
            c = ws.cell(row=row_idx, column=i + 1, value=value)
            if fmt:
                c.number_format = fmt
        else:
            c = ws.cell(row=row_idx, column=i + 1, value=None)
        c.font = totals_font
        c.alignment = align_for(col)
        c.border = border
    row_idx += 2  # blank separator

    # notes
    notes = result.notes_ur if lang == "ur" else result.notes_en
    if notes:
        notes_title = "نوٹس" if lang == "ur" else "Notes"
        ws.merge_cells(start_row=row_idx, start_column=1, end_row=row_idx, end_column=ncols)
        c = ws.cell(row=row_idx, column=1, value=_shape(notes_title, lang))
        c.font = Font(bold=True, size=11)
        row_idx += 1
        for note in notes:
            ws.merge_cells(start_row=row_idx, start_column=1, end_row=row_idx, end_column=ncols)
            c = ws.cell(row=row_idx, column=1, value=_shape("• " + str(note), lang))
            c.alignment = base_align
            row_idx += 1

    # column widths by kind
    from openpyxl.utils import get_column_letter

    width_for = {"text": 30, "money": 18, "quantity": 16, "price": 18, "date": 14, "number": 12}
    for i, col in enumerate(columns):
        ws.column_dimensions[get_column_letter(i + 1)].width = width_for[col.kind]

    with _atomic_write(dest_path) as tmp:
        wb.save(str(tmp))
    return dest_path


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def export_report_csv(
    result: ReportResult,
    dest: str | os.PathLike,
    *,
    lang: str = "en",
    conn: sqlite3.Connection | None = None,
    station_name: str | None = None,
) -> Path:
    """Render *result* to a UTF-8 (with BOM) CSV at *dest*.

    Tabular rows carry human-readable formatted values. Returns the
    destination Path.
    """
    dest_path = _validate(result, lang, dest)

    columns = _display_columns(result, lang)
    lines: list[list[str]] = []
    lines.append([_shape(_station_name(conn, station_name, lang), lang)])
    lines.append([_shape(_title(result, lang), lang)])
    period = _period_label(result, lang)
    if period:
        lines.append([_shape(period, lang)])
    meta_bits: list[str] = []
    if result.business_date:
        day_label = "کاروباری تاریخ" if lang == "ur" else "Business date"
        meta_bits.append(f"{day_label}: {_format_date(result.business_date)}")
    if result.closing_revision is not None:
        rev_label = "بندش نظرثانی" if lang == "ur" else "Closing revision"
        meta_bits.append(f"{rev_label}: {result.closing_revision}")
    if meta_bits:
        lines.append([_shape("  |  ".join(meta_bits), lang)])
    gen_label = "تیار کردہ" if lang == "ur" else "Generated"
    lines.append([_shape(f"{gen_label}: {_format_generated(result.generated_at_utc)}", lang)])
    lines.append([])
    lines.append([_shape(_label(c, lang), lang) for c in columns])
    for row in result.rows:
        lines.append([_shape(_format_value(row.get(c.key), c.kind), lang) for c in columns])
    totals_key = _totals_label_column(columns)
    total_label = "کل" if lang == "ur" else "Total"
    totals_cells = []
    for c in columns:
        if c.key == totals_key:
            totals_cells.append(_shape(total_label, lang))
        elif c.kind in _TOTALLED_KINDS and c.key in result.totals:
            totals_cells.append(_shape(_format_value(result.totals[c.key], c.kind), lang))
        else:
            totals_cells.append("")
    lines.append(totals_cells)
    notes = result.notes_ur if lang == "ur" else result.notes_en
    if notes:
        lines.append([])
        lines.append([_shape("نوٹس" if lang == "ur" else "Notes", lang)])
        for note in notes:
            lines.append([_shape("• " + str(note), lang)])

    with _atomic_write(dest_path) as tmp:
        with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerows(lines)
    return dest_path
