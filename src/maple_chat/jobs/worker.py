"""Bounded lease worker with transactional acknowledgement and graceful stop."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maple_chat.jobs.queue import JobLease, acknowledge_job, fail_job, lease_jobs
from maple_chat.ops.shutdown import ShutdownCoordinator

JobHandler = Callable[[JobLease], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class WorkerTick:
    leased: int
    succeeded: int
    failed: int


class LeaseWorker:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        worker_id: str,
        handlers: dict[str, JobHandler],
        shutdown: ShutdownCoordinator | None = None,
    ) -> None:
        self.factory = factory
        self.worker_id = worker_id
        self.handlers = handlers
        self.shutdown = shutdown or ShutdownCoordinator()

    async def tick(self, *, limit: int = 1) -> WorkerTick:
        if not self.shutdown.accepting_leases:
            return WorkerTick(0, 0, 0)
        leases = await lease_jobs(self.factory, worker_id=self.worker_id, limit=limit)
        succeeded = 0
        failed = 0
        for lease in leases:
            handler = self.handlers.get(lease.job_type)
            if handler is None:
                async with self.factory() as session, session.begin():
                    await fail_job(
                        session,
                        job_id=lease.id,
                        worker_id=self.worker_id,
                        error_code="unknown_job_type",
                    )
                failed += 1
                continue
            try:
                await handler(lease)
                async with self.factory() as session, session.begin():
                    acknowledged = await acknowledge_job(
                        session, job_id=lease.id, worker_id=self.worker_id
                    )
                    if not acknowledged:
                        raise RuntimeError("job lease ownership was lost before acknowledgement")
                succeeded += 1
            except Exception as exc:
                async with self.factory() as session, session.begin():
                    await fail_job(
                        session,
                        job_id=lease.id,
                        worker_id=self.worker_id,
                        error_code=f"handler_failed:{type(exc).__name__}",
                    )
                failed += 1
        return WorkerTick(len(leases), succeeded, failed)
