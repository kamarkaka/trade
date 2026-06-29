"""Tests for individual risk rules: reject/clamp/fail-closed per rule + boundaries (M4.2)."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from trader.config.models import RiskConfig
from trader.core import Account, DayState, Order, Position, Quote
from trader.core.enums import OrderType, Side
from trader.risk import rules

NOW = datetime(2024, 7, 8, 15, 0, tzinfo=UTC)
ACCOUNT = Account(cash=Decimal("100000"), buying_power=Decimal("100000"), equity=Decimal("100000"))
DAY = DayState(
    trading_date=date(2024, 7, 8),
    start_of_day_equity=Decimal("100000"),
    realized_pnl=Decimal("0"),
    unrealized_pnl=Decimal("0"),
    trades_today=0,
    loss_today=Decimal("0"),
)


def _order(side: Side = Side.BUY, qty: int = 10, symbol: str = "AAPL", cid: str = "c1") -> Order:
    return Order(cid, "s1", symbol, side, qty, OrderType.MARKET)


def _limit(side: Side = Side.BUY, qty: int = 10, limit: str = "100", symbol: str = "AAPL") -> Order:
    return Order("c1", "s1", symbol, side, qty, OrderType.LIMIT, Decimal(limit))


def _quote(
    last: str = "100",
    bid: str = "99.5",
    ask: str = "100.5",
    ts: datetime = NOW,
    prev_close: str | None = None,
) -> Quote:
    pc = Decimal(prev_close) if prev_close is not None else None
    return Quote("AAPL", ts, Decimal(last), Decimal(bid), Decimal(ask), 1000, pc)


_UNSET = object()  # sentinel so quote=None is distinguishable from "use default"


def _ctx(
    *,
    config: RiskConfig | None = None,
    positions: tuple[Position, ...] = (),
    quote: Quote | None | object = _UNSET,
    day_state: DayState = DAY,
    seen: frozenset[str] = frozenset(),
) -> rules.RuleContext:
    resolved = _quote() if quote is _UNSET else quote
    return rules.RuleContext(
        config=config or RiskConfig(),
        positions=positions,
        account=ACCOUNT,
        quote=resolved,  # type: ignore[arg-type]
        day_state=day_state,
        now=NOW,
        seen_client_order_ids=seen,
    )


# --- allow/deny + duplicate ------------------------------------------------- #


def test_denylist_blocks() -> None:
    ctx = _ctx(config=RiskConfig(denylist=("AAPL",)))
    assert rules.allowlist_denylist(_order(), ctx).ok is False


def test_allowlist_default_deny() -> None:
    ctx = _ctx(config=RiskConfig(allowlist=("MSFT",)))
    assert rules.allowlist_denylist(_order(symbol="AAPL"), ctx).ok is False  # not listed
    assert rules.allowlist_denylist(_order(symbol="MSFT"), ctx).ok is True
    assert rules.allowlist_denylist(_order(), _ctx()).ok is True  # empty allowlist -> allow


def test_duplicate_order_guard() -> None:
    assert rules.duplicate_order_guard(_order(cid="c1"), _ctx(seen=frozenset({"c1"}))).ok is False
    assert rules.duplicate_order_guard(_order(cid="c2"), _ctx(seen=frozenset({"c1"}))).ok is True


# --- notional (clamp) ------------------------------------------------------- #


def test_max_order_notional_rejects_over() -> None:
    ctx = _ctx(config=RiskConfig(max_order_notional_usd=Decimal("50")))  # < 1 share @ 100
    assert rules.max_order_notional(_order(qty=10), ctx).ok is False


def test_max_order_notional_clamp_to_limit() -> None:
    ctx = _ctx(config=RiskConfig(max_order_notional_usd=Decimal("500")))  # 5 shares @ 100
    result = rules.max_order_notional(_order(qty=10), ctx)
    assert result.ok and result.clamped_quantity == 5


def test_max_order_notional_within_limit_passes() -> None:
    result = rules.max_order_notional(_order(qty=10), _ctx())  # 1000 < 5000 default
    assert result.ok and result.clamped_quantity is None


# --- resulting-position cap ------------------------------------------------- #


def test_resulting_position_cap() -> None:
    # 10% of 100k equity / $100 = 100 shares cap; evaluated on the RESULTING position
    over = rules.max_position_size(_order(qty=150), _ctx())
    assert over.ok is False
    ok = rules.max_position_size(_order(qty=50), _ctx())
    assert ok.ok is True
    # existing position counts toward the resulting total
    with_pos = rules.max_position_size(
        _order(qty=60), _ctx(positions=(Position("AAPL", 60, Decimal("100"), Decimal("6000")),))
    )
    assert with_pos.ok is False  # 60 + 60 = 120 > 100


# --- gross exposure --------------------------------------------------------- #


def test_max_gross_exposure() -> None:
    held = (Position("MSFT", 240, Decimal("100"), Decimal("24000")),)
    assert rules.max_gross_exposure(_order(qty=10), _ctx(positions=held)).ok is True  # 25000 == cap
    assert rules.max_gross_exposure(_order(qty=20), _ctx(positions=held)).ok is False  # 26000 > cap


# --- daily loss + trade count ----------------------------------------------- #


def test_daily_loss_limit_halts_new_entries() -> None:
    breached = DayState(
        date(2024, 7, 8), Decimal("100000"), Decimal("0"), Decimal("0"), 0, Decimal("2500")
    )  # 2.5% > 2% limit
    assert rules.daily_loss_limit(_order(), _ctx(day_state=breached)).ok is False
    assert rules.daily_loss_limit(_order(), _ctx()).ok is True  # 0 loss


def test_max_trades_per_day() -> None:
    maxed = DayState(
        date(2024, 7, 8), Decimal("100000"), Decimal("0"), Decimal("0"), 6, Decimal("0")
    )
    assert rules.max_trades_per_day(_order(), _ctx(day_state=maxed)).ok is False
    assert rules.max_trades_per_day(_order(), _ctx()).ok is True


# --- price sanity ----------------------------------------------------------- #


def test_price_sanity_rejects_zero_negative_wide_spread_and_stale() -> None:
    assert rules.price_sanity(_order(), _ctx(quote=_quote(last="0"))).ok is False
    assert rules.price_sanity(_order(), _ctx(quote=_quote(last="-1"))).ok is False
    wide = _quote(bid="90", ask="110")  # 20% spread > 1%
    assert rules.price_sanity(_order(), _ctx(quote=wide)).ok is False
    crossed = _quote(bid="101", ask="99")  # bid > ask: crossed/locked book
    assert rules.price_sanity(_order(), _ctx(quote=crossed)).ok is False
    stale = _quote(ts=NOW - timedelta(seconds=120))  # > 60s
    assert rules.price_sanity(_order(), _ctx(quote=stale)).ok is False
    assert rules.price_sanity(_order(), _ctx()).ok is True  # good quote


# --- fail-closed on missing data -------------------------------------------- #


def test_price_sanity_rejects_bad_tick_beyond_prev_close_band() -> None:
    # 10x bad tick (last=100 vs prev_close=10 => 900% deviation > 20% default band)
    bad = _ctx(quote=_quote(last="100", prev_close="10"))
    assert rules.price_sanity(_order(), bad).ok is False
    within = _ctx(quote=_quote(last="110", prev_close="100"))  # 10% <= 20% band
    assert rules.price_sanity(_order(), within).ok is True
    no_pc = _ctx(quote=_quote(last="100"))  # prev_close None => band check skipped
    assert rules.price_sanity(_order(), no_pc).ok is True


def test_fail_closed_on_missing_quote() -> None:
    ctx = _ctx(quote=None)
    for rule in (
        rules.price_sanity,
        rules.max_order_notional,
        rules.max_position_size,
        rules.max_gross_exposure,
    ):
        assert rule(_order(), ctx).ok is False, rule.__name__


def test_limit_order_fail_closed_on_missing_quote() -> None:
    # H1 regression: a LIMIT order carries its own price, but missing market data
    # must still fail closed in the cap rules (not just price_sanity).
    ctx = _ctx(quote=None)
    for rule in (rules.max_order_notional, rules.max_position_size, rules.max_gross_exposure):
        assert rule(_limit(qty=10), ctx).ok is False, rule.__name__


# --- never block de-risking (design §10) ------------------------------------ #


def test_reducing_sell_exempt_from_notional_cap() -> None:
    # Hold 100; SELL 10 with a tiny cap that would clamp/reject a fresh entry.
    held = (Position("AAPL", 100, Decimal("100"), Decimal("10000")),)
    ctx = _ctx(positions=held, config=RiskConfig(max_order_notional_usd=Decimal("50")))
    result = rules.max_order_notional(_order(side=Side.SELL, qty=10), ctx)
    assert result.ok is True and result.clamped_quantity is None


def test_reducing_sell_exempt_from_gross_when_over_cap() -> None:
    # Already over the gross cap; a reduction must still be allowed.
    held = (Position("AAPL", 300, Decimal("100"), Decimal("30000")),)  # $30k > $25k cap
    ctx = _ctx(positions=held)
    assert rules.max_gross_exposure(_order(side=Side.SELL, qty=100), ctx).ok is True


def test_reducing_sell_exempt_from_position_cap_when_over() -> None:
    # Position already over its size cap (e.g. adopted via reconciliation); de-risk allowed.
    held = (Position("AAPL", 300, Decimal("100"), Decimal("30000")),)  # cap is 100 shares
    ctx = _ctx(positions=held)
    assert rules.max_position_size(_order(side=Side.SELL, qty=50), ctx).ok is True


def test_position_cap_flip_through_zero_into_large_short_rejected() -> None:
    # SELL 300 from long 100 => resulting short 200 (abs 200 > 100 cap), an exposure
    # INCREASE on the short side -> not exempt, must be rejected.
    held = (Position("AAPL", 100, Decimal("100"), Decimal("10000")),)
    assert (
        rules.max_position_size(_order(side=Side.SELL, qty=300), _ctx(positions=held)).ok is False
    )


def test_cover_short_exempt() -> None:
    held = (Position("AAPL", -100, Decimal("100"), Decimal("-10000")),)  # short 100
    assert rules.max_position_size(_order(side=Side.BUY, qty=50), _ctx(positions=held)).ok is True


def test_gross_exposure_counts_short_market_value() -> None:
    held = (Position("MSFT", -100, Decimal("100"), Decimal("-10000")),)  # |mv| = 10000
    ok = rules.max_gross_exposure(_order(qty=150), _ctx(positions=held))  # 10000 + 15000 == cap
    assert ok.ok is True
    over = rules.max_gross_exposure(_order(qty=160), _ctx(positions=held))  # 26000 > cap
    assert over.ok is False


def test_gross_exposure_addon_does_not_double_count() -> None:
    # Existing AAPL 100 ($10k), BUY 100 more -> resulting 200 ($20k), not $30k.
    held = (Position("AAPL", 100, Decimal("100"), Decimal("10000")),)
    assert rules.max_gross_exposure(_order(qty=100), _ctx(positions=held)).ok is True  # 20k <= 25k


def test_daily_loss_limit_allows_gain() -> None:
    gain = DayState(
        date(2024, 7, 8), Decimal("100000"), Decimal("0"), Decimal("0"), 0, Decimal("-500")
    )  # negative loss == a gain
    assert rules.daily_loss_limit(_order(), _ctx(day_state=gain)).ok is True
