"""Permission service: the authorization decision point.

Design rules enforced here:

* The configured owner (numeric ``BOT_OWNER_ID``) is implicitly authorized for
  every scope, because the owner is the one who grants everything else.
* Every other principal needs an explicit, non-expired grant for the exact
  scope and resource.
* Administration (grant/revoke) is owner-only and re-validated by ID.
"""

from __future__ import annotations

import logging

from config import Config
from permissions.models import Decision, Grant, PermissionScope, ResourceType
from permissions.store import PermissionStore
from utils import utcnow_iso

logger = logging.getLogger(__name__)


class PermissionError_(Exception):
    """Raised when an actor may not administer permissions."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class PermissionService:
    def __init__(self, config: Config, store: PermissionStore) -> None:
        self.config = config
        self.store = store

    # -- administration (owner only) ----------------------------------------
    def _require_owner(self, actor_id: int) -> None:
        if not self.config.is_owner(actor_id):
            logger.warning("Permission administration denied for user id %s", actor_id)
            raise PermissionError_("Only the bot owner can modify permissions.")

    async def grant(
        self,
        *,
        principal_id: int,
        scope: PermissionScope,
        resource_type: ResourceType,
        resource_id: str = "*",
        actor_id: int,
        auto_prod: bool = False,
        expires_at: str | None = None,
    ) -> Grant:
        self._require_owner(actor_id)
        if resource_type is ResourceType.GLOBAL:
            resource_id = "*"
        await self.store.upsert(
            principal_id=principal_id,
            scope=scope,
            resource_type=resource_type,
            resource_id=resource_id,
            created_by=actor_id,
            auto_prod=auto_prod,
            expires_at=expires_at,
        )
        return Grant(
            principal_id=principal_id,
            scope=scope,
            resource_type=resource_type,
            resource_id=resource_id,
            created_by=actor_id,
            auto_prod=auto_prod,
        )

    async def revoke(
        self,
        *,
        principal_id: int,
        scope: PermissionScope,
        resource_type: ResourceType,
        resource_id: str = "*",
        actor_id: int,
    ) -> bool:
        self._require_owner(actor_id)
        return await self.store.delete(
            principal_id=principal_id,
            scope=scope,
            resource_type=resource_type,
            resource_id=resource_id,
        )

    async def list_grants(self, principal_id: int | None = None) -> list[Grant]:
        return await self.store.list(principal_id)

    # -- authorization ------------------------------------------------------
    async def check(
        self,
        *,
        principal_id: int,
        scope: PermissionScope,
        resource_type: ResourceType = ResourceType.GLOBAL,
        resource_id: str = "*",
    ) -> Decision:
        """Decide whether ``principal_id`` may perform ``scope`` on a resource."""

        if self.config.is_owner(principal_id):
            return Decision(True, "owner")

        grants = await self.store.for_scope(principal_id, scope)
        now = utcnow_iso()
        for grant in grants:
            if grant.expires_at and grant.expires_at <= now:
                continue
            if grant.matches(scope=scope, resource_type=resource_type, resource_id=resource_id):
                return Decision(True, f"grant #{grant.id}", grant_id=grant.id)
        return Decision(False, f"no {scope.value} grant for {resource_type.value}:{resource_id}")

    async def require(
        self,
        *,
        principal_id: int,
        scope: PermissionScope,
        resource_type: ResourceType = ResourceType.GLOBAL,
        resource_id: str = "*",
    ) -> Decision:
        """Like :meth:`check`, but raise on refusal."""

        decision = await self.check(
            principal_id=principal_id,
            scope=scope,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        if not decision.allowed:
            raise PermissionError_(
                f"Not permitted: `{scope.value}` on `{resource_type.value}:{resource_id}`."
            )
        return decision

    async def has_auto_prod(self, *, principal_id: int, service: str) -> bool:
        """Whether production deploys to ``service`` are preauthorized."""

        if self.config.is_owner(principal_id):
            grants = await self.store.for_scope(principal_id, PermissionScope.DEPLOY_PROD)
            return any(
                grant.auto_prod
                and grant.matches(
                    scope=PermissionScope.DEPLOY_PROD,
                    resource_type=ResourceType.SERVICE,
                    resource_id=service,
                )
                for grant in grants
            )
        grants = await self.store.for_scope(principal_id, PermissionScope.DEPLOY_PROD)
        return any(grant.auto_prod and grant.matches(
            scope=PermissionScope.DEPLOY_PROD,
            resource_type=ResourceType.SERVICE,
            resource_id=service,
        ) for grant in grants)
