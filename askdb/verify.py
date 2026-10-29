"""Ground truth for a result set, and the check that a summary only quotes figures from it.

A figure in the summary is verified when it matches, within the rounding it is written with, a
number the data actually supports: a cell, a column statistic, a share of a column total, the row
count, or a number in the question or in a text cell (e.g. the 2025 in "2025-11").
"""

import math
import re
from dataclasses import dataclass, field
from typing import Any

_DATE = re.compile(r"\b\d{4}-\d{2}(?:-\d{2})?\b")
_LIST_MARKER = re.compile(r"^\s*(?:[-*]\s*)?\d+[.)]\s+", re.M)
_NUMBER = re.compile(
    r"""
    (?<![\w.])
    (?P<sign>[-−+])?
    [$€£]?
    (?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)
    (?P<suffix>\s?(?:%|percent\b|thousand\b|million\b|billion\b|bn\b|k\b|m\b|b\b))?
    (?!\w)
    """,
    re.X | re.I,
)
_SCALES = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}
_DISTINCT_CAP = 1_000


@dataclass(frozen=True)
class Figure:
    text: str  # as written, for messages
    value: float
    tolerance: float  # half a unit in the last written decimal place, at the written scale
    percent: bool


def extract_figures(text: str) -> list[Figure]:
    """Every number written in `text`, with the precision it was written at."""
    text = _LIST_MARKER.sub("", _DATE.sub(" ", text))
    figures = []
    for m in _NUMBER.finditer(text):
        raw = m.group("num").replace(",", "")
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        suffix = (m.group("suffix") or "").strip().lower()
        scale = _SCALES.get(suffix, 1.0)
        value = float(raw) * scale
        if m.group("sign") in ("-", "−"):
            value = -value
        figures.append(
            Figure(
                m.group(0).strip(), value, 0.5 * 10**-decimals * scale, suffix in ("%", "percent")
            )
        )
    return figures


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


@dataclass
class GroundTruth:
    """What a summary may quote: `stats` is shown to the model, `candidates` is the full check."""

    stats: dict[str, Any]
    candidates: set[float] = field(default_factory=set)
    shares: set[float] = field(default_factory=set)  # each cell as a percentage of its column sum

    @classmethod
    def from_result(
        cls,
        question: str,
        columns: list[str],
        rows: list[tuple[Any, ...]] | list[list[Any]],
        truncated: bool,
    ) -> "GroundTruth":
        candidates: set[float] = {float(len(rows)), float(len(columns))}
        shares: set[float] = set()
        candidates.update(f.value for f in extract_figures(question))
        column_stats = []
        for i, name in enumerate(columns):
            values = [r[i] for r in rows]
            nulls = sum(v is None for v in values)
            stat: dict[str, Any] = {"name": name, "nulls": nulls, "non_null": len(values) - nulls}
            nums = [float(v) for v in values if _is_number(v)]
            if nums and len(nums) == len(values) - nulls:
                total = math.fsum(nums)
                stat |= {
                    "numeric": True,
                    "sum": total,
                    "mean": total / len(nums),
                    "min": min(nums),
                    "max": max(nums),
                }
                candidates.update(nums)
                candidates.update(v for k, v in stat.items() if k in ("sum", "mean", "min", "max"))
                if total:
                    shares.update(n / total * 100 for n in nums)
            else:
                stat["numeric"] = False
                distinct = {v for v in values if v is not None}
                stat["distinct"] = len(distinct) if len(distinct) <= _DISTINCT_CAP else None
                for v in distinct:
                    if isinstance(v, str):
                        candidates.update(f.value for f in extract_figures(v))
                        candidates.update(
                            float(n) for n in re.findall(r"\d+", v) if len(n) < 16
                        )  # digits inside dates and codes, e.g. 2025 and 11 in "2025-11"
            candidates.update(float(stat[k]) for k in ("nulls", "non_null"))
            if stat.get("distinct") is not None:
                candidates.add(float(stat["distinct"]))
            column_stats.append(stat)
        stats = {
            "row_count": len(rows),
            "truncated": truncated,
            "columns": [_round_stat(s) for s in column_stats],
        }
        return cls(stats, candidates, shares)


def _round_stat(stat: dict[str, Any]) -> dict[str, Any]:
    return {k: round(v, 4) if isinstance(v, float) else v for k, v in stat.items()}


def _near(target: float, tol: float, pool: set[float]) -> bool:
    limit = tol + 1e-9 * max(1.0, abs(target))
    return any(abs(abs(c) - abs(target)) <= limit for c in pool)


def _supported(figure: Figure, truth: GroundTruth) -> bool:
    value, tol = figure.value, figure.tolerance
    if _near(value, tol, truth.candidates):
        return True
    if figure.percent:
        # "12.5%" may quote a fraction (0.125) or a share of a column total
        return _near(value / 100, tol / 100, truth.candidates) or _near(value, tol, truth.shares)
    # "0.253 of the total" is a share written as a fraction
    return abs(value) <= 1 and _near(value * 100, tol * 100, truth.shares)


def verify_summary(summary: str, truth: GroundTruth) -> list[str]:
    """Figures in `summary` that the data does not support (empty list means verified)."""
    return [f.text for f in extract_figures(summary) if not _supported(f, truth)]
