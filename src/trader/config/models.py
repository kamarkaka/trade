"""Validated configuration schema (design §11).

Pydantic models that mirror the YAML config field-for-field. They are the
validating *edge* layer: raw config (defaults < file < env < CLI, assembled by
the loader in M0.5) is validated into an immutable ``AppConfig``, so config
errors fail fast and loudly. The runtime/core dataclasses (``trader.core``) are
distinct; the bindings loader (M3.7) converts ``StrategyBindingConfig`` →
``trader.core.StrategyBinding``.

All models are frozen and forbid unknown keys (so typos are caught). Enum-valued
fields reuse the ``trader.core.enums`` enums, so config strings validate against a
single source of truth.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trader.core.enums import (
    ConflictPolicy,
    Distribution,
    DriftDirection,
    Mode,
    OnOvershoot,
    OrderType,
)

# Hard ceiling for jitter so a typo can't schedule a wild drift (design §7.2).
DRIFT_MAX_CEILING_MINUTES = 60


class _Base(BaseModel):
    """Frozen, typo-proof base for every config model."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class AccountConfig(_Base):
    broker: str = "schwab"
    account_ref: str = "primary"
    secrets_ref: str = "keychain"  # where credentials live (§13); never inline


class ScheduleConfig(_Base):
    """Global scheduling defaults shared by every strategy (design §7.1)."""

    timezone: str = "America/New_York"
    market_calendar: str = "XNYS"
    base_seed: int | None = None  # int => reproducible (backtest); None => entropy (live)
    catch_up: bool = False
    misfire_grace_seconds: int = Field(default=120, ge=0)

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"invalid IANA timezone: {v!r}") from exc
        return v


class SlotConfig(_Base):
    """One scheduled slot in a strategy's daily schedule (design §7.1)."""

    id: str
    time: str  # local wall-clock "HH:MM"
    drift_max_minutes: int = Field(default=30, ge=0, le=DRIFT_MAX_CEILING_MINUTES)
    drift_direction: DriftDirection = DriftDirection.FORWARD
    distribution: Distribution = Distribution.UNIFORM
    on_overshoot: OnOvershoot = OnOvershoot.CLAMP
    catch_up: bool | None = None  # None => inherit schedule.catch_up (§7.1)

    @field_validator("time")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%H:%M")  # validate "HH:MM" format only
        except ValueError as exc:
            raise ValueError(f"time must be HH:MM, got {v!r}") from exc
        return v


class RiskConfig(_Base):
    """Account-wide risk limits (design §10). Strategy ``risk_overrides`` may
    override these keys per strategy."""

    max_position_size_pct: float = Field(default=10.0, gt=0, le=100)
    max_order_notional_usd: Decimal = Field(default=Decimal("5000"), gt=0)
    max_gross_exposure_usd: Decimal = Field(default=Decimal("25000"), gt=0)
    daily_loss_limit_pct: float = Field(default=2.0, gt=0)
    max_trades_per_day: int = Field(default=6, ge=0)
    max_staleness_seconds: int = Field(default=60, gt=0)
    max_spread_pct: float = Field(default=1.0, ge=0)
    allowlist: tuple[str, ...] = ()  # when non-empty, only these symbols may trade
    denylist: tuple[str, ...] = ()  # these symbols are always blocked (takes precedence)
    enforce_pdt: bool = True
    auto_flatten_on_kill: bool = False
    conflict_policy: ConflictPolicy = ConflictPolicy.NET


class StrategyBindingConfig(_Base):
    """A strategy + its own universe + its own schedule (design §6.1)."""

    id: str
    name: str
    enabled: bool = True
    params: dict[str, object] = Field(default_factory=dict)  # validated by each strategy later
    universe: tuple[str, ...] = ()
    slots: tuple[SlotConfig, ...] = ()
    risk_overrides: dict[str, object] | None = None

    @field_validator("risk_overrides")
    @classmethod
    def _valid_override_keys(cls, v: dict[str, object] | None) -> dict[str, object] | None:
        if v is None:
            return v
        unknown = set(v) - set(RiskConfig.model_fields)
        if unknown:
            raise ValueError(f"unknown risk_overrides keys: {sorted(unknown)}")
        return v

    @model_validator(mode="after")
    def _enabled_requirements(self) -> StrategyBindingConfig:
        if self.enabled:
            if not self.universe:
                raise ValueError(f"enabled strategy {self.id!r} needs a non-empty universe")
            if not self.slots:
                raise ValueError(f"enabled strategy {self.id!r} needs at least one slot")
        return self


class ExecutionConfig(_Base):
    order_type: OrderType = OrderType.MARKET
    poll_timeout_seconds: int = Field(default=60, gt=0)
    rate_limit_per_min: int = Field(default=100, gt=0, le=120)

    @field_validator("order_type", mode="before")
    @classmethod
    def _normalize_order_type(cls, v: object) -> object:
        # Config uses lowercase (order_type: market); OrderType values are UPPER.
        return v.upper() if isinstance(v, str) else v


class FeesModelConfig(_Base):
    commission: Decimal = Decimal("0")
    regulatory_bps: float = Field(default=0.0, ge=0)


class SlippageModelConfig(_Base):
    type: Literal["bps", "fixed", "vol"] = "bps"
    value: float = Field(default=0.0, ge=0)


class BacktestConfig(_Base):
    start: date
    end: date
    data_vendor: str = "schwab"
    fees_model: FeesModelConfig = Field(default_factory=FeesModelConfig)
    slippage_model: SlippageModelConfig = Field(default_factory=SlippageModelConfig)
    base_seed: int | None = None

    @model_validator(mode="after")
    def _date_order(self) -> BacktestConfig:
        if self.end < self.start:
            raise ValueError("backtest end must be >= start")
        return self


class AlertingConfig(_Base):
    channels: tuple[str, ...] = ("telegram", "email")
    heartbeat_minutes: int = Field(default=60, gt=0)


class ObservabilityConfig(_Base):
    log_format: Literal["json", "console"] = "json"
    db_path: str = "/state/trader.sqlite"
    data_cache: str = "/data/"


class AppConfig(_Base):
    """The full validated configuration. The same object drives live and backtest;
    only the injected broker/data/clock differ (design §11)."""

    mode: Mode = Mode.PAPER
    account: AccountConfig = Field(default_factory=AccountConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    strategies: tuple[StrategyBindingConfig, ...] = Field(min_length=1)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    backtest: BacktestConfig | None = None
    alerting: AlertingConfig = Field(default_factory=AlertingConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)

    @model_validator(mode="after")
    def _unique_strategy_ids(self) -> AppConfig:
        ids = [s.id for s in self.strategies]
        if len(set(ids)) != len(ids):
            raise ValueError("strategy ids must be unique")
        return self


__all__ = [
    "AccountConfig",
    "AlertingConfig",
    "AppConfig",
    "BacktestConfig",
    "ExecutionConfig",
    "FeesModelConfig",
    "ObservabilityConfig",
    "RiskConfig",
    "ScheduleConfig",
    "SlippageModelConfig",
    "SlotConfig",
    "StrategyBindingConfig",
]
