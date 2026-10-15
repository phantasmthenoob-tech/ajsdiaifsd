"""Durable task persistence.

Task and step state live in the database (never only in memory), so a restart
cannot silently forget a job or leave a task looking successful when it is not.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import aiosqlite

from agent.models import StepStatus, Task, TaskKind, TaskStatus, TaskStep
from database.database import Database
from utils import utcnow_iso


def _row_to_task(row: aiosqlite.Row) -> Task:
    return Task(
        id=row["id"],
        requester_id=int(row["requester_id"]),
        guild_id=int(row["guild_id"]) if row["guild_id"] is not None else None,
        channel_id=int(row["channel_id"]) if row["channel_id"] is not None else None,
        prompt=row["prompt"],
        kind=TaskKind(row["kind"]),
        status=TaskStatus(row["status"]),
        plan_summary=row["plan_summary"],
        tool_calls=row["tool_calls"],
        max_tool_calls=row["max_tool_calls"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        deadline=row["deadline"],
        result=row["result"],
        error=row["error"],
        approval_id=row["approval_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_step(row: aiosqlite.Row) -> TaskStep:
    args: dict[str, Any] = {}
    if row["args"]:
        try:
            args = json.loads(row["args"])
        except json.JSONDecodeError:
            args = {}
    return TaskStep(
        id=row["id"],
        task_id=row["task_id"],
        step_index=row["step_index"],
        name=row["name"],
        tool=row["tool"],
        args=args,
        status=StepStatus(row["status"]),
        attempts=row["attempts"],
        result=row["result"],
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class TaskStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, task: Task) -> Task:
        now = utcnow_iso()
        task_id = await self.db.execute(
            """
            INSERT INTO tasks
                (requester_id, guild_id, channel_id, prompt, kind, status, plan_summary,
                 tool_calls, max_tool_calls, attempts, max_attempts, deadline, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(task.requester_id),
                str(task.guild_id) if task.guild_id is not None else None,
                str(task.channel_id) if task.channel_id is not None else None,
                task.prompt,
                task.kind.value,
                task.status.value,
                task.plan_summary,
                0,
                task.max_tool_calls,
                0,
                task.max_attempts,
                task.deadline,
                now,
                now,
            ),
        )
        task.id = task_id
        task.created_at = now
        task.updated_at = now
        return task

    async def add_steps(self, task_id: int, steps: list[TaskStep]) -> None:
        now = utcnow_iso()
        for step in steps:
            step_id = await self.db.execute(
                """
                INSERT INTO task_steps
                    (task_id, step_index, name, tool, args, status, attempts, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    step.step_index,
                    step.name,
                    step.tool,
                    json.dumps(step.args),
                    step.status.value,
                    step.attempts,
                    now,
                    now,
                ),
            )
            step.id = step_id
            step.task_id = task_id

    async def get(self, task_id: int, *, with_steps: bool = True) -> Optional[Task]:
        row = await self.db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if not row:
            return None
        task = _row_to_task(row)
        if with_steps:
            step_rows = await self.db.fetchall(
                "SELECT * FROM task_steps WHERE task_id = ? ORDER BY step_index ASC", (task_id,)
            )
            task.steps = [_row_to_step(r) for r in step_rows]
        return task

    async def update_task(self, task_id: int, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "status",
            "plan_summary",
            "tool_calls",
            "attempts",
            "result",
            "error",
            "approval_id",
            "deadline",
        }
        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key} = ?")
            params.append(value.value if isinstance(value, (TaskStatus, TaskKind)) else value)
        if not sets:
            return
        sets.append("updated_at = ?")
        params.append(utcnow_iso())
        params.append(task_id)
        await self.db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", params)

    async def update_step(self, step_id: int, **fields: Any) -> None:
        if not fields:
            return
        allowed = {"status", "attempts", "result", "error", "args"}
        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "status" and isinstance(value, StepStatus):
                value = value.value
            if key == "args" and not isinstance(value, str):
                value = json.dumps(value)
            sets.append(f"{key} = ?")
            params.append(value)
        if not sets:
            return
        sets.append("updated_at = ?")
        params.append(utcnow_iso())
        params.append(step_id)
        await self.db.execute(f"UPDATE task_steps SET {', '.join(sets)} WHERE id = ?", params)

    async def increment_tool_calls(self, task_id: int) -> int:
        await self.db.execute(
            "UPDATE tasks SET tool_calls = tool_calls + 1, updated_at = ? WHERE id = ?",
            (utcnow_iso(), task_id),
        )
        row = await self.db.fetchone("SELECT tool_calls FROM tasks WHERE id = ?", (task_id,))
        return int(row["tool_calls"]) if row else 0

    async def list_by_status(self, statuses: list[TaskStatus], *, limit: int = 50) -> list[Task]:
        placeholders = ",".join("?" for _ in statuses)
        rows = await self.db.fetchall(
            f"SELECT * FROM tasks WHERE status IN ({placeholders}) ORDER BY id ASC LIMIT ?",
            [*(s.value for s in statuses), limit],
        )
        return [_row_to_task(r) for r in rows]

    async def list_recent(self, *, limit: int = 10) -> list[Task]:
        rows = await self.db.fetchall(
            "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [_row_to_task(r) for r in rows]

    async def recover_orphans(self) -> list[int]:
        """Reset tasks left RUNNING by a previous process so they can resume."""

        rows = await self.db.fetchall(
            "SELECT id FROM tasks WHERE status = ?", (TaskStatus.RUNNING.value,)
        )
        ids = [int(r["id"]) for r in rows]
        for task_id in ids:
            await self.update_task(task_id, status=TaskStatus.PENDING)
        return ids
