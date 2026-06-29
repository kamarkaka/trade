"""Live scheduler daemon — paper placeholder (design §7.4/§7.5/§16.5, Appendix C).

Registers one APScheduler cron job per (strategy_id, slot). Each callback gates at
fire time on calendar + jitter + the fired-slot ledger, then runs the SAME
``Orchestrator.run_cycle`` (under the orchestrator's single global cycle lock) — so
backtest and live share the exact decision path. In M3 the broker behind the
orchestrator is SimBroker/FakeBroker only: NO real orders (the CLI enforces
mode=paper; live wiring is M5).

Per-job ``max_instances=1``/``coalesce`` are necessary but NOT sufficient for
cross-strategy safety, so the actual decide->submit critical section is serialized by
the GlobalCycleLock inside the orchestrator. Callbacks are exposed for deterministic
testing (no wall-clock loop): the registered job and ``fire(strategy_id, slot_id)``
run the identical logic.
"""

from __future__ import annotations

import time as _time
from collections.abc import Callable
from datetime import timedelta
from zoneinfo import ZoneInfo

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from trader.config.models import ScheduleConfig
from trader.core.protocols import Clock
from trader.core.types import SlotSpec, StrategyBinding
from trader.observability.alerting import Alerter, AlertEvent, AlertKind
from trader.observability.heartbeat import Heartbeat
from trader.observability.logging import get_logger
from trader.orchestrator.cycle import CycleResult, Orchestrator
from trader.scheduler.calendar import TradingCalendar
from trader.scheduler.jitter import compute_drift
from trader.state.ledger import FiredSlotLedger
from trader.strategy.registry import REGISTRY, StrategyRegistry


class SchedulerDaemon:
    """Registers per-(strategy,slot) cron jobs gating into a paper-mode run_cycle."""

    def __init__(
        self,
        *,
        bindings: list[StrategyBinding],
        schedule: ScheduleConfig,
        calendar: TradingCalendar,
        ledger: FiredSlotLedger,
        orchestrator: Orchestrator,
        clock: Clock,
        registry: StrategyRegistry = REGISTRY,
        alerter: Alerter | None = None,
        heartbeat: Heartbeat | None = None,
        heartbeat_interval_seconds: float = 60.0,
        sleep: Callable[[float], None] = _time.sleep,
    ) -> None:
        self._bindings = bindings
        self._schedule = schedule
        self._calendar = calendar
        self._ledger = ledger
        self._orchestrator = orchestrator
        self._clock = clock
        self._registry = registry
        self._tz = ZoneInfo(schedule.timezone)
        self._log = get_logger("daemon")
        self._alerter = alerter
        self._heartbeat = heartbeat
        self._heartbeat_interval = heartbeat_interval_seconds
        self._sleep = sleep
        # Single-worker executor: jobs run one-at-a-time on one thread, so the SQLite
        # connection is never used concurrently (the global cycle lock is the additional
        # cross-strategy guard). All callbacks run off the main thread.
        # The heartbeat runs on its OWN single-thread executor so a long-but-healthy cycle
        # occupying the default worker can't starve the liveness beat (which would otherwise
        # trip the healthcheck and restart a daemon mid-cycle). Each executor thread uses a
        # distinct sqlite connection (the cycle conn vs the heartbeat's own conn).
        self._scheduler = BackgroundScheduler(
            timezone=self._tz,
            executors={
                "default": ThreadPoolExecutor(max_workers=1),
                "heartbeat": ThreadPoolExecutor(max_workers=1),
            },
        )
        self._callbacks: dict[tuple[str, str], Callable[[], CycleResult | None]] = {}
        self._build_callbacks()  # available to fire() without starting the scheduler

    # --- lifecycle -------------------------------------------------------- #

    def _build_callbacks(self) -> None:
        for binding in self._bindings:
            if not binding.enabled:
                continue
            for slot in binding.slots:
                self._callbacks[(binding.strategy_id, slot.slot_id)] = self._make_callback(
                    binding, slot
                )

    def _make_callback(
        self, binding: StrategyBinding, slot: SlotSpec
    ) -> Callable[[], CycleResult | None]:
        def callback() -> CycleResult | None:
            return self._fire(binding, slot)

        return callback

    def register(self) -> None:
        """Add one cron job per enabled (strategy, slot). Idempotent per instance."""
        for binding in self._bindings:
            if not binding.enabled:
                continue
            for slot in binding.slots:
                self._register_slot(binding, slot)

    def _emit(self, kind: AlertKind, message: str) -> None:
        """Log + (if configured) send a typed alert. Alerting never raises into the daemon."""
        self._log.warning("daemon alert", kind=kind.value, detail=message)
        if self._alerter is not None:
            try:
                self._alerter.alert(AlertEvent(kind, message))
            except Exception as exc:  # pragma: no cover - alerter is itself fail-safe
                self._log.error("alerter raised", error_type=type(exc).__name__)

    def _beat(self) -> None:
        """Liveness touch. Runs ONLY on the dedicated 'heartbeat' executor, so it reflects
        process/scheduler liveness independent of whether a cycle is occupying the worker."""
        if self._heartbeat is not None:
            self._heartbeat.touch("running", f"{len(self._scheduler.get_jobs())} jobs")

    def start(self) -> None:
        if self._scheduler.running:
            return
        self.register()
        if self._heartbeat is not None:
            self._heartbeat.touch("running")  # fresh from the moment we start (main thread)
            self._scheduler.add_job(
                self._beat,
                IntervalTrigger(seconds=self._heartbeat_interval),
                id="__heartbeat__",
                executor="heartbeat",  # own thread + own connection; never starved by a cycle
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )
        self._scheduler.start()

    def stop(self) -> None:
        """Clean shutdown (finish the running cycle, then stop)."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=True)

    @property
    def scheduler(self) -> BackgroundScheduler:
        return self._scheduler

    def fire(self, strategy_id: str, slot_id: str) -> CycleResult | None:
        """Invoke a registered slot's callback directly (for tests / manual ticks)."""
        return self._callbacks[(strategy_id, slot_id)]()

    # --- internals -------------------------------------------------------- #

    def _register_slot(self, binding: StrategyBinding, slot: SlotSpec) -> None:
        catch_up = slot.catch_up if slot.catch_up is not None else self._schedule.catch_up
        # catch_up True => fire if within the grace window; False => skip stale fires.
        # APScheduler requires a positive grace, so clamp (config allows 0).
        grace = max(1, self._schedule.misfire_grace_seconds) if catch_up else 1
        self._scheduler.add_job(
            self._callbacks[(binding.strategy_id, slot.slot_id)],
            CronTrigger(hour=slot.at.hour, minute=slot.at.minute, timezone=self._tz),
            id=f"{binding.strategy_id}:{slot.slot_id}",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=grace,
            replace_existing=True,
        )

    def _fire(self, binding: StrategyBinding, slot: SlotSpec) -> CycleResult | None:
        # NOTE: the heartbeat is touched ONLY by the dedicated 'heartbeat' executor (_beat),
        # never here on the cycle worker -- so liveness is independent of cycle duration and
        # the heartbeat's connection is never used cross-thread.
        today = self._clock.now().astimezone(self._tz).date()
        drift, seed = compute_drift(slot, self._schedule.base_seed, today, binding.strategy_id)
        nominal = self._calendar.localize(today, slot.at)
        resolved = self._calendar.resolve_fire(nominal + timedelta(seconds=drift), slot)
        if resolved is None:
            self._emit(
                AlertKind.SKIPPED_SLOT,
                f"{binding.strategy_id}/{slot.slot_id} skipped (calendar gate) on {today}",
            )
            return None

        delay = (resolved - self._clock.now()).total_seconds()
        if delay > 0:
            self._sleep(delay)  # realize the jitter (no-op sleep in tests)

        if not self._ledger.claim(today, binding.strategy_id, slot.slot_id, resolved, drift, seed):
            return None  # already fired this session (exactly-once)

        strategy = self._registry.create(binding.strategy_name, dict(binding.params))
        try:
            result = self._orchestrator.run_cycle(
                strategy, binding.universe, binding.strategy_id, resolved
            )
        except Exception as exc:
            self._ledger.mark_failed(today, binding.strategy_id, slot.slot_id, str(exc))
            self._emit(
                AlertKind.CRASH, f"{binding.strategy_id}/{slot.slot_id} cycle crashed: {exc}"
            )
            return None

        if result.errors:
            self._ledger.mark_failed(
                today, binding.strategy_id, slot.slot_id, "; ".join(result.errors)
            )
            self._emit(
                AlertKind.CRASH,
                f"{binding.strategy_id}/{slot.slot_id} cycle failed: {result.errors}",
            )
        else:
            self._ledger.mark_done(today, binding.strategy_id, slot.slot_id)
        return result
