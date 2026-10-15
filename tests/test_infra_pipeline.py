"""Publisher tests: dry-run behaviour, validation, and secret scrubbing."""

from __future__ import annotations

import pytest

from infra.models import DeployPath, PublishError
from infra.pipeline import (
    DockerImagePublisher,
    GitPushPublisher,
    PublishRequest,
    _write_files,
    scrub_credentials,
)


def test_scrub_credentials():
    assert scrub_credentials("remote https://user:token@host/x.git") == "remote https://***@host/x.git"
    assert scrub_credentials("no creds here") == "no creds here"


async def test_git_publisher_dry_run_does_not_execute():
    publisher = GitPushPublisher(dry_run=True)
    result = await publisher.publish(
        PublishRequest(files={"main.py": "print(1)\n"}, repo="https://example.invalid/r.git", branch="main")
    )
    assert result.path is DeployPath.GIT
    assert result.executed is False
    assert result.reference == "https://example.invalid/r.git#main"


async def test_git_publisher_requires_remote():
    publisher = GitPushPublisher(dry_run=True)
    with pytest.raises(PublishError):
        await publisher.publish(PublishRequest(files={"a.py": "x"}, repo=None))


async def test_git_publisher_requires_files():
    publisher = GitPushPublisher(dry_run=True)
    with pytest.raises(PublishError):
        await publisher.publish(PublishRequest(files={}, repo="https://example.invalid/r.git"))


async def test_docker_publisher_requires_dockerfile():
    publisher = DockerImagePublisher(dry_run=True, registry="registry.example")
    with pytest.raises(PublishError):
        await publisher.publish(PublishRequest(files={"main.py": "x"}))


async def test_docker_publisher_dry_run_builds_image_name():
    publisher = DockerImagePublisher(dry_run=True, registry="registry.example", prefix="bot")
    result = await publisher.publish(
        PublishRequest(files={"Dockerfile": "FROM scratch\n"}, extra={"name": "app"})
    )
    assert result.path is DeployPath.DOCKER
    assert result.executed is False
    assert result.reference == "registry.example/bot-app:latest"


def test_write_files_rejects_traversal(tmp_path):
    with pytest.raises(PublishError):
        _write_files(tmp_path, {"../escape.py": "x"})


def test_write_files_preserves_structure(tmp_path):
    _write_files(tmp_path, {"src/main.py": "print(1)\n"})
    assert (tmp_path / "src" / "main.py").read_text() == "print(1)\n"
