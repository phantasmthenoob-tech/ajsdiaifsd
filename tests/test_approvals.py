"""Approval workflow tests: TTL, owner-only decisions, and single-use consumption."""

from __future__ import annotations

import pytest

from approvals.service import ApprovalError, ApprovalStatus

from tests.conftest import OWNER_ID


async def test_request_creates_pending_approval(config, approval_service):
    approval = await approval_service.request(
        operation="repo.commit", target="repo:owner/x", requested_by=OWNER_ID
    )
    assert approval.status is ApprovalStatus.PENDING
    assert approval.owner_id == OWNER_ID
    assert approval.expires_at > approval.created_at


async def test_only_owner_can_decide(config, approval_service):
    approval = await approval_service.request(
        operation="op", target="t", requested_by=OWNER_ID
    )
    with pytest.raises(ApprovalError):
        await approval_service.decide(approval.id, actor_id=999, approve=True)
    # Still pending - a refused decision must not change state.
    fetched = await approval_service.get(approval.id)
    assert fetched.status is ApprovalStatus.PENDING


async def test_owner_can_approve_and_deny(config, approval_service):
    first = await approval_service.request(operation="op", target="t", requested_by=OWNER_ID)
    approved = await approval_service.decide(first.id, actor_id=OWNER_ID, approve=True)
    assert approved.status is ApprovalStatus.APPROVED

    second = await approval_service.request(operation="op", target="t", requested_by=OWNER_ID)
    denied = await approval_service.decide(second.id, actor_id=OWNER_ID, approve=False)
    assert denied.status is ApprovalStatus.DENIED


async def test_consume_requires_exact_operation_and_target(config, approval_service):
    approval = await approval_service.request(
        operation="repo.commit", target="repo:owner/x", requested_by=OWNER_ID
    )
    await approval_service.decide(approval.id, actor_id=OWNER_ID, approve=True)

    assert await approval_service.consume(approval.id, operation="repo.commit", target="repo:owner/y") is False
    assert await approval_service.consume(approval.id, operation="repo.push", target="repo:owner/x") is False
    assert await approval_service.consume(approval.id, operation="repo.commit", target="repo:owner/x") is True
    # Single use.
    assert await approval_service.consume(approval.id, operation="repo.commit", target="repo:owner/x") is False


async def test_expired_approval_is_rejected_and_reaped(config, approval_service):
    approval = await approval_service.request(
        operation="op", target="t", requested_by=OWNER_ID, ttl_seconds=-1
    )
    fetched = await approval_service.get(approval.id)
    assert fetched.status is ApprovalStatus.EXPIRED
    with pytest.raises(ApprovalError):
        await approval_service.decide(approval.id, actor_id=OWNER_ID, approve=True)


async def test_deciding_twice_is_rejected(config, approval_service):
    approval = await approval_service.request(operation="op", target="t", requested_by=OWNER_ID)
    await approval_service.decide(approval.id, actor_id=OWNER_ID, approve=True)
    with pytest.raises(ApprovalError):
        await approval_service.decide(approval.id, actor_id=OWNER_ID, approve=True)


async def test_expire_stale_reaps_pending(config, approval_service):
    await approval_service.request(
        operation="op", target="t", requested_by=OWNER_ID, ttl_seconds=-5
    )
    reaped = await approval_service.expire_stale()
    assert reaped == 1
    assert await approval_service.list_pending() == []
