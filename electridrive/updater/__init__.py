"""Safe, user-controlled update discovery for ElectriDrive."""

from electridrive.updater.update_checker import (
    CHECK_INTERVAL,
    GITHUB_RELEASES_API_URL,
    GITHUB_RELEASES_PAGE_URL,
    CheckResult,
    CheckStatus,
    GitHubReleaseSource,
    ReleaseInfo,
    SemanticVersion,
    UpdateChecker,
    UpdateCheckService,
    find_latest_stable_release,
)

__all__ = [
    "CHECK_INTERVAL",
    "GITHUB_RELEASES_API_URL",
    "GITHUB_RELEASES_PAGE_URL",
    "CheckResult",
    "CheckStatus",
    "GitHubReleaseSource",
    "ReleaseInfo",
    "SemanticVersion",
    "UpdateChecker",
    "UpdateCheckService",
    "find_latest_stable_release",
]
