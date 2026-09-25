"""Spreadsheet, Word and chart generation."""

from __future__ import annotations

import os
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt
from openpyxl import Workbook
from openpyxl.chart import LineChart, Reference
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from core import fx

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)

RETENTION_HOURS = float(os.environ.get("OUTPUT_RETENTION_HOURS", "6"))
MAX_FILES = int(os.environ.get("MAX_OUTPUT_FILES", "200"))

BODY_FONT = "Arial"
HEADER_FILL = PatternFill("solid", fgColor="1F3A5F")
HEADER_FONT = Font(name=BODY_FONT, bold=True, color="FFFFFF", size=10)
SOURCED_FONT = Font(name=BODY_FONT, color="0000FF", size=10)
FORMULA_FONT = Font(name=BODY_FONT, color="000000", size=10)

PALETTE = ["#1f4e79", "#b5651d", "#2e6f47", "#7b3f9d", "#a33a3a"]
CHART_TYPES = ["indexed", "line", "trend", "monthly_bar", "small_multiples"]


def prune_outputs() -> int:
    """Delete generated files past their age or count limit. Runs before each
    write, so the output volume cannot fill."""
    try:
        files = sorted(
            (f for f in OUTPUT_DIR.iterdir() if f.is_file() and f.name != ".gitkeep"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return 0

    cutoff = time.time() - RETENTION_HOURS * 3600
    removed = 0
    for index, path in enumerate(files):
        try:
            if index >= MAX_FILES or path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _stamp(prefix: str, extension: str) -> Path:
    prune_outputs()
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in prefix)
    return OUTPUT_DIR / f"{safe}-{datetime.now():%Y%m%d-%H%M%S-%f}.{extension}"


def _label(series: dict[str, Any]) -> str:
    return ", ".join(series["columns"])


# ---------------------------------------------------------------------------
# Spreadsheet
# ---------------------------------------------------------------------------


def build_workbook(series: dict[str, Any], *, filename_prefix: str = "fx-rates") -> Path:
    columns = series["columns"]
    rows = series["rows"]
    last = len(rows) + 1

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Rates"

    headers = ["Date"]
    for column in columns:
        headers += [
            f"{column} rate",
            f"{column} index (start=100)",
            f"{column} 30d average",
            f"{column} trend",
        ]
    headers.append("Carried forward")

    for index, label in enumerate(headers, start=1):
        cell = sheet.cell(row=1, column=index, value=label)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    sheet.row_dimensions[1].height = 30
    sheet.freeze_panes = "B2"

    for row_index, row in enumerate(rows, start=2):
        date_cell = sheet.cell(row=row_index, column=1, value=date.fromisoformat(row["date"]))
        date_cell.number_format = "yyyy-mm-dd"
        date_cell.font = SOURCED_FONT

        for offset, column in enumerate(columns):
            first = 2 + offset * 4
            letter = get_column_letter(first)

            rate = sheet.cell(row=row_index, column=first, value=row.get(column))
            rate.number_format = "0.000000"
            rate.font = SOURCED_FONT

            indexed = sheet.cell(
                row=row_index, column=first + 1, value=f"={letter}{row_index}/{letter}$2*100"
            )
            indexed.number_format = "0.0"
            indexed.font = FORMULA_FONT

            average = sheet.cell(
                row=row_index,
                column=first + 2,
                value=(
                    f"=AVERAGE({letter}{row_index - 29}:{letter}{row_index})"
                    if row_index >= 31
                    else '=""'
                ),
            )
            average.number_format = "0.000000"
            average.font = FORMULA_FONT

            # Live least-squares fit. Excel stores dates as serial numbers, so
            # SLOPE and INTERCEPT work against column A directly.
            trend = sheet.cell(
                row=row_index,
                column=first + 3,
                value=(
                    f"=SLOPE({letter}$2:{letter}${last},$A$2:$A${last})*$A{row_index}"
                    f"+INTERCEPT({letter}$2:{letter}${last},$A$2:$A${last})"
                ),
            )
            trend.number_format = "0.000000"
            trend.font = FORMULA_FONT

        flag = sheet.cell(
            row=row_index, column=len(headers), value="yes" if row.get("carried_forward") else ""
        )
        flag.font = FORMULA_FONT

    sheet.column_dimensions["A"].width = 12
    for index in range(2, len(headers) + 1):
        sheet.column_dimensions[get_column_letter(index)].width = 20

    chart = LineChart()
    chart.title = f"{_label(series)} indexed to 100 at series start"
    chart.y_axis.title = "Index"
    chart.x_axis.title = "Date"
    chart.height, chart.width = 9, 24
    for offset in range(len(columns)):
        chart.add_data(
            Reference(sheet, min_col=3 + offset * 4, min_row=1, max_row=last),
            titles_from_data=True,
        )
    chart.set_categories(Reference(sheet, min_col=1, min_row=2, max_row=last))
    sheet.add_chart(chart, f"{get_column_letter(len(headers) + 2)}2")

    _add_monthly_sheet(workbook, series, last)
    _add_methodology_sheet(workbook, series)

    path = _stamp(filename_prefix, "xlsx")
    workbook.save(path)
    return path


def _add_monthly_sheet(workbook: Workbook, series: dict[str, Any], last_row: int) -> None:
    sheet = workbook.create_sheet("Monthly")
    columns = series["columns"]

    headers = ["Month start", "Month end"] + [f"{c} average" for c in columns]
    for index, label in enumerate(headers, start=1):
        cell = sheet.cell(row=1, column=index, value=label)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL

    months = sorted({row["date"][:7] for row in series["rows"]})
    for row_index, month in enumerate(months, start=2):
        year, mon = int(month[:4]), int(month[5:7])
        start = date(year, mon, 1)
        end = date(year + (mon == 12), (mon % 12) + 1, 1)

        for offset, value in enumerate((start, end)):
            cell = sheet.cell(row=row_index, column=1 + offset, value=value)
            cell.number_format = "yyyy-mm-dd"
            cell.font = SOURCED_FONT

        # The carried-forward flag is the last column of the Rates sheet.
        flag_letter = get_column_letter(1 + len(columns) * 4 + 1)
        for offset in range(len(columns)):
            letter = get_column_letter(2 + offset * 4)
            cell = sheet.cell(
                row=row_index,
                column=3 + offset,
                value=(
                    # The third criterion excludes carried-forward rows. Without it
                    # the workbook averages calendar days, so every Friday is
                    # counted three times and the figure is not a monthly average
                    # in the sense a central bank publishes one.
                    f"=AVERAGEIFS(Rates!{letter}$2:{letter}${last_row},"
                    f'Rates!$A$2:$A${last_row},">="&$A{row_index},'
                    f'Rates!$A$2:$A${last_row},"<"&$B{row_index},'
                    f'Rates!${flag_letter}$2:${flag_letter}${last_row},"")'
                ),
            )
            cell.number_format = "0.000000"
            cell.font = FORMULA_FONT

    for index in range(1, len(headers) + 1):
        sheet.column_dimensions[get_column_letter(index)].width = 20


def _add_methodology_sheet(workbook: Workbook, series: dict[str, Any]) -> None:
    sheet = workbook.create_sheet("Methodology")
    meta = series["meta"]
    facts = [
        ("Source", meta["source"]),
        ("Retrieved", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("Pairs", _label(series)),
        ("First published rate", meta["first_published"]),
        ("Last published rate", meta["last_published"]),
        ("Published business days", meta["published_days"]),
        ("Calendar days in sheet", meta["calendar_days"]),
        ("Days carried forward", meta["carried_forward_days"]),
        ("Cross-rate caveat", meta["caveat"]),
        (
            "Gap handling",
            "The ECB publishes on business days only. Weekend and holiday rows carry the "
            "previous published rate forward and are flagged in the Rates sheet.",
        ),
        (
            "Trend method",
            "Ordinary least squares on rate against date serial, computed by SLOPE and "
            "INTERCEPT so the fit updates if rows are added or removed.",
        ),
        (
            "Index method",
            "Each rate divided by the first rate in the series, times 100. Used for the "
            "comparison chart because the pairs differ by orders of magnitude.",
        ),
    ]

    sheet.cell(row=1, column=1, value="How this workbook was built").font = Font(
        name=BODY_FONT, bold=True, size=12
    )
    for index, (label, value) in enumerate(facts, start=3):
        key = sheet.cell(row=index, column=1, value=label)
        key.font = Font(name=BODY_FONT, bold=True, size=10)
        cell = sheet.cell(row=index, column=2, value=str(value))
        cell.font = Font(name=BODY_FONT, size=10)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    sheet.column_dimensions["A"].width = 26
    sheet.column_dimensions["B"].width = 92


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------


def render_chart(
    series: dict[str, Any],
    *,
    chart_type: str = "indexed",
    title: str | None = None,
    filename_prefix: str = "fx-chart",
) -> Path:
    # matplotlib costs about 60 MB resident and is only needed when drawing, so
    # it is imported here rather than at module load.
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = series["rows"]
    columns = series["columns"]
    dates = [date.fromisoformat(r["date"]) for r in rows]

    plt.rcParams.update(
        {
            "font.size": 10,
            "axes.edgecolor": "#8a8f98",
            "axes.labelcolor": "#333333",
            "axes.grid": True,
            "grid.color": "#e4e7ea",
            "grid.linewidth": 0.8,
            "figure.facecolor": "white",
        }
    )

    if chart_type == "small_multiples":
        figure, axes = plt.subplots(len(columns), 1, figsize=(11, 2.6 * len(columns)), sharex=True)
        axes = axes if len(columns) > 1 else [axes]
        for axis, column, colour in zip(axes, columns, PALETTE, strict=False):
            axis.plot(dates, [r.get(column) for r in rows], color=colour, linewidth=1.4)
            axis.set_ylabel(column)
            axis.spines[["top", "right"]].set_visible(False)
        axes[-1].set_xlabel("Date")
        figure.suptitle(title or "Each pair on its own scale", y=0.99)

    elif chart_type == "monthly_bar":
        figure, axis = plt.subplots(figsize=(11, 5))
        width = 0.8 / len(columns)
        labels = [m["month"] for m in fx.monthly_average(rows, columns[0])]
        for offset, (column, colour) in enumerate(zip(columns, PALETTE, strict=False)):
            monthly = fx.monthly_average(rows, column)
            first = monthly[0]["average"] if monthly else 1
            axis.bar(
                [i + offset * width for i in range(len(monthly))],
                [(m["average"] / first - 1) * 100 for m in monthly],
                width=width,
                label=column,
                color=colour,
            )
        step = max(1, len(labels) // 12)
        axis.set_xticks(range(0, len(labels), step))
        axis.set_xticklabels(labels[::step], rotation=45, ha="right")
        axis.set_ylabel("% change vs first month")
        axis.axhline(0, color="#333333", linewidth=0.9)
        axis.legend(frameon=False)
        axis.set_title(title or "Monthly average movement")
        axis.spines[["top", "right"]].set_visible(False)

    elif chart_type == "trend":
        figure, axis = plt.subplots(figsize=(11, 5))
        for column, colour in zip(columns, PALETTE, strict=False):
            values = [r.get(column) for r in rows]
            axis.plot(dates, values, color=colour, linewidth=1.3, label=column)
            fit = fx.trendline([r["date"] for r in rows], values)
            if "error" not in fit:
                axis.plot(
                    [dates[0], dates[-1]],
                    [fit["fit_start"]["value"], fit["fit_end"]["value"]],
                    color=colour,
                    linestyle="--",
                    linewidth=1.1,
                    label=(
                        f"{column} trend {fit['slope_per_year']:+.4f}/yr, R2 {fit['r_squared']:.2f}"
                    ),
                )
        axis.set_ylabel("Rate")
        axis.legend(frameon=False, fontsize=8)
        axis.set_title(title or "Rates with fitted trend")
        axis.spines[["top", "right"]].set_visible(False)

    elif chart_type == "line":
        figure, axis = plt.subplots(figsize=(11, 5))
        for column, colour in zip(columns, PALETTE, strict=False):
            axis.plot(
                dates, [r.get(column) for r in rows], color=colour, linewidth=1.4, label=column
            )
        axis.set_ylabel("Rate")
        axis.legend(frameon=False)
        axis.set_title(title or _label(series))
        axis.spines[["top", "right"]].set_visible(False)

    else:
        figure, axis = plt.subplots(figsize=(11, 5))
        for column, colour in zip(columns, PALETTE, strict=False):
            indexed = fx.rebase_to_100([r.get(column) for r in rows])
            axis.plot(dates, indexed, color=colour, linewidth=1.5, label=column)
            fit = fx.trendline([r["date"] for r in rows], indexed)
            if "error" not in fit:
                axis.plot(
                    [dates[0], dates[-1]],
                    [fit["fit_start"]["value"], fit["fit_end"]["value"]],
                    color=colour,
                    linestyle="--",
                    linewidth=1.0,
                )
        axis.axhline(100, color="#8a8f98", linewidth=0.9, linestyle=":")
        axis.set_ylabel("Index, start = 100")
        axis.legend(frameon=False)
        axis.set_title(title or f"{_label(series)} indexed with trend")
        axis.spines[["top", "right"]].set_visible(False)

    figure.text(
        0.01,
        0.01,
        "Source: ECB reference rates via Frankfurter. Non-EUR pairs are crosses through EUR.",
        fontsize=7,
        color="#6b7280",
    )
    figure.tight_layout(rect=(0, 0.03, 1, 1))
    path = _stamp(filename_prefix, "png")
    figure.savefig(path, dpi=150)
    plt.close(figure)
    return path


# ---------------------------------------------------------------------------
# Word document
# ---------------------------------------------------------------------------


def build_document(
    series: dict[str, Any],
    *,
    charts: list[Path] | None = None,
    filename_prefix: str = "fx-report",
) -> Path:
    columns = series["columns"]
    meta = series["meta"]
    stats = fx.summarise(series)

    document = Document()
    normal = document.styles["Normal"]
    normal.font.name = BODY_FONT
    normal.font.size = Pt(10.5)

    document.add_heading(
        f"Exchange rates: {meta['first_published']} to {meta['last_published']}", level=0
    )
    document.add_paragraph(
        f"Daily rates for {_label(series)}, with a least-squares trend for each pair. "
        f"Generated {datetime.now():%d %B %Y at %H:%M}."
    )

    document.add_heading("What the trend shows", level=1)
    for column in columns:
        fit = stats.get(column, {})
        if "error" in fit:
            continue
        base = column.split("/")[0]
        direction = "strengthening" if fit["slope_per_year"] < 0 else "weakening"
        paragraph = document.add_paragraph()
        paragraph.add_run(f"{column}. ").bold = True
        paragraph.add_run(
            f"Moved from {fit['observed_start']['value']} to {fit['observed_end']['value']}, "
            f"a change of {fit['change_pct']}%. The fitted line slopes "
            f"{fit['slope_per_year']:+.4f} per year with an R-squared of {fit['r_squared']}, "
            f"so {base} is {direction} over this window. Range "
            f"{fit['min']['value']} on {fit['min']['date']} to "
            f"{fit['max']['value']} on {fit['max']['date']}."
        )

    for chart in charts or []:
        document.add_picture(str(chart), width=Inches(6.4))
        document.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER

    document.add_heading("Monthly averages", level=1)
    per_column = {
        c: {m["month"]: m["average"] for m in fx.monthly_average(series["rows"], c)}
        for c in columns
    }
    table = document.add_table(rows=1, cols=1 + len(columns))
    table.style = "Light Grid Accent 1"
    header = table.rows[0].cells
    header[0].text = "Month"
    for index, column in enumerate(columns, start=1):
        header[index].text = column
    for month in fx.monthly_average(series["rows"], columns[0]):
        cells = table.add_row().cells
        cells[0].text = month["month"]
        for index, column in enumerate(columns, start=1):
            value = per_column[column].get(month["month"])
            cells[index].text = f"{value:.6f}".rstrip("0").rstrip(".") if value is not None else "-"

    document.add_heading("Method and limits", level=1)
    for line in [
        f"Source: {meta['source']}.",
        meta["caveat"],
        f"The window holds {meta['published_days']} published business days across "
        f"{meta['calendar_days']} calendar days. The {meta['carried_forward_days']} non-publishing "
        "days carry the previous rate forward so the horizontal axis stays a real calendar.",
        "The trend is ordinary least squares on rate against date. It describes the window it was "
        "fitted to and is not a forecast.",
        "Where pairs are compared, charts plot an index set to 100 at the series start, because "
        "the underlying rates differ by orders of magnitude and would not share an axis.",
    ]:
        document.add_paragraph(line, style="List Bullet")

    path = _stamp(filename_prefix, "docx")
    document.save(path)
    return path
