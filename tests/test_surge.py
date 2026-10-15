"""Surge publisher tests: domain allowlist, build validation, dry run."""

from __future__ import annotations

import pytest

from infra.surge import SurgeError, SurgePublisher

INDEX = {"index.html": "<h1>hi</h1>"}


def test_domain_must_be_valid():
    publisher = SurgePublisher(dry_run=True, allowed_domains=["demo.surge.sh"])
    with pytest.raises(SurgeError):
        publisher.check_domain("not a domain")
    assert publisher.check_domain("demo.surge.sh") == "demo.surge.sh"


def test_domain_must_be_on_the_allowlist():
    publisher = SurgePublisher(dry_run=True, allowed_domains=["demo.surge.sh"])
    with pytest.raises(SurgeError):
        publisher.check_domain("evil.surge.sh")


def test_no_allowlist_means_no_publishing():
    publisher = SurgePublisher(dry_run=True, allowed_domains=[])
    with pytest.raises(SurgeError):
        publisher.check_domain("demo.surge.sh")


def test_wildcard_allowlist_matches_subdomains():
    publisher = SurgePublisher(dry_run=True, allowed_domains=["*.example.com"])
    assert publisher.check_domain("app.example.com") == "app.example.com"
    with pytest.raises(SurgeError):
        publisher.check_domain("example.com")


async def test_dry_run_publish_reports_not_executed():
    publisher = SurgePublisher(dry_run=True, allowed_domains=["demo.surge.sh"])
    result = await publisher.publish(domain="demo.surge.sh", files=INDEX)
    assert result.executed is False
    assert result.url == "https://demo.surge.sh"


async def test_publish_requires_index_html():
    publisher = SurgePublisher(dry_run=True, allowed_domains=["demo.surge.sh"])
    with pytest.raises(SurgeError):
        await publisher.publish(domain="demo.surge.sh", files={"a.txt": "x"})


async def test_publish_rejects_unsafe_paths():
    publisher = SurgePublisher(dry_run=True, allowed_domains=["demo.surge.sh"])
    with pytest.raises(SurgeError):
        await publisher.publish(
            domain="demo.surge.sh", files={"../index.html": "<h1>x</h1>"}
        )


async def test_publish_rejects_empty_build():
    publisher = SurgePublisher(dry_run=True, allowed_domains=["demo.surge.sh"])
    with pytest.raises(SurgeError):
        await publisher.publish(domain="demo.surge.sh", files={})
