"""Bounded graceful shutdown for lease workers."""

from __future__ import annotations

import asyncio


class ShutdownCoordinator:
    def __init__(self) -> None:
        self.accepting_leases = True
        self._tasks: set[asyncio.Task[object]] = set()

    def track(self, task: asyncio.Task[object]) -> None:
        if not self.accepting_leases:
            raise RuntimeError("shutdown has started")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def shutdown(self, *, timeout_seconds: float = 20.0) -> bool:
        self.accepting_leases = False
        if not self._tasks:
            return True
        _done, pending = await asyncio.wait(self._tasks, timeout=timeout_seconds)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return not pending
