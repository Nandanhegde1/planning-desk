# Sample output

**Generated from a synthetic fixture, not from live exchange rates.** The
underlying series are straight lines, which is why each trendline sits exactly on
its data. They exist so the shape of the deliverable can be inspected without
running the app or reaching an API.

Real output is written to `outputs/` at runtime and served at `/files/<name>`.

- `SYNTHETIC-three-currency-workbook.xlsx` — Rates, Monthly and Methodology
  sheets, live SLOPE and INTERCEPT formulas, native comparison chart
- `SYNTHETIC-comparison-chart.png` — USD/INR, INR/GBP and INR/EUR indexed to 100,
  which is the only fair way to share one axis across those magnitudes
- `SYNTHETIC-monthly-bar.png` — the alternate chart shape
- `SYNTHETIC-report.docx` — trend paragraphs, embedded charts, monthly table,
  method and limits
