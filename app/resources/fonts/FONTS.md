# FONTS.md — bundled fonts for the export engine

The export engine (`app/services/report_export.py`) must render Urdu on a
Windows installer target that may have **no Urdu-capable system fonts**, so
the fonts are bundled here and registered with ReportLab at runtime. System
fonts are never used.

## Files

- `NotoSansArabic-Regular.ttf` — Noto Sans Arabic, weight 400 (static instance)
- `NotoSansArabic-Bold.ttf`    — Noto Sans Arabic, weight 700 (static instance)
- `OFL.txt`                    — SIL Open Font License 1.1 (full text)

## Provenance

Source: Google Fonts (`family=Noto Sans Arabic`), weight-specific static
TrueType files served by `fonts.gstatic.com` (v33), downloaded 2026-10-15.

Upstream project: https://github.com/notofonts/arabic
License: SIL Open Font License, Version 1.1 — see `OFL.txt`.

## Coverage

Both files were verified with fontTools to contain every Urdu-specific code
point used by the app (ھ U+06BE, ہ U+06C1, ڈ U+0688, ڑ U+0691, پ U+067E,
ٹ U+0679, چ U+0686, ژ U+0698, ک U+06A9, گ U+06AF, ں U+06BA, ے U+06D2)
plus ASCII space, digits and Latin — so English reports render with the same
family and mixed strings (e.g. "PKR 1,234.56" inside Urdu text) stay in one
font.

## RTL approach (summary)

ReportLab has no complex-text layout: Urdu strings are reshaped with
`arabic_reshaper` (presentation forms) and visually reordered with
`python-bidi` (`get_display`) *before* drawing. Western digits are kept as-is
(the paper register uses Western digits). In Urdu mode the table column order
is mirrored and XLSX sheets set `rightToLeft`. See the module docstring of
`app/services/report_export.py` for the full description.
