"""GitHub domain models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class RepoRef:
    full_name: str

    @property
    def owner(self) -> str:
        return self.full_name.split("/", 1)[0]

    @property
    def name(self) -> str:
        return self.full_name.split("/", 1)[1] if "/" in self.full_name else self.full_name

    @classmethod
    def parse(cls, full_name: str) -> "RepoRef":
        cleaned = (full_name or "").strip().removeprefix("https://github.com/").strip("/")
        if cleaned.endswith(".git"):
            cleaned = cleaned[:-4]
        if "/" not in cleaned:
            raise ValueError(f"Repository must be 'owner/name', got {full_name!r}")
        return cls(cleaned)


@dataclass
class BranchInfo:
    name: str
    sha: str


@dataclass
class FileContent:
    path: str
    content: str
    sha: Optional[str] = None


@dataclass
class PullRequestInfo:
    number: int
    url: str
    title: str
    head: str
    base: str
    state: str = "open"


@dataclass
class WorkflowRun:
    id: int
    name: str
    status: str
    conclusion: Optional[str] = None
    url: Optional[str] = None
