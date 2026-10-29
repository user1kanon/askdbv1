import pytest

from askdb.verify import GroundTruth, extract_figures, verify_summary

COLUMNS = ["plan", "customers", "mrr", "ended"]
ROWS = [
    ("Starter", 120, 3480.5, None),
    ("Business", 45, 6705.0, "2025-11"),
    ("Enterprise", 12, 3588.0, None),
]


def truth(question="mrr by plan", columns=COLUMNS, rows=ROWS, truncated=False):
    return GroundTruth.from_result(question, columns, rows, truncated)


def bad(summary, **kw):
    return verify_summary(summary, truth(**kw))


# --- extraction -------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "value", "tolerance"),
    [
        ("1,234", 1234, 0.5),
        ("1,234,567.89", 1234567.89, 0.005),
        ("12.5%", 12.5, 0.05),
        ("$1.2M", 1_200_000, 50_000),
        ("1.2 million", 1_200_000, 50_000),
        ("3k", 3000, 500),
        ("-5", -5, 0.5),
        (".5", 0.5, 0.05),
    ],
)
def test_extract_value_and_precision(text, value, tolerance):
    (fig,) = extract_figures(f"it was {text} overall")
    assert fig.value == pytest.approx(value)
    assert fig.tolerance == pytest.approx(tolerance)


def test_percent_flag_and_multiple_figures():
    figs = extract_figures("Up 12.5% to 1,200 from 1,000.")
    assert [f.value for f in figs] == [12.5, 1200, 1000]
    assert [f.percent for f in figs] == [True, False, False]


@pytest.mark.parametrize(
    "text",
    [
        "In 2025-11 and 2025-11-03",
        "Q2 and H1 results",
        "the 3rd plan and 1st place",
        "see column mrr_2",
        "version v2.1",
        "12kg of it",
        "1. first point\n2. second point",
    ],
)
def test_non_figures_are_not_extracted(text):
    assert extract_figures(text) == []


def test_year_range_gives_both_years():
    assert [f.value for f in extract_figures("from 2024-2025")] == [2024, 2025]


# --- verification -----------------------------------------------------------


@pytest.mark.parametrize(
    "summary",
    [
        "Starter has 120 customers.",
        "Business MRR is 6,705 and Enterprise is $3,588.00.",
        "Starter MRR is 3,480.5, or about 3,481 rounded.",  # rounding to written precision
        "Starter MRR is 3,480.50.",
        "Total MRR is 13,773.5 across 3 plans.",  # sum and row count
        "The average is 4,591.17.",  # mean 4591.1666
        "The largest is 6,705 and the smallest 3,480.5.",
        "Starter is 25.3% of MRR.",  # share of total: 3480.5 / 13773.5
        "Starter is 25% of MRR.",
        "Customers total 177.",
        "Enterprise has the fewest customers at 12 (value from the data).",
        "One plan ended in 2025-11.",
        "MRR is 6.7 thousand for Business.",  # scale suffix, 6705 within 0.05k
        "That is 0.253 of the total, i.e. 25.3%.",
        "Two plans have no end date.",  # words are not figures
    ],
)
def test_supported_figures_pass(summary):
    assert bad(summary) == []


@pytest.mark.parametrize(
    ("summary", "unsupported"),
    [
        ("Starter has 121 customers.", ["121"]),
        ("Business MRR is 6,750.", ["6,750"]),
        ("MRR grew 40% last year.", ["40%"]),
        ("Business has 45 customers and 99 churned.", ["99"]),
        ("Starter MRR is 3,480.51.", ["3,480.51"]),  # more precise than the data
        ("About 3,500 for Starter.", ["3,500"]),  # rounding to hundreds is not allowed
        ("Revenue was $9.9M.", ["$9.9M"]),
        ("Starter is 30% of MRR.", ["30%"]),
    ],
)
def test_unsupported_figures_are_reported(summary, unsupported):
    assert bad(summary) == unsupported


def test_numbers_from_the_question_are_allowed():
    t = truth(question="Show the top 5 plans in 2025", rows=ROWS)
    assert verify_summary("The top 5 plans in 2025 are below.", t) == []
    assert verify_summary("The top 7 plans in 2025 are below.", t) == ["7"]


def test_negative_figures_match_by_magnitude():
    t = truth(columns=["delta"], rows=[(-250.0,), (100.0,)])
    assert verify_summary("Revenue fell by 250.", t) == []
    assert verify_summary("Revenue changed by -250.", t) == []


def test_fraction_column_matches_percent_in_text():
    t = truth(columns=["rate"], rows=[(0.125,), (0.5,)])
    assert verify_summary("The churn rate is 12.5%.", t) == []
    assert verify_summary("The churn rate is 13.5%.", t) == ["13.5%"]


def test_null_and_distinct_counts_are_supported():
    assert bad("Two rows have no end date.") == []
    assert bad("2 of the rows have NULL ended.") == []
    assert bad("There are 3 distinct plans.") == []


def test_stats_shown_to_the_model():
    stats = truth(truncated=True).stats
    assert stats["row_count"] == 3 and stats["truncated"] is True
    by_name = {c["name"]: c for c in stats["columns"]}
    assert by_name["mrr"]["sum"] == 13773.5 and by_name["mrr"]["max"] == 6705.0
    assert by_name["plan"]["numeric"] is False and by_name["plan"]["distinct"] == 3
    assert by_name["ended"]["nulls"] == 2


def test_empty_result_supports_only_zero():
    t = truth(rows=[])
    assert verify_summary("No rows came back.", t) == []
    assert verify_summary("0 rows matched.", t) == []
    assert verify_summary("5 rows matched.", t) == ["5"]
