"""Timestamps with milliseconds must parse in both lifecycle phases."""
from __future__ import annotations

from datetime import date

import pytest

from t3ra_unified.completion.extractors import parse_date
from t3ra_unified.covered.domain.extractors import format_mmddyyyy

CASES = [
    ("2025-04-08T00:00:00Z", date(2025, 4, 8)),
    ("2025-04-08T00:00:00.000Z", date(2025, 4, 8)),
    ("2025-04-08T13:45:10.123Z", date(2025, 4, 8)),
    ("2025-04-08T13:45:10.12Z", date(2025, 4, 8)),  # 2-digit fraction: fromisoformat rejects on 3.10
    ("2025-04-08T13:45:10.123456789Z", date(2025, 4, 8)),
    ("2025-04-08T13:45:10+00:00", date(2025, 4, 8)),
]


@pytest.mark.parametrize("raw,expected", CASES)
def test_completion_parser_reads_every_shape(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw,expected", CASES)
def test_covered_formatter_no_longer_returns_none_for_milliseconds(raw, expected):
    assert format_mmddyyyy(raw) == expected.strftime("%m/%d/%Y")


@pytest.mark.parametrize("raw", [None, "", "not a date", "2025-13-45T00:00:00Z"])
def test_garbage_is_none(raw):
    assert parse_date(raw) is None
    assert format_mmddyyyy(raw) is None
