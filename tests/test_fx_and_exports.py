"""Trend maths and file generation, on synthetic data so no network is needed."""

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import exports, fx


def synthetic(days=400, columns=("USD/INR", "INR/GBP")):
    """USD/INR rises a paisa a day; INR/GBP is flat. Both answers are known."""
    start = date(2024, 1, 1)
    rows = []
    for i in range(days):
        rows.append(
            {
                "date": (start + timedelta(days=i)).isoformat(),
                "carried_forward": i % 7 in (5, 6),
                "USD/INR": 80.0 + 0.01 * i,
                "INR/GBP": 0.0095,
            }
        )
    return {
        "columns": list(columns),
        "rows": rows,
        "meta": {
            "source": "synthetic fixture",
            "published_days": days - (days // 7) * 2,
            "calendar_days": days,
            "carried_forward_days": (days // 7) * 2,
            "first_published": rows[0]["date"],
            "last_published": rows[-1]["date"],
            "caveat": "test fixture",
        },
    }


@pytest.mark.parametrize(
    "text,expected",
    [("USD/INR", ("USD", "INR")), ("inr-gbp", ("INR", "GBP")), ("usdjpy", ("USD", "JPY"))],
)
def test_pair_parsing_accepts_the_forms_people_type(text, expected):
    assert fx.parse_pair(text) == expected


@pytest.mark.parametrize("bad", ["USD", "rupees to pounds", "US/INR", ""])
def test_pair_parsing_rejects_nonsense(bad):
    with pytest.raises(ValueError):
        fx.parse_pair(bad)


def test_trendline_recovers_a_known_slope():
    series = synthetic()
    fit = fx.trendline([r["date"] for r in series["rows"]], [r["USD/INR"] for r in series["rows"]])
    assert abs(fit["slope_per_day"] - 0.01) < 1e-6
    assert abs(fit["slope_per_year"] - 3.6525) < 1e-3
    assert fit["r_squared"] > 0.999


def test_flat_series_has_zero_slope():
    series = synthetic()
    fit = fx.trendline([r["date"] for r in series["rows"]], [r["INR/GBP"] for r in series["rows"]])
    assert abs(fit["slope_per_year"]) < 1e-9


def test_trendline_refuses_a_series_too_short_to_fit():
    assert "error" in fx.trendline(["2024-01-01", "2024-01-02"], [80.0, 80.1])


def test_rebasing_makes_pairs_of_different_magnitude_comparable():
    """The reason the comparison chart plots an index: 88 and 0.0095 share no axis."""
    series = synthetic()
    inr = fx.rebase_to_100([r["USD/INR"] for r in series["rows"]])
    gbp = fx.rebase_to_100([r["INR/GBP"] for r in series["rows"]])
    assert inr[0] == 100 and gbp[0] == 100
    assert inr[-1] > 100 and gbp[-1] == 100


def test_moving_average_stays_empty_until_the_window_fills():
    averaged = fx.moving_average([float(i) for i in range(40)], window=30)
    assert averaged[28] is None
    assert averaged[29] == 14.5


def test_monthly_average_buckets_by_calendar_month():
    """Assertion updated with the behaviour, deliberately. It expected 31, the
    whole of January, because the average ran over calendar days. It now runs over
    published days, and January 2024 has 23 weekdays. The bucketing by month, which
    is what this test is named for, is unchanged.
    """
    months = fx.monthly_average(synthetic()["rows"], "USD/INR")
    assert months[0]["month"] == "2024-01"
    assert months[0]["days"] == 23, "published days, not calendar days"


def test_summarise_covers_every_column_in_both_forms():
    series = synthetic()
    plain = fx.summarise(series)
    indexed = fx.summarise(series, indexed=True)
    assert set(plain) == set(series["columns"])
    assert indexed["USD/INR"]["observed_start"]["value"] == 100


def test_gap_filling_carries_the_last_published_rate_forward():
    by_column = {"USD/INR": {"2024-01-01": 83.0, "2024-01-04": 83.5}}
    built = fx._build(by_column, ["USD/INR"])
    assert [r["date"] for r in built["rows"]] == [
        "2024-01-01",
        "2024-01-02",
        "2024-01-03",
        "2024-01-04",
    ]
    assert built["rows"][1]["USD/INR"] == 83.0
    assert built["rows"][1]["carried_forward"] is True
    assert built["meta"]["carried_forward_days"] == 2
    assert built["meta"]["published_days"] == 2


def test_workbook_ships_formulas_not_computed_answers():
    from openpyxl import load_workbook

    path = exports.build_workbook(synthetic(60), filename_prefix="test-fx")
    assert path.exists() and path.stat().st_size > 5000

    workbook = load_workbook(path)
    assert workbook.sheetnames == ["Rates", "Monthly", "Methodology"]
    sheet = workbook["Rates"]
    assert str(sheet["E5"].value).startswith("=SLOPE(")
    assert str(sheet["C5"].value).startswith("=")
    assert "USD/INR rate" in sheet["B1"].value
    path.unlink()


def test_workbook_widens_for_a_three_pair_comparison():
    from openpyxl import load_workbook

    series = synthetic(60, columns=("USD/INR", "INR/GBP"))
    series["rows"] = [{**r, "INR/EUR": 0.0111} for r in series["rows"]]
    series["columns"].append("INR/EUR")

    path = exports.build_workbook(series, filename_prefix="test-three")
    sheet = load_workbook(path)["Rates"]
    assert sheet["J1"].value.startswith("INR/EUR")
    path.unlink()


def test_every_chart_type_renders():
    series = synthetic(120)
    for kind in exports.CHART_TYPES:
        path = exports.render_chart(series, chart_type=kind, filename_prefix=f"test-{kind}")
        assert path.exists() and path.stat().st_size > 8000, kind
        path.unlink()


def test_document_embeds_a_chart_and_a_table():
    from docx import Document

    series = synthetic(90)
    chart = exports.render_chart(series, chart_type="indexed", filename_prefix="test-chart")
    path = exports.build_document(series, charts=[chart], filename_prefix="test-report")

    document = Document(str(path))
    assert len(document.tables) == 1
    assert len(document.tables[0].rows) == 4
    assert document.tables[0].rows[0].cells[1].text == "USD/INR"
    assert any("Method and limits" in p.text for p in document.paragraphs)
    chart.unlink()
    path.unlink()


def test_filenames_cannot_escape_the_output_directory():
    path = exports._stamp("../../etc/passwd", "png")
    assert path.parent == exports.OUTPUT_DIR


def test_output_pruning_respects_the_file_cap(monkeypatch):
    """Generated files accumulate on disk; without pruning the volume fills."""
    monkeypatch.setattr(exports, "MAX_FILES", 3)
    made = []
    for index in range(6):
        path = exports.OUTPUT_DIR / f"prune-test-{index}.txt"
        path.write_text("x")
        made.append(path)

    exports.prune_outputs()
    surviving = [p for p in made if p.exists()]
    assert len(surviving) <= 3
    for path in surviving:
        path.unlink()


def test_pruning_never_touches_the_gitkeep_marker(monkeypatch):
    monkeypatch.setattr(exports, "MAX_FILES", 0)
    marker = exports.OUTPUT_DIR / ".gitkeep"
    marker.touch()
    exports.prune_outputs()
    assert marker.exists()


# ---- published days versus calendar days --------------------------------


def _series_with_weekends():
    from datetime import date, timedelta

    rows, day = [], date(2026, 1, 1)
    for index in range(60):
        rows.append(
            {
                "date": day.isoformat(),
                "carried_forward": day.weekday() >= 5,
                "USD/INR": 83.0 + index * 0.01,
            }
        )
        day += timedelta(days=1)
    return rows


def test_monthly_average_ignores_carried_forward_rows():
    """The ECB publishes on working days and the series carries the last rate
    forward so charts have a continuous axis. Averaging those counts every Friday
    three times, and a calendar-day mean labelled "average" is a quietly wrong
    number in a deliverable."""
    from core import fx

    rows = [
        {"date": "2026-01-01", "carried_forward": False, "USD/INR": 80.0},
        {"date": "2026-01-02", "carried_forward": False, "USD/INR": 90.0},
        {"date": "2026-01-03", "carried_forward": True, "USD/INR": 90.0},
        {"date": "2026-01-04", "carried_forward": True, "USD/INR": 90.0},
    ]

    got = fx.monthly_average(rows, "USD/INR")
    assert got == [{"month": "2026-01", "average": 85.0, "days": 2}]


def test_the_workbook_average_excludes_carried_rows_too():
    """The spreadsheet recomputes this in Excel, so the formula needs the same
    exclusion or the file disagrees with the answer that produced it."""
    import openpyxl

    from core import exports

    series = {
        "columns": ["USD/INR"],
        "rows": _series_with_weekends(),
        "meta": {
            "source": "test",
            "published_days": 44,
            "calendar_days": 60,
            "carried_forward_days": 16,
            "first_published": "2026-01-01",
            "last_published": "2026-03-01",
            "caveat": "test",
        },
    }
    book = openpyxl.load_workbook(exports.build_workbook(series))
    rates = book["Rates"]
    flag_column = rates.cell(row=1, column=rates.max_column)

    assert flag_column.value == "Carried forward"
    formula = next(
        book["Monthly"].cell(row=r, column=3).value
        for r in range(2, 6)
        if book["Monthly"].cell(row=r, column=3).value
    )
    assert formula.count(",") >= 6, "a third criterion pair should be present"
    assert '""' in formula, "it should match rows where the flag is empty"


def test_a_duplicated_pair_does_not_silently_empty_the_series():
    """The completeness check compared a dict of carried values against the column
    list, so a repeated column made the lengths permanently unequal and the series
    came back with no rows and no error."""
    from core import fx

    built = fx._build({"USD/INR": {"2026-01-01": 80.0, "2026-01-02": 90.0}}, ["USD/INR", "USD/INR"])

    assert len(built["rows"]) == 2
    assert built["columns"] == ["USD/INR"]


def test_first_published_describes_a_row_that_exists():
    """Leading days are skipped until every column has a value, so with one pair
    starting later the reported start date was absent from the table under it."""
    from core import fx

    built = fx._build(
        {
            "USD/INR": {"2026-01-01": 80.0, "2026-01-02": 81.0, "2026-01-03": 82.0},
            "USD/GBP": {"2026-01-03": 0.9},
        },
        ["USD/INR", "USD/GBP"],
    )

    assert built["rows"][0]["date"] == built["meta"]["first_published"]
    assert built["meta"]["first_published_any_column"] == "2026-01-01"
