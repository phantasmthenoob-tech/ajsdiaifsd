"""Context service: the business rules on top of :mod:`context.repository`.

This is the only place that decides *which* entries apply where, how
``/setcontext`` operations behave, and how context is exported/imported.  Every
method assumes the caller has already performed the owner authorization check;
the service itself never inspects Discord objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from context.models import ContextEntry, Operation, Role, Scope
from context.repository import ContextRepository
from utils import parse_snowflake, utcnow_iso

# Discord's slash-command string option is capped at 6000 characters.
MAX_CONTENT_LENGTH = 6000
MAX_IMPORT_ENTRIES = 1000
EXPORT_FORMAT_VERSION = 1


class ContextError(ValueError):
    """Raised for invalid context operations (safe to show to the owner)."""


@dataclass
class SetContextResult:
    operation: str
    scope: Scope
    target_id: Optional[int]
    role: Optional[Role]
    affected: int
    entry_id: Optional[int] = None
    label: Optional[str] = None

    def describe(self) -> str:
        where = self.scope.value if self.target_id is None else f"{self.scope.value}:{self.target_id}"
        if self.operation == "remove":
            return f"Removed {self.affected} context entr{'y' if self.affected == 1 else 'ies'} from `{where}`."
        if self.operation == "replace":
            return (
                f"Replaced {self.affected} context entr{'y' if self.affected == 1 else 'ies'} "
                f"in `{where}`."
            )
        return f"Added context entry #{self.entry_id} to `{where}` as `{self.role.value if self.role else '?'}`."


class ContextService:
    """Owner-facing context administration."""

    def __init__(self, repository: ContextRepository) -> None:
        self.repository = repository

    # -- validation ---------------------------------------------------------
    @staticmethod
    def validate_role(role: Any) -> Role:
        try:
            parsed = Role.parse(role) if not isinstance(role, Role) else role
        except ValueError as exc:
            raise ContextError(str(exc)) from exc
        if parsed not in (Role.USER, Role.ASSISTANT):
            # system/developer are application-level and must never be
            # user-settable, otherwise stored content could override security.
            raise ContextError(
                "Only `user` and `assistant` roles may be stored. System and developer "
                "instructions are reserved for the application."
            )
        return parsed

    @staticmethod
    def validate_content(content: str) -> str:
        if not isinstance(content, str):
            raise ContextError("content must be text")
        text = content.strip()
        if not text:
            raise ContextError("content must not be empty")
        if len(text) > MAX_CONTENT_LENGTH:
            raise ContextError(f"content exceeds the {MAX_CONTENT_LENGTH}-character limit")
        return text

    @staticmethod
    def validate_label(label: Optional[str]) -> Optional[str]:
        if label is None:
            return None
        text = str(label).strip()
        if not text:
            return None
        if len(text) > 100:
            raise ContextError("label must be 100 characters or fewer")
        return text

    @staticmethod
    def resolve_target(scope: Scope, target: Any) -> Optional[int]:
        """Resolve a scope's target ID, enforcing presence where required."""

        if scope is Scope.ALL:
            # Global context never carries a target; ignore anything supplied.
            return None
        if target is None:
            raise ContextError(f"scope `{scope.value}` requires a target")
        parsed = parse_snowflake(target)
        if parsed is None:
            raise ContextError(
                f"target for scope `{scope.value}` must be a valid Discord ID or mention"
            )
        return parsed

    # -- mutations ----------------------------------------------------------
    async def set_context(
        self,
        *,
        scope: Any,
        content: Optional[str] = None,
        operation: Any = Operation.APPEND,
        target: Any = None,
        role: Any = Role.USER,
        label: Optional[str] = None,
        actor_id: Optional[int] = None,
    ) -> SetContextResult:
        scope_enum = Scope.parse(scope) if not isinstance(scope, Scope) else scope
        operation_enum = (
            Operation.parse(operation) if not isinstance(operation, Operation) else operation
        )
        target_id = self.resolve_target(scope_enum, target)
        label = self.validate_label(label)

        if operation_enum is Operation.REMOVE:
            if label:
                affected = await self.repository.delete_by_label(
                    scope=scope_enum, target_id=target_id, label=label
                )
            else:
                affected = await self.repository.clear(scope=scope_enum, target_id=target_id)
            return SetContextResult(
                operation="remove",
                scope=scope_enum,
                target_id=target_id,
                role=None,
                affected=affected,
                label=label,
            )

        text = self.validate_content(content or "")
        role_enum = self.validate_role(role)

        if operation_enum is Operation.REPLACE:
            if label:
                existing = await self.repository.list_by_label(
                    scope=scope_enum, target_id=target_id, label=label
                )
                if existing:
                    # Update the first, remove any accidental duplicates.
                    await self.repository.update_entry(
                        existing[0].id, content=text, role=role_enum  # type: ignore[arg-type]
                    )
                    for duplicate in existing[1:]:
                        await self.repository.delete_entry(duplicate.id)  # type: ignore[arg-type]
                    return SetContextResult(
                        operation="replace",
                        scope=scope_enum,
                        target_id=target_id,
                        role=role_enum,
                        affected=1,
                        entry_id=existing[0].id,
                        label=label,
                    )
                entry_id = await self.repository.add_entry(
                    scope=scope_enum,
                    role=role_enum,
                    content=text,
                    target_id=target_id,
                    label=label,
                    created_by=actor_id,
                )
                return SetContextResult(
                    operation="replace",
                    scope=scope_enum,
                    target_id=target_id,
                    role=role_enum,
                    affected=1,
                    entry_id=entry_id,
                    label=label,
                )
            # No label: replace the entire scope/target bucket.
            removed = await self.repository.clear(scope=scope_enum, target_id=target_id)
            entry_id = await self.repository.add_entry(
                scope=scope_enum,
                role=role_enum,
                content=text,
                target_id=target_id,
                label=None,
                created_by=actor_id,
            )
            return SetContextResult(
                operation="replace",
                scope=scope_enum,
                target_id=target_id,
                role=role_enum,
                affected=removed,
                entry_id=entry_id,
                label=None,
            )

        # Append. A label acts as an upsert key so owners can keep one canonical
        # entry per topic without flooding the bucket.
        if label:
            existing = await self.repository.list_by_label(
                scope=scope_enum, target_id=target_id, label=label
            )
            if existing:
                await self.repository.update_entry(
                    existing[0].id, content=text, role=role_enum  # type: ignore[arg-type]
                )
                return SetContextResult(
                    operation="append",
                    scope=scope_enum,
                    target_id=target_id,
                    role=role_enum,
                    affected=1,
                    entry_id=existing[0].id,
                    label=label,
                )
        entry_id = await self.repository.add_entry(
            scope=scope_enum,
            role=role_enum,
            content=text,
            target_id=target_id,
            label=label,
            created_by=actor_id,
        )
        return SetContextResult(
            operation="append",
            scope=scope_enum,
            target_id=target_id,
            role=role_enum,
            affected=1,
            entry_id=entry_id,
            label=label,
        )

    async def delete_context(self, entry_id: int) -> bool:
        return await self.repository.delete_entry(entry_id)

    async def clear_context(self, *, scope: Any, target: Any = None) -> int:
        scope_enum = Scope.parse(scope) if not isinstance(scope, Scope) else scope
        target_id = self.resolve_target(scope_enum, target)
        return await self.repository.clear(scope=scope_enum, target_id=target_id)

    # -- queries ------------------------------------------------------------
    async def list_context(
        self,
        *,
        scope: Any = None,
        target: Any = None,
        limit: int = 100,
    ) -> list[ContextEntry]:
        scope_enum: Optional[Scope]
        if scope is None:
            scope_enum = None
        else:
            scope_enum = Scope.parse(scope) if not isinstance(scope, Scope) else scope

        if scope_enum is None:
            return await self.repository.list_entries(limit=limit)
        target_id = self.resolve_target(scope_enum, target)
        return await self.repository.list_entries(scope=scope_enum, target_id=target_id, limit=limit)

    async def get_context(
        self,
        *,
        entry_id: Optional[int] = None,
        label: Optional[str] = None,
        scope: Any = None,
        target: Any = None,
    ) -> list[ContextEntry]:
        if entry_id is not None:
            entry = await self.repository.get_entry(entry_id)
            return [entry] if entry else []
        if label:
            if scope is None:
                raise ContextError("Retrieving by label requires a scope")
            scope_enum = Scope.parse(scope) if not isinstance(scope, Scope) else scope
            target_id = self.resolve_target(scope_enum, target)
            return await self.repository.list_by_label(
                scope=scope_enum, target_id=target_id, label=label
            )
        raise ContextError("Provide either an entry ID or a label with scope")

    async def count_context(self) -> int:
        return await self.repository.count()

    # -- layered resolution -------------------------------------------------
    async def applicable_context(
        self,
        *,
        guild_id: Optional[int],
        channel_id: Optional[int],
        user_id: Optional[int],
    ) -> dict[Scope, list[ContextEntry]]:
        """Return applicable entries grouped by scope layer (priority order)."""

        entries = await self.repository.applicable(
            guild_id=guild_id, channel_id=channel_id, user_id=user_id
        )
        layers: dict[Scope, list[ContextEntry]] = {
            Scope.ALL: [],
            Scope.SERVER: [],
            Scope.CHANNEL: [],
            Scope.USER: [],
        }
        for entry in entries:
            layers[entry.scope].append(entry)
        return layers

    # -- export / import ----------------------------------------------------
    async def export_context(
        self,
        *,
        scope: Any = None,
        target: Any = None,
    ) -> dict[str, Any]:
        entries = await self.list_context(scope=scope, target=target, limit=MAX_IMPORT_ENTRIES)
        return {
            "version": EXPORT_FORMAT_VERSION,
            "exported_at": utcnow_iso(),
            "count": len(entries),
            "entries": [e.to_dict() for e in entries],
        }

    @staticmethod
    def validate_import_payload(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            raise ContextError("Imported JSON must be an object")
        entries = payload.get("entries")
        if not isinstance(entries, list):
            raise ContextError("Imported JSON must contain an `entries` array")
        if len(entries) > MAX_IMPORT_ENTRIES:
            raise ContextError(f"Cannot import more than {MAX_IMPORT_ENTRIES} entries at once")

        validated: list[dict[str, Any]] = []
        for index, raw in enumerate(entries):
            if not isinstance(raw, dict):
                raise ContextError(f"Entry {index} is not an object")
            try:
                scope = Scope.parse(raw.get("scope"))
                role = ContextService.validate_role(raw.get("role", "user"))
            except (ValueError, ContextError) as exc:
                raise ContextError(f"Entry {index} is invalid: {exc}") from exc
            content = ContextService.validate_content(raw.get("content", ""))
            label = ContextService.validate_label(raw.get("label"))
            target_id = ContextService.resolve_target(scope, raw.get("target_id"))
            validated.append(
                {
                    "scope": scope,
                    "role": role,
                    "content": content,
                    "target_id": target_id,
                    "label": label,
                }
            )
        return validated

    async def import_context(
        self, payload: Any, *, actor_id: Optional[int] = None, replace: bool = False
    ) -> int:
        validated = self.validate_import_payload(payload)
        if replace:
            # Wipe each bucket the import touches so the result is deterministic.
            seen: set[tuple[Scope, Optional[int]]] = set()
            for entry in validated:
                key = (entry["scope"], entry["target_id"])
                if key in seen:
                    continue
                seen.add(key)
                await self.repository.clear(scope=entry["scope"], target_id=entry["target_id"])
        count = 0
        for entry in validated:
            await self.repository.add_entry(
                scope=entry["scope"],
                role=entry["role"],
                content=entry["content"],
                target_id=entry["target_id"],
                label=entry["label"],
                created_by=actor_id,
            )
            count += 1
        return count
