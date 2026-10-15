"""Permission model tests: explicit grants, owner bypass, expiry, admin lockdown."""

from __future__ import annotations

import pytest

from permissions.models import PermissionScope, ResourceType
from permissions.service import PermissionError_

from tests.conftest import OWNER_ID


async def test_owner_is_implicitly_authorized(config, permission_service):
    decision = await permission_service.check(
        principal_id=OWNER_ID, scope=PermissionScope.REPO_WRITE
    )
    assert decision.allowed is True
    assert decision.reason == "owner"


async def test_non_owner_has_no_permissions_by_default(config, permission_service):
    decision = await permission_service.check(
        principal_id=12345, scope=PermissionScope.REPO_READ
    )
    assert decision.allowed is False
    assert "no repo_read grant" in decision.reason


async def test_grant_and_check_specific_resource(config, permission_service):
    await permission_service.grant(
        principal_id=12345,
        scope=PermissionScope.REPO_WRITE,
        resource_type=ResourceType.REPO,
        resource_id="owner/repo-a",
        actor_id=OWNER_ID,
    )
    allowed = await permission_service.check(
        principal_id=12345,
        scope=PermissionScope.REPO_WRITE,
        resource_type=ResourceType.REPO,
        resource_id="owner/repo-a",
    )
    denied = await permission_service.check(
        principal_id=12345,
        scope=PermissionScope.REPO_WRITE,
        resource_type=ResourceType.REPO,
        resource_id="owner/repo-b",
    )
    assert allowed.allowed and not denied.allowed


async def test_wildcard_grant_covers_all_resources(config, permission_service):
    await permission_service.grant(
        principal_id=12345,
        scope=PermissionScope.REPO_READ,
        resource_type=ResourceType.REPO,
        resource_id="*",
        actor_id=OWNER_ID,
    )
    assert (
        await permission_service.check(
            principal_id=12345,
            scope=PermissionScope.REPO_READ,
            resource_type=ResourceType.REPO,
            resource_id="anything/at-all",
        )
    ).allowed


async def test_only_owner_may_grant_or_revoke(config, permission_service):
    with pytest.raises(PermissionError_):
        await permission_service.grant(
            principal_id=1,
            scope=PermissionScope.REPO_READ,
            resource_type=ResourceType.REPO,
            resource_id="x/y",
            actor_id=999,
        )
    with pytest.raises(PermissionError_):
        await permission_service.revoke(
            principal_id=1,
            scope=PermissionScope.REPO_READ,
            resource_type=ResourceType.REPO,
            resource_id="x/y",
            actor_id=999,
        )


async def test_expired_grant_is_not_honored(config, permission_service):
    await permission_service.grant(
        principal_id=12345,
        scope=PermissionScope.DEPLOY_PROD,
        resource_type=ResourceType.SERVICE,
        resource_id="svc",
        actor_id=OWNER_ID,
        expires_at="2000-01-01T00:00:00+00:00",
    )
    decision = await permission_service.check(
        principal_id=12345,
        scope=PermissionScope.DEPLOY_PROD,
        resource_type=ResourceType.SERVICE,
        resource_id="svc",
    )
    assert decision.allowed is False


async def test_auto_prod_flag(config, permission_service):
    assert await permission_service.has_auto_prod(principal_id=OWNER_ID, service="app") is False
    await permission_service.grant(
        principal_id=OWNER_ID,
        scope=PermissionScope.DEPLOY_PROD,
        resource_type=ResourceType.SERVICE,
        resource_id="app",
        actor_id=OWNER_ID,
        auto_prod=True,
    )
    assert await permission_service.has_auto_prod(principal_id=OWNER_ID, service="app") is True


async def test_revoke_removes_access(config, permission_service):
    await permission_service.grant(
        principal_id=12345,
        scope=PermissionScope.VM_EXEC,
        resource_type=ResourceType.VM,
        resource_id="vm1",
        actor_id=OWNER_ID,
    )
    removed = await permission_service.revoke(
        principal_id=12345,
        scope=PermissionScope.VM_EXEC,
        resource_type=ResourceType.VM,
        resource_id="vm1",
        actor_id=OWNER_ID,
    )
    assert removed is True
    assert (
        await permission_service.check(
            principal_id=12345,
            scope=PermissionScope.VM_EXEC,
            resource_type=ResourceType.VM,
            resource_id="vm1",
        )
    ).allowed is False


async def test_grants_are_recorded_and_listed(config, permission_service):
    await permission_service.grant(
        principal_id=12345,
        scope=PermissionScope.REPO_READ,
        resource_type=ResourceType.REPO,
        resource_id="a/b",
        actor_id=OWNER_ID,
    )
    grants = await permission_service.list_grants(12345)
    assert len(grants) == 1 and grants[0].resource_id == "a/b"
