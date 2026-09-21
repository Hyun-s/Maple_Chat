"""Transactional persistence for resumable Agent plans and tool checkpoints."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maple_chat.db.models import (
    AgentCallState,
    AgentRun,
    AgentToolCall,
    Answer,
    AnswerMode,
    RunState,
)
from maple_chat.qa.service import AnswerOutcome, QuestionRequest


class AgentRunAccessError(RuntimeError):
    """A requested run is unavailable to the current requester."""


@dataclass(frozen=True, slots=True)
class StoredToolCall:
    step_index: int
    tool_name: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    failed: bool


@dataclass(frozen=True, slots=True)
class StoredAgentRun:
    run_id: str
    query: str
    state: RunState
    plan: dict[str, Any]
    calls: tuple[StoredToolCall, ...]
    max_tool_calls: int


class AgentRunStore:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        ttl: timedelta = timedelta(days=7),
    ) -> None:
        self.factory = factory
        self.ttl = ttl

    async def existing_answer(self, request_message_id: int) -> AnswerOutcome | None:
        async with self.factory() as session:
            answer = await session.scalar(
                sa.select(Answer).where(Answer.request_message_id == request_message_id)
            )
        if answer is None:
            return None
        return AnswerOutcome(answer.answer_id, answer.mode, "", (), duplicate=True)

    async def create(self, request: QuestionRequest, *, max_tool_calls: int) -> StoredAgentRun:
        run_id = str(uuid.uuid4())
        async with self.factory() as session, session.begin():
            await session.execute(
                sa.select(sa.func.pg_advisory_xact_lock(request.request_message_id))
            )
            existing = await session.scalar(
                sa.select(AgentRun).where(AgentRun.request_message_id == request.request_message_id)
            )
            if existing is None:
                existing = AgentRun(
                    run_id=run_id,
                    guild_id=request.guild_id,
                    channel_id=request.channel_id,
                    request_message_id=request.request_message_id,
                    requester_hash=request.requester_hash,
                    query=request.query,
                    query_hash=hashlib.sha256(request.query.encode()).hexdigest(),
                    state=RunState.QUEUED,
                    plan_json={},
                    checkpoint_json={"next_action": "plan"},
                    tool_call_count=0,
                    max_tool_calls=max_tool_calls,
                    resume_count=0,
                    created_at=request.received_at,
                    updated_at=request.received_at,
                    expires_at=request.received_at + self.ttl,
                )
                session.add(existing)
                await session.flush()
            return await _snapshot(session, existing)

    async def resume(
        self,
        run_id: str,
        *,
        requester_hash: str,
        guild_id: int,
        now: datetime,
    ) -> StoredAgentRun:
        async with self.factory() as session, session.begin():
            run = await session.get(AgentRun, run_id, with_for_update=True)
            if (
                run is None
                or run.requester_hash != requester_hash
                or run.guild_id != guild_id
                or run.expires_at <= now
            ):
                raise AgentRunAccessError("Agent 실행을 찾을 수 없거나 재개 권한이 없습니다.")
            if run.state in {RunState.SUCCEEDED, RunState.CANCELLED}:
                raise AgentRunAccessError("이미 종료된 Agent 실행은 재개할 수 없습니다.")
            run.state = RunState.RUNNING
            run.resume_count += 1
            run.error_code = None
            run.updated_at = now
            return await _snapshot(session, run)

    async def save_plan(
        self, run_id: str, plan: dict[str, Any], *, now: datetime
    ) -> StoredAgentRun:
        async with self.factory() as session, session.begin():
            run = await _required_run(session, run_id, lock=True)
            run.plan_json = plan
            run.state = RunState.RUNNING
            run.checkpoint_json = {"next_action": "act", "completed_tool_calls": 0}
            run.updated_at = now
            return await _snapshot(session, run)

    async def record_tool_call(
        self,
        run_id: str,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
        failed: bool,
        now: datetime,
    ) -> StoredAgentRun:
        async with self.factory() as session, session.begin():
            run = await _required_run(session, run_id, lock=True)
            step_index = run.tool_call_count + 1
            session.add(
                AgentToolCall(
                    run_id=run_id,
                    step_index=step_index,
                    tool_name=tool_name,
                    arguments_json=arguments,
                    result_json=result,
                    state=AgentCallState.FAILED if failed else AgentCallState.SUCCEEDED,
                    error_code="tool_failed" if failed else None,
                    created_at=now,
                )
            )
            run.tool_call_count = step_index
            run.state = RunState.PARTIAL
            run.checkpoint_json = {
                "next_action": "evaluate",
                "completed_tool_calls": step_index,
                "last_tool": tool_name,
            }
            run.updated_at = now
            await session.flush()
            return await _snapshot(session, run)

    async def fail(self, run_id: str, *, error_code: str, now: datetime) -> None:
        async with self.factory() as session, session.begin():
            run = await _required_run(session, run_id, lock=True)
            run.state = RunState.FAILED
            run.error_code = error_code[:128]
            run.checkpoint_json = {
                **run.checkpoint_json,
                "next_action": "resume",
                "error_code": error_code[:128],
            }
            run.updated_at = now

    async def finish(
        self,
        run_id: str,
        request: QuestionRequest,
        *,
        succeeded: bool,
        now: datetime,
    ) -> str:
        async with self.factory() as session, session.begin():
            await session.execute(
                sa.select(sa.func.pg_advisory_xact_lock(request.request_message_id))
            )
            existing = await session.scalar(
                sa.select(Answer).where(Answer.request_message_id == request.request_message_id)
            )
            if existing is not None:
                return existing.answer_id
            run = await _required_run(session, run_id, lock=True)
            answer_id = str(uuid.uuid4())
            session.add(
                Answer(
                    answer_id=answer_id,
                    guild_id=request.guild_id,
                    channel_id=request.channel_id,
                    request_message_id=request.request_message_id,
                    requester_hash=request.requester_hash,
                    query_hash=hashlib.sha256(request.query.encode()).hexdigest(),
                    mode=AnswerMode.DIRECT,
                    created_at=request.received_at,
                    expires_at=request.received_at + self.ttl,
                )
            )
            if succeeded:
                run.state = RunState.SUCCEEDED
                run.error_code = None
                run.finished_at = now
                run.checkpoint_json = {
                    **run.checkpoint_json,
                    "next_action": "complete",
                }
            run.updated_at = now
            await session.flush()
            return answer_id

    async def standalone_answer(self, request: QuestionRequest) -> str:
        """Persist a non-run response such as a configuration or access failure."""
        async with self.factory() as session, session.begin():
            await session.execute(
                sa.select(sa.func.pg_advisory_xact_lock(request.request_message_id))
            )
            existing = await session.scalar(
                sa.select(Answer).where(Answer.request_message_id == request.request_message_id)
            )
            if existing is not None:
                return existing.answer_id
            answer_id = str(uuid.uuid4())
            session.add(
                Answer(
                    answer_id=answer_id,
                    guild_id=request.guild_id,
                    channel_id=request.channel_id,
                    request_message_id=request.request_message_id,
                    requester_hash=request.requester_hash,
                    query_hash=hashlib.sha256(request.query.encode()).hexdigest(),
                    mode=AnswerMode.DIRECT,
                    created_at=request.received_at,
                    expires_at=request.received_at + self.ttl,
                )
            )
            await session.flush()
            return answer_id


async def _required_run(session: AsyncSession, run_id: str, *, lock: bool) -> AgentRun:
    statement = sa.select(AgentRun).where(AgentRun.run_id == run_id)
    if lock:
        statement = statement.with_for_update()
    run = await session.scalar(statement)
    if run is None:
        raise LookupError("Agent run does not exist")
    return run


async def _snapshot(session: AsyncSession, run: AgentRun) -> StoredAgentRun:
    rows = await session.scalars(
        sa.select(AgentToolCall)
        .where(AgentToolCall.run_id == run.run_id)
        .order_by(AgentToolCall.step_index)
    )
    return StoredAgentRun(
        run_id=run.run_id,
        query=run.query,
        state=run.state,
        plan=run.plan_json,
        calls=tuple(
            StoredToolCall(
                step_index=call.step_index,
                tool_name=call.tool_name,
                arguments=call.arguments_json,
                result=call.result_json,
                failed=call.state is AgentCallState.FAILED,
            )
            for call in rows
        ),
        max_tool_calls=run.max_tool_calls,
    )
