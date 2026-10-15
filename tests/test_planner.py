"""Planner tests: classification, step scopes, and the mandatory verify step."""

from __future__ import annotations

from agent.planner import PlanContext, build_plan, classify, extract_repo
from agent.models import TaskKind
from permissions.models import PermissionScope


def test_classification():
    assert classify("fix the bug in owner/repo") is TaskKind.REPO_CHANGE
    assert classify("why is the deploy failing?") is TaskKind.DIAGNOSE
    assert classify("deploy this to production") is TaskKind.DEPLOY_PROD
    assert classify("deploy this") is TaskKind.DEPLOY_DEV
    assert classify("open a PR for this") is TaskKind.PULL_REQUEST
    assert classify("inspect owner/repo") is TaskKind.REPO_INSPECT
    assert classify("publish the static site to surge") is TaskKind.STATIC_PUBLISH
    assert classify("hello there") is TaskKind.ANSWER


def test_extract_repo_ignores_source_paths():
    assert extract_repo("fix owner/repo please") == "owner/repo"
    assert extract_repo("edit src/main") is None


def test_every_plan_ends_with_verification():
    plan = build_plan("hello")
    assert plan.steps[-1].tool == "task.verify"


def test_repo_change_plan_requires_repo_write_scope():
    plan = build_plan("add a feature to owner/repo")
    scopes = {step.scope for step in plan.steps}
    assert PermissionScope.REPO_WRITE in scopes
    assert plan.steps[0].tool == "repo.get"
    assert plan.steps[0].resource_id == "owner/repo"


def test_production_deploy_is_flagged_destructive():
    plan = build_plan("deploy owner/repo to production")
    deploy_steps = [s for s in plan.steps if s.tool == "render.deploy"]
    assert deploy_steps, "expected a deploy step"
    assert deploy_steps[0].destructive is True
    assert deploy_steps[0].scope is PermissionScope.DEPLOY_PROD


def test_development_deploy_is_not_destructive():
    plan = build_plan("deploy owner/repo to dev")
    deploy_steps = [s for s in plan.steps if s.tool == "render.deploy"]
    assert deploy_steps[0].destructive is False
    assert deploy_steps[0].scope is PermissionScope.DEPLOY_DEV


def test_vm_exec_requires_vm_scope():
    plan = build_plan("run pytest -q", context=PlanContext(repo="owner/repo"))
    worker_steps = [s for s in plan.steps if s.tool == "worker.run"]
    assert worker_steps and worker_steps[0].scope is PermissionScope.VM_EXEC


def test_static_publish_is_destructive_and_domain_scoped():
    plan = build_plan("publish the static site with surge")
    steps = [s for s in plan.steps if s.tool == "surge.publish"]
    assert steps and steps[0].destructive is True
    assert steps[0].resource_id == "*"
