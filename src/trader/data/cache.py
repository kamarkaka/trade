"""On-disk OHLCV bar cache: partitioned Parquet + a coverage catalog (design §9.4).

Backtests must be fast, offline, and reproducible, so daily history is cached as
Parquet partitioned by ``symbol`` and ``year``, with a small SQLite catalog tracking
the *requested* coverage ranges (so a holiday/weekend gap with no bars isn't
re-fetched forever). ``content_hash`` feeds the run manifest (M2.9) so a backtest
references an exact data snapshot.

Design choices worth calling out:

* **Catalog uses SQLite** (the plan permits "sqlite or duckdb"); DuckDB's
  query-Parquet-directly capability isn't needed until a SQL reporting path exists,
  so the heavier dependency is deferred.
* **Prices are stored as strings** in Parquet and parsed back to ``Decimal`` on read,
  so money keeps full precision (binary float would corrupt it). ``Decimal``↔``str``
  is exact.
* **``content_hash`` hashes a canonical row serialization**, not the raw Parquet
  bytes (which aren't stable across library versions / compression settings).
"""

from __future__ import annotations

import contextlib
import hashlib
import numbers
import os
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd

# Canonical bar frame schema exchanged with callers.
BAR_COLUMNS = ("ts", "open", "high", "low", "close", "volume")
_PRICE_COLUMNS = ("open", "high", "low", "close")

Interval = tuple[datetime, datetime]


def _require_utc(dt: datetime, name: str) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware, got naive {dt!r}")
    return dt.astimezone(UTC)


def _ensure_decimal(value: object, col: str) -> Decimal:
    # Fail closed (like core.types): money must arrive as Decimal, never float — a
    # float would already have lost precision before it reached the cache.
    if not isinstance(value, Decimal):
        raise TypeError(
            f"price column {col!r} must contain Decimal values, got {type(value).__name__}"
        )
    return value


def _ensure_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, numbers.Integral):
        raise TypeError(f"volume must be an integer, got {type(value).__name__}")
    return int(value)


def _merge_intervals(intervals: list[Interval]) -> list[Interval]:
    """Merge overlapping/adjacent intervals into a minimal sorted set."""
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda iv: iv[0])
    merged: list[Interval] = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:  # overlap or touch
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


class ParquetCache:
    """A content-hashed, range-tracked Parquet cache of OHLCV bars."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._catalog_path = self._root / "catalog.sqlite"
        with contextlib.closing(self._connect()) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS coverage ("
                "symbol TEXT NOT NULL, start_ts TEXT NOT NULL, end_ts TEXT NOT NULL)"
            )

    # --- paths / catalog -------------------------------------------------- #

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._catalog_path), isolation_level=None)

    def _symbol_dir(self, symbol: str) -> Path:
        return self._root / "bars" / symbol

    def _partition_path(self, symbol: str, year: int) -> Path:
        return self._symbol_dir(symbol) / f"{year}.parquet"

    def _load_coverage(self, symbol: str) -> list[Interval]:
        with contextlib.closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT start_ts, end_ts FROM coverage WHERE symbol = ?", (symbol,)
            ).fetchall()
        return [(datetime.fromisoformat(s), datetime.fromisoformat(e)) for s, e in rows]

    def _record_coverage(self, symbol: str, start: datetime, end: datetime) -> None:
        merged = _merge_intervals(
            [*self._load_coverage(symbol), (_require_utc(start, "start"), _require_utc(end, "end"))]
        )
        with contextlib.closing(self._connect()) as conn:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM coverage WHERE symbol = ?", (symbol,))
            conn.executemany(
                "INSERT INTO coverage (symbol, start_ts, end_ts) VALUES (?, ?, ?)",
                [(symbol, s.isoformat(), e.isoformat()) for s, e in merged],
            )
            conn.execute("COMMIT")

    # --- frame (de)serialization ------------------------------------------ #

    def _normalize(self, df: pd.DataFrame) -> pd.DataFrame:
        """Validate + canonicalize an incoming bar frame (fail-closed on bad input)."""
        missing = [c for c in BAR_COLUMNS if c not in df.columns]
        if missing:
            raise ValueError(f"bar frame missing columns: {missing}")
        out = df[list(BAR_COLUMNS)].copy()
        # ts must be tz-aware (reject naive — never silently localize); store as UTC.
        ts = pd.to_datetime(out["ts"])
        if ts.dt.tz is None:
            raise ValueError("bar 'ts' column must be timezone-aware (UTC)")
        out["ts"] = ts.dt.tz_convert("UTC")
        for col in _PRICE_COLUMNS:
            out[col] = out[col].map(lambda v, _c=col: _ensure_decimal(v, _c))
        out["volume"] = out["volume"].map(_ensure_int).astype("int64")
        return out

    def _to_storage(self, frame: pd.DataFrame) -> pd.DataFrame:
        # Decimal -> str for exact, version-stable Parquet storage.
        out = frame.copy()
        for col in _PRICE_COLUMNS:
            out[col] = out[col].map(str)
        return out

    def _from_storage(self, frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.copy()
        out["ts"] = pd.to_datetime(out["ts"], utc=True)
        for col in _PRICE_COLUMNS:
            out[col] = out[col].map(lambda v: Decimal(str(v)))
        out["volume"] = out["volume"].astype("int64")
        return out

    def _empty_frame(self) -> pd.DataFrame:
        return pd.DataFrame({c: [] for c in BAR_COLUMNS})

    # --- public API ------------------------------------------------------- #

    def write_bars(self, symbol: str, df: pd.DataFrame, *, covered: Interval | None = None) -> None:
        """Write/merge bars for ``symbol`` and record the covered range.

        ``df`` must use the canonical schema: tz-aware ``ts``, ``Decimal`` prices
        (never float — that would already have lost precision), and integer
        ``volume``. ``covered`` is the *requested* range (e.g. the ingestion
        window); when omitted it defaults to the data's own extent. Bars are merged
        with any existing partition, de-duplicated by timestamp (last wins), sorted.
        """
        if df.empty:
            if covered is not None:
                self._record_coverage(symbol, covered[0], covered[1])
            return

        frame = self._normalize(df)
        self._symbol_dir(symbol).mkdir(parents=True, exist_ok=True)
        for year in sorted(frame["ts"].dt.year.unique()):
            part = self._to_storage(frame[frame["ts"].dt.year == year])
            path = self._partition_path(symbol, int(year))
            if path.exists():
                part = pd.concat([pd.read_parquet(path), part], ignore_index=True)
            part = (
                part.drop_duplicates(subset="ts", keep="last")
                .sort_values("ts")
                .reset_index(drop=True)
            )
            # Atomic replace: write a temp partition then rename, so a crash mid-write
            # can't leave a truncated/corrupt Parquet file.
            tmp = path.with_name(f"{path.name}.tmp")
            part.to_parquet(tmp, engine="pyarrow", index=False)
            os.replace(tmp, path)

        cov_start = covered[0] if covered else frame["ts"].min().to_pydatetime()
        cov_end = covered[1] if covered else frame["ts"].max().to_pydatetime()
        self._record_coverage(symbol, cov_start, cov_end)

    def read_bars(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Return bars for ``symbol`` within ``[start, end]`` (inclusive), ascending."""
        start = _require_utc(start, "start")
        end = _require_utc(end, "end")
        frames = [
            pd.read_parquet(self._partition_path(symbol, year))
            for year in range(start.year, end.year + 1)
            if self._partition_path(symbol, year).exists()
        ]
        if not frames:
            return self._empty_frame()
        df = self._from_storage(pd.concat(frames, ignore_index=True))
        mask = (df["ts"] >= pd.Timestamp(start)) & (df["ts"] <= pd.Timestamp(end))
        return df[mask].sort_values("ts").reset_index(drop=True)

    def missing_ranges(self, symbol: str, start: datetime, end: datetime) -> list[Interval]:
        """Sub-intervals of ``[start, end]`` not yet covered (for cache-on-demand).

        Coverage is treated as half-open here (a gap whose end touches a coverage
        start is not re-fetched), whereas ``read_bars`` is closed on both ends. For
        daily bars the only effect is that the M2.4 ingester may re-read a single
        boundary bar — harmless given write_bars de-dupes.
        """
        start = _require_utc(start, "start")
        end = _require_utc(end, "end")
        if end <= start:
            return []
        clipped = [
            (max(s, start), min(e, end))
            for s, e in self._load_coverage(symbol)
            if max(s, start) < min(e, end)
        ]
        gaps: list[Interval] = []
        cursor = start
        for s, e in _merge_intervals(clipped):
            if s > cursor:
                gaps.append((cursor, s))
            cursor = max(cursor, e)
        if cursor < end:
            gaps.append((cursor, end))
        return gaps

    def content_hash(self, symbol: str) -> str:
        """A stable SHA-256 over the symbol's full cached bar set (snapshot id)."""
        symbol_dir = self._symbol_dir(symbol)
        parts = sorted(symbol_dir.glob("*.parquet")) if symbol_dir.exists() else []
        digest = hashlib.sha256()
        if parts:
            df = self._from_storage(
                pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
            )
            df = df.sort_values("ts").reset_index(drop=True)
            for row in df.itertuples(index=False):
                line = (
                    f"{row.ts.isoformat()}|{row.open}|{row.high}|{row.low}|{row.close}|{row.volume}"
                )
                digest.update(line.encode())
                digest.update(b"\n")
        return digest.hexdigest()
