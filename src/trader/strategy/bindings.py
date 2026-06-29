"""Config -> core StrategyBinding loader (design §7.1 / §11).

The single boundary that turns the validated ``AppConfig`` (schedule + strategies)
into the frozen core ``StrategyBinding``/``SlotSpec`` types the scheduler (M3.4) and
orchestrator (M3.9) consume — identically in backtest and live. It also resolves each
binding's strategy ``name`` against the registry (M3.6) and applies the global
``schedule.catch_up`` as the default when a slot doesn't override it.

(Field validation — time format, drift ceiling, unique strategy ids — already lives
in the pydantic config models from M0.4; this layer adds registry resolution and the
config->core type mapping.)
"""

from __future__ import annotations

from datetime import datetime

from trader.config.models import AppConfig, ScheduleConfig, SlotConfig
from trader.core.types import SlotSpec, StrategyBinding

from .params import validate_params
from .registry import REGISTRY, StrategyRegistry


def _to_slotspec(slot: SlotConfig, schedule: ScheduleConfig) -> SlotSpec:
    catch_up = slot.catch_up if slot.catch_up is not None else schedule.catch_up
    return SlotSpec(
        slot_id=slot.id,
        at=datetime.strptime(slot.time, "%H:%M").time(),
        drift_max_minutes=slot.drift_max_minutes,
        drift_direction=slot.drift_direction,
        distribution=slot.distribution,
        on_overshoot=slot.on_overshoot,
        catch_up=catch_up,
    )


def load_bindings(
    config: AppConfig, *, registry: StrategyRegistry = REGISTRY
) -> tuple[ScheduleConfig, list[StrategyBinding]]:
    """Resolve config strategies into core StrategyBindings (registry-validated)."""
    available = registry.names()
    bindings: list[StrategyBinding] = []
    for sb in config.strategies:
        if sb.name not in available:
            raise ValueError(
                f"unknown strategy name {sb.name!r} for binding {sb.id!r}; available: {available}"
            )
        bindings.append(
            StrategyBinding(
                strategy_id=sb.id,
                strategy_name=sb.name,
                # Validate params against the strategy's model (if any) at load time, so a
                # bad/typo'd param fails fast instead of at the first cycle.
                params=validate_params(sb.name, dict(sb.params)),
                universe=tuple(sb.universe),
                slots=tuple(_to_slotspec(slot, config.schedule) for slot in sb.slots),
                enabled=sb.enabled,
                risk_overrides=dict(sb.risk_overrides) if sb.risk_overrides is not None else None,
            )
        )
    return config.schedule, bindings
