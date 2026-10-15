"""Approval workflow for destructive or privileged operations.

An approval captures the **exact operation and target**, expires after a
configurable period, and can only be decided by the configured owner (matched by
numeric Discord ID). Approvals are single-use: they must be consumed by the
operation they authorize.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

import aiosqlite

from config import Config
from database.database import Database
from utils import utcnow_iso

logger = logging.getLogger(__name__)


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CONSUMED = "consumed"


@dataclass
class ApprovalRequest:
    operation: str
    target: str
    requested_by: int
    owner_id: int
    expires_at: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    reason: Optional[str] = None
    task_id: Optional[int] = None
    decided_at: Optional[str] = None
    decided_by: Optional[int] = None
    id: Optional[int] = None
    created_at: Optional[str] = None

    def is_expired(self, *, now: Optional[str] = None) -> bool:
        return self.expires_at <= (now or utcnow_iso())

    @property
    def is_usable(self) -> bool:
        return self.status is ApprovalStatus.PENDING and not self.is_expired()

    def describe(self) -> str:
        where = f" on `{self.target}`" if self.target else ""
        return f"`{self.operation}`{where}"


def _row_to_request(row: aiosqlite.Row) -> ApprovalRequest:
    return ApprovalRequest(
        id=row["id"],
        owner_id=int(row["owner_id"]),
        operation=row["operation"],
        target=row["target"],
        reason=row["reason"],
        requested_by=int(row["requested_by"]),
        status=ApprovalStatus(row["status"]),
        task_id=row["task_id"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        decided_at=row["decided_at"],
        decided_by=int(row["decided_by"]) if row["decided_by"] is not None else None,
    )


class ApprovalError(Exception):
    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class ApprovalService:
    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db

    # -- lifecycle ----------------------------------------------------------
    async def request(
        self,
        *,
        operation: str,
        target: str,
        requested_by: int,
        reason: Optional[str] = None,
        task_id: Optional[int] = None,
        ttl_seconds: Optional[int] = None,
    ) -> ApprovalRequest:
        ttl = ttl_seconds or self.config.agent.approval_ttl_seconds
        expires = (
            datetime.now(timezone.utc) + timedelta(seconds=ttl)
        ).replace(microsecond=0).isoformat()
        approval_id = await self.db.execute(
            """
            INSERT INTO approval_requests
                (owner_id, operation, target, reason, requested_by, status, task_id, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                str(self.config.owner_id),
                operation,
                target,
                reason,
                str(requested_by),
                task_id,
                utcnow_iso(),
                expires,
            ),
        )
        created = utcnow_iso()
        logger.info("Approval #%s requested: %s on %s", approval_id, operation, target)
        return ApprovalRequest(
            id=approval_id,
            owner_id=self.config.owner_id,
            operation=operation,
            target=target,
            reason=reason,
            requested_by=requested_by,
            task_id=task_id,
            expires_at=expires,
            created_at=created,
        )

    async def get(self, approval_id: int) -> Optional[ApprovalRequest]:
        row = await self.db.fetchone(
            "SELECT * FROM approval_requests WHERE id = ?", (approval_id,)
        )
        if not row:
            return None
        request = _row_to_request(row)
        if request.status is ApprovalStatus.PENDING and request.is_expired():
            await self._set_status(approval_id, ApprovalStatus.EXPIRED)
            request.status = ApprovalStatus.EXPIRED
        return request

    async def list_pending(self) -> list[ApprovalRequest]:
        rows = await self.db.fetchall(
            "SELECT * FROM approval_requests WHERE status = 'pending' ORDER BY id ASC"
        )
        now = utcnow_iso()
        pending: list[ApprovalRequest] = []
        for row in rows:
            request = _row_to_request(row)
            if request.expires_at <= now:
                await self._set_status(request.id, ApprovalStatus.EXPIRED)  # type: ignore[arg-type]
                continue
            pending.append(request)
        return pending

    async def decide(
        self, approval_id: int, *, actor_id: int, approve: bool
    ) -> ApprovalRequest:
        """Approve or deny. Only the configured owner may decide."""

        if not self.config.is_owner(actor_id):
            logger.warning(
                "Denied approval decision for #%s by non-owner id %s", approval_id, actor_id
            )
            raise ApprovalError("Only the bot owner can approve or deny operations.")

        request = await self.get(approval_id)
        if request is None:
            raise ApprovalError(f"There is no approval request #{approval_id}.")
        if request.status is ApprovalStatus.EXPIRED:
            raise ApprovalError(f"Approval #{approval_id} has expired.")
        if request.status is not ApprovalStatus.PENDING:
            raise ApprovalError(f"Approval #{approval_id} is already {request.status.value}.")

        await self._set_status(
            approval_id,
            ApprovalStatus.APPROVED if approve else ApprovalStatus.DENIED,
            decided_by=actor_id,
        )
        request.status = ApprovalStatus.APPROVED if approve else ApprovalStatus.DENIED
        request.decided_by = actor_id
        request.decided_at = utcnow_iso()
        logger.info("Approval #%s %s by owner", approval_id, request.status.value)
        return request

    async def consume(self, approval_id: int, *, operation: str, target: str) -> bool:
        """Consume an approved request for exactly the operation it authorized."""

        request = await self.get(approval_id)
        if request is None or request.status is not ApprovalStatus.APPROVED:
            return False
        if request.operation != operation or request.target != target:
            return False
        if request.is_expired():
            await self._set_status(approval_id, ApprovalStatus.EXPIRED)
            return False
        await self._set_status(approval_id, ApprovalStatus.CONSUMED)
        return True

    async def expire_stale(self) -> int:
        rows = await self.db.fetchall(
            "SELECT id FROM approval_requests WHERE status = 'pending' AND expires_at <= ?",
            (utcnow_iso(),),
        )
        for row in rows:
            await self._set_status(int(row["id"]), ApprovalStatus.EXPIRED)
        return len(rows)

    async def _set_status(
        self,
        approval_id: int,
        status: ApprovalStatus,
        *,
        decided_by: Optional[int] = None,
    ) -> None:
        await self.db.execute(
            "UPDATE approval_requests SET status = ?, decided_at = ?, decided_by = ? WHERE id = ?",
            (
                status.value,
                utcnow_iso(),
                str(decided_by) if decided_by is not None else None,
                approval_id,
            ),
        )
