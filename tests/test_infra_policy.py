"""Guardrail tests: names, quotas, confirmation, and deploy concurrency."""

from __future__ import annotations

import pytest

from infra.models import PolicyError
from infra.policy import PolicyGuard, PolicyLimits


def make_guard(**overrides) -> PolicyGuard:
    return PolicyGuard(PolicyLimits(**overrides))


def test_valid_names_are_lowercased():
    guard = make_guard()
    assert guard.validate_name("My-App") == "my-app"
    assert guard.validate_name("app2") == "app2"


@pytest.mark.parametrize("bad", ["", "a", "-leading", "UPPER SPACE", "has_underscore", "x" * 60])
def test_invalid_names_rejected(bad):
    # "UPPER SPACE" -> "upper space" is invalid; "a" is too short; etc.
    guard = make_guard()
    if bad == "UPPER SPACE":
        with pytest.raises(PolicyError):
            guard.validate_name(bad)
    else:
        with pytest.raises(PolicyError):
            guard.validate_name(bad)


def test_create_quota_enforced():
    guard = make_guard(max_targets=2)
    assert guard.check_create(name="one", existing_count=0) == "one"
    assert guard.check_create(name="two", existing_count=1) == "two"
    with pytest.raises(PolicyError):
        guard.check_create(name="three", existing_count=2)


def test_destroy_requires_confirmation():
    guard = make_guard()
    with pytest.raises(PolicyError):
        guard.check_destroy(name="app", confirm=False)
    assert guard.check_destroy(name="app", confirm=True) == "app"


async def test_deploy_slots_are_bounded():
    guard = make_guard(max_concurrent_deploys=1)
    async with guard.deploy_slot():
        with pytest.raises(PolicyError):
            async with guard.deploy_slot():
                pass
    # The slot is released afterwards.
    async with guard.deploy_slot():
        pass
