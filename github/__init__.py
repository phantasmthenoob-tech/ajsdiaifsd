"""GitHub integration: App (preferred) and PAT (fallback) behind one interface."""

from github.auth import GitHubAppAuth, GitHubAuth, GitHubAuthError, PATAuth
from github.client import (
    DryRunGitHubIntegration,
    GitHubError,
    GitHubIntegration,
    RestGitHubIntegration,
    build_github,
)
from github.models import BranchInfo, FileContent, PullRequestInfo, RepoRef, WorkflowRun

__all__ = [
    "GitHubAuth",
    "GitHubAppAuth",
    "PATAuth",
    "GitHubAuthError",
    "GitHubIntegration",
    "RestGitHubIntegration",
    "DryRunGitHubIntegration",
    "GitHubError",
    "build_github",
    "RepoRef",
    "BranchInfo",
    "FileContent",
    "PullRequestInfo",
    "WorkflowRun",
]
