"""Separate component health plus an aggregate readiness verdict."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    name: str
    healthy: bool
    detail: str
    latency_ms: float | None = None


@dataclass(frozen=True, slots=True)
class HealthReport:
    components: tuple[ComponentHealth, ...]
    ready: bool


HealthProbe = Callable[[], Awaitable[ComponentHealth]]


async def collect_health(
    probes: tuple[HealthProbe, ...], *, timeout_seconds: float = 3.0
) -> HealthReport:
    async def bounded(probe: HealthProbe) -> ComponentHealth:
        try:
            return await asyncio.wait_for(probe(), timeout_seconds)
        except TimeoutError:
            return ComponentHealth("unknown", False, "timeout")
        except Exception as exc:
            return ComponentHealth("unknown", False, type(exc).__name__)

    components = tuple(await asyncio.gather(*(bounded(probe) for probe in probes)))
    return HealthReport(components, all(component.healthy for component in components))


def scheduler_freshness(
    *, last_success: datetime | None, now: datetime, maximum_age: timedelta = timedelta(hours=30)
) -> ComponentHealth:
    if last_success is None:
        return ComponentHealth("scheduler", False, "never_succeeded")
    age = now - last_success
    return ComponentHealth(
        "scheduler",
        age <= maximum_age,
        "fresh" if age <= maximum_age else "stale",
    )
