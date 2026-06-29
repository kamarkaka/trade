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


def _quote(last: str = "100", bid: str = "99.5", ask: str = "100.5", ts: datetime = NOW) -> Quote:
    return Quote("AAPL", ts, Decimal(last), Decimal(bid), Decimal(ask), 1000)


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
    stale = _quote(ts=NOW - timedelta(seconds=120))  # > 60s
    assert rules.price_sanity(_order(), _ctx(quote=stale)).ok is False
    assert rules.price_sanity(_order(), _ctx()).ok is True  # good quote


# --- fail-closed on missing data -------------------------------------------- #


def test_fail_closed_on_missing_quote() -> None:
    ctx = _ctx(quote=None)
    for rule in (
        rules.price_sanity,
        rules.max_order_notional,
        rules.max_position_size,
        rules.max_gross_exposure,
    ):
        assert rule(_order(), ctx).ok is False, rule.__name__
