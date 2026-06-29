"""Tests for ParquetCache: roundtrip (Decimal-exact), missing-range computation,
content hashing, partitioning, and de-duplication (M2.2)."""

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from trader.data.cache import BAR_COLUMNS, ParquetCache


def _d(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


def _bars(rows: list[tuple[datetime, str, str, str, str, int]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts": [r[0] for r in rows],
            "open": [Decimal(r[1]) for r in rows],
            "high": [Decimal(r[2]) for r in rows],
            "low": [Decimal(r[3]) for r in rows],
            "close": [Decimal(r[4]) for r in rows],
            "volume": [r[5] for r in rows],
        }
    )


JAN = _bars(
    [
        (_d(2023, 1, 3), "10", "11", "9", "10.50", 1000),
        (_d(2023, 1, 4), "10.50", "12", "10", "11.25", 2000),
    ]
)


def test_write_read_roundtrip(tmp_path: Path) -> None:
    cache = ParquetCache(tmp_path)
    cache.write_bars("AAPL", JAN)
    got = cache.read_bars("AAPL", _d(2023, 1, 1), _d(2023, 1, 31))

    assert len(got) == 2
    assert got["close"].tolist() == [Decimal("10.50"), Decimal("11.25")]
    assert isinstance(got["close"].iloc[0], Decimal)  # exact, not float
    assert got["volume"].tolist() == [1000, 2000]
    assert got["ts"].tolist() == [pd.Timestamp(_d(2023, 1, 3)), pd.Timestamp(_d(2023, 1, 4))]


def test_decimal_trailing_zero_preserved(tmp_path: Path) -> None:
    cache = ParquetCache(tmp_path)
    cache.write_bars("AAPL", _bars([(_d(2023, 1, 3), "10.10", "10.10", "10.10", "10.10", 1)]))
    got = cache.read_bars("AAPL", _d(2023, 1, 1), _d(2023, 1, 31))
    assert str(got["close"].iloc[0]) == "10.10"  # exponent preserved through str storage


def test_read_unknown_symbol_returns_empty(tmp_path: Path) -> None:
    cache = ParquetCache(tmp_path)
    got = cache.read_bars("NOPE", _d(2023, 1, 1), _d(2023, 12, 31))
    assert list(got.columns) == list(BAR_COLUMNS)
    assert got.empty


def test_read_filters_to_range(tmp_path: Path) -> None:
    cache = ParquetCache(tmp_path)
    cache.write_bars("AAPL", JAN)
    got = cache.read_bars("AAPL", _d(2023, 1, 4), _d(2023, 1, 4))
    assert got["ts"].tolist() == [pd.Timestamp(_d(2023, 1, 4))]


def test_cross_year_partitioning(tmp_path: Path) -> None:
    cache = ParquetCache(tmp_path)
    cache.write_bars(
        "AAPL",
        _bars(
            [
                (_d(2022, 12, 30), "9", "9", "9", "9", 1),
                (_d(2023, 1, 3), "10", "10", "10", "10", 2),
            ]
        ),
    )
    assert (tmp_path / "bars" / "AAPL" / "2022.parquet").exists()
    assert (tmp_path / "bars" / "AAPL" / "2023.parquet").exists()
    got = cache.read_bars("AAPL", _d(2022, 1, 1), _d(2023, 12, 31))
    assert len(got) == 2


def test_overwrite_dedupes_last_wins(tmp_path: Path) -> None:
    cache = ParquetCache(tmp_path)
    cache.write_bars("AAPL", JAN)
    # re-write 2023-01-04 with a corrected close
    cache.write_bars("AAPL", _bars([(_d(2023, 1, 4), "10.50", "12", "10", "99.99", 2000)]))
    got = cache.read_bars("AAPL", _d(2023, 1, 1), _d(2023, 1, 31))
    assert len(got) == 2  # not duplicated
    assert got[got["ts"] == pd.Timestamp(_d(2023, 1, 4))]["close"].iloc[0] == Decimal("99.99")


def test_missing_ranges(tmp_path: Path) -> None:
    cache = ParquetCache(tmp_path)
    cache.write_bars("AAPL", JAN, covered=(_d(2023, 1, 1), _d(2023, 1, 31)))

    # a tail gap beyond coverage
    assert cache.missing_ranges("AAPL", _d(2023, 1, 1), _d(2023, 3, 1)) == [
        (_d(2023, 1, 31), _d(2023, 3, 1))
    ]
    # fully covered inner range -> nothing missing
    assert cache.missing_ranges("AAPL", _d(2023, 1, 5), _d(2023, 1, 20)) == []
    # unknown symbol -> whole range missing
    assert cache.missing_ranges("ZZZ", _d(2023, 1, 1), _d(2023, 1, 2)) == [
        (_d(2023, 1, 1), _d(2023, 1, 2))
    ]


def test_missing_ranges_with_leading_and_internal_gaps(tmp_path: Path) -> None:
    cache = ParquetCache(tmp_path)
    cache.write_bars("AAPL", JAN, covered=(_d(2023, 2, 1), _d(2023, 2, 28)))
    cache.write_bars("AAPL", JAN, covered=(_d(2023, 4, 1), _d(2023, 4, 30)))
    gaps = cache.missing_ranges("AAPL", _d(2023, 1, 1), _d(2023, 5, 1))
    assert gaps == [
        (_d(2023, 1, 1), _d(2023, 2, 1)),  # leading
        (_d(2023, 2, 28), _d(2023, 4, 1)),  # internal
        (_d(2023, 4, 30), _d(2023, 5, 1)),  # trailing
    ]


def test_coverage_merges_adjacent(tmp_path: Path) -> None:
    cache = ParquetCache(tmp_path)
    cache.write_bars("AAPL", JAN, covered=(_d(2023, 1, 1), _d(2023, 1, 15)))
    cache.write_bars("AAPL", JAN, covered=(_d(2023, 1, 15), _d(2023, 1, 31)))
    assert cache.missing_ranges("AAPL", _d(2023, 1, 1), _d(2023, 1, 31)) == []


def test_write_rejects_naive_timestamps(tmp_path: Path) -> None:
    cache = ParquetCache(tmp_path)
    naive = pd.DataFrame(
        {
            "ts": [datetime(2023, 1, 3)],
            "open": [Decimal("10")],
            "high": [Decimal("10")],
            "low": [Decimal("10")],
            "close": [Decimal("10")],
            "volume": [1],
        }
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        cache.write_bars("AAPL", naive)


def test_write_rejects_float_prices(tmp_path: Path) -> None:
    cache = ParquetCache(tmp_path)
    floaty = pd.DataFrame(
        {
            "ts": [_d(2023, 1, 3)],
            "open": [10.1],  # float, not Decimal -> would lose precision
            "high": [10.1],
            "low": [10.1],
            "close": [10.1],
            "volume": [1],
        }
    )
    with pytest.raises(TypeError, match="Decimal"):
        cache.write_bars("AAPL", floaty)


def test_write_rejects_fractional_volume(tmp_path: Path) -> None:
    cache = ParquetCache(tmp_path)
    frac = _bars([(_d(2023, 1, 3), "10", "10", "10", "10", 1)])
    frac["volume"] = [1.5]  # fractional -> not an integer
    with pytest.raises(TypeError, match="integer"):
        cache.write_bars("AAPL", frac)


def test_missing_ranges_empty_when_end_not_after_start(tmp_path: Path) -> None:
    cache = ParquetCache(tmp_path)
    assert cache.missing_ranges("AAPL", _d(2023, 1, 10), _d(2023, 1, 10)) == []
    assert cache.missing_ranges("AAPL", _d(2023, 1, 10), _d(2023, 1, 1)) == []


def test_content_hash_insensitive_to_split_writes(tmp_path: Path) -> None:
    both = _bars(
        [
            (_d(2023, 1, 3), "10", "11", "9", "10.50", 1000),
            (_d(2023, 1, 4), "10.50", "12", "10", "11.25", 2000),
        ]
    )
    one_shot = ParquetCache(tmp_path / "one")
    one_shot.write_bars("AAPL", both)

    # writing the two bars in separate calls (and reversed order) yields the same hash
    split = ParquetCache(tmp_path / "split")
    split.write_bars("AAPL", _bars([(_d(2023, 1, 4), "10.50", "12", "10", "11.25", 2000)]))
    split.write_bars("AAPL", _bars([(_d(2023, 1, 3), "10", "11", "9", "10.50", 1000)]))
    assert split.content_hash("AAPL") == one_shot.content_hash("AAPL")


def test_content_hash_stable(tmp_path: Path) -> None:
    a = ParquetCache(tmp_path / "a")
    a.write_bars("AAPL", JAN)
    h1 = a.content_hash("AAPL")

    # same data in a fresh cache -> same hash
    b = ParquetCache(tmp_path / "b")
    b.write_bars("AAPL", JAN)
    assert b.content_hash("AAPL") == h1

    # different data -> different hash
    c = ParquetCache(tmp_path / "c")
    c.write_bars("AAPL", _bars([(_d(2023, 1, 3), "10", "11", "9", "99", 1000)]))
    assert c.content_hash("AAPL") != h1


def test_content_hash_empty_symbol_is_empty_digest(tmp_path: Path) -> None:
    cache = ParquetCache(tmp_path)
    assert cache.content_hash("NONE") == hashlib.sha256(b"").hexdigest()
