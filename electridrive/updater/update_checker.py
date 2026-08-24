"""Check the official ElectriDrive GitHub releases feed for stable updates.

This module deliberately has no Qt dependency.  Version parsing, release selection,
throttling, persistence decisions, and network handling can therefore be exercised
without starting the desktop application (or contacting GitHub in tests).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from functools import total_ordering
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

LOGGER = logging.getLogger(__name__)

GITHUB_OWNER = "ElectriCity-AI-Systems"
GITHUB_REPOSITORY = "electri-city-gdrive"
GITHUB_RELEASES_API_URL = (
    f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/releases?per_page=100"
)
GITHUB_RELEASES_PAGE_URL = (
    f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/releases"
)
CHECK_INTERVAL = timedelta(hours=24)
NETWORK_TIMEOUT_SECONDS = 8.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

_SEMVER_RE = re.compile(
    r"^(?:v)?"
    r"(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>"
    r"(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*"
    r"))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


class UpdateCheckError(RuntimeError):
    """A safe, user-displayable classification of an update-check failure."""


@total_ordering
@dataclass(frozen=True)
class SemanticVersion:
    """A strict Semantic Versioning 2.0.0 value."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[int | str, ...] = ()

    @classmethod
    def parse(cls, value: str) -> "SemanticVersion":
        if not isinstance(value, str):
            raise ValueError("version must be text")
        match = _SEMVER_RE.fullmatch(value.strip())
        if not match:
            raise ValueError(f"invalid semantic version: {value!r}")
        prerelease: list[int | str] = []
        raw_prerelease = match.group("prerelease")
        if raw_prerelease:
            for part in raw_prerelease.split("."):
                prerelease.append(int(part) if part.isdigit() else part)
        return cls(
            int(match.group("major")),
            int(match.group("minor")),
            int(match.group("patch")),
            tuple(prerelease),
        )

    @property
    def is_stable(self) -> bool:
        return not self.prerelease

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        if not self.prerelease:
            return base
        return base + "-" + ".".join(str(item) for item in self.prerelease)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        own_core = (self.major, self.minor, self.patch)
        other_core = (other.major, other.minor, other.patch)
        if own_core != other_core:
            return own_core < other_core
        if not self.prerelease:
            return False
        if not other.prerelease:
            return True
        for own, theirs in zip(self.prerelease, other.prerelease):
            if own == theirs:
                continue
            if isinstance(own, int) and isinstance(theirs, str):
                return True
            if isinstance(own, str) and isinstance(theirs, int):
                return False
            return own < theirs
        return len(self.prerelease) < len(other.prerelease)


@dataclass(frozen=True)
class ReleaseInfo:
    version: SemanticVersion
    tag_name: str
    page_url: str


def _canonical_release_url(tag_name: str) -> str:
    """Build a trusted URL instead of accepting a URL from release metadata."""
    return f"{GITHUB_RELEASES_PAGE_URL}/tag/{quote(tag_name, safe='')}"


def find_latest_stable_release(payload: Any) -> ReleaseInfo | None:
    """Select the greatest valid, non-draft, non-prerelease GitHub release.

    Invalid entries are ignored.  In particular, a prerelease-looking SemVer remains
    excluded even if malformed API metadata marks it as a normal release.
    """
    if not isinstance(payload, list):
        return None

    candidates: list[ReleaseInfo] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        if item.get("draft", False) is not False:
            continue
        if item.get("prerelease", False) is not False:
            continue
        tag_name = item.get("tag_name")
        if not isinstance(tag_name, str) or not tag_name:
            continue
        try:
            version = SemanticVersion.parse(tag_name)
        except ValueError:
            continue
        if not version.is_stable:
            continue
        candidates.append(
            ReleaseInfo(version, tag_name, _canonical_release_url(tag_name))
        )
    return max(candidates, key=lambda release: release.version, default=None)


class _OfficialGitHubRedirectHandler(HTTPRedirectHandler):
    """Reject redirects away from GitHub's HTTPS API host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        parsed = urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname != "api.github.com":
            raise UpdateCheckError("GitHub returned an untrusted redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_official_github(request: Request, timeout: float) -> bytes:
    parsed = urlsplit(request.full_url)
    if parsed.scheme != "https" or parsed.hostname != "api.github.com":
        raise UpdateCheckError("Update checks require the official HTTPS GitHub API")
    opener = build_opener(_OfficialGitHubRedirectHandler())
    with opener.open(request, timeout=timeout) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise UpdateCheckError("GitHub release metadata was unexpectedly large")
    return body


class ReleaseSource(Protocol):
    def latest_stable(self) -> ReleaseInfo | None: ...


class GitHubReleaseSource:
    """Retrieve public release metadata from ElectriDrive's official repository."""

    def __init__(
        self,
        *,
        transport: Callable[[Request, float], bytes] = _read_official_github,
        timeout: float = NETWORK_TIMEOUT_SECONDS,
    ):
        self._transport = transport
        self._timeout = timeout

    def latest_stable(self) -> ReleaseInfo | None:
        request = Request(
            GITHUB_RELEASES_API_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "ElectriDrive-update-checker",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="GET",
        )
        try:
            body = self._transport(request, self._timeout)
            payload = json.loads(body.decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise UpdateCheckError("Could not reach GitHub") from exc
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            raise UpdateCheckError("GitHub returned invalid release metadata") from exc
        if not isinstance(payload, list):
            raise UpdateCheckError("GitHub returned invalid release metadata")
        return find_latest_stable_release(payload)


class UpdateChecker:
    """Compare the installed version against the latest stable official release."""

    def __init__(self, source: ReleaseSource | None = None):
        self._source = source or GitHubReleaseSource()

    def available_update(self, current_version: str) -> ReleaseInfo | None:
        current = SemanticVersion.parse(current_version)
        latest = self._source.latest_stable()
        if latest is None or latest.version <= current:
            return None
        return latest


class CheckStatus(str, Enum):
    AVAILABLE = "available"
    UP_TO_DATE = "up_to_date"
    DISMISSED = "dismissed"
    THROTTLED = "throttled"
    ERROR = "error"


@dataclass(frozen=True)
class CheckResult:
    status: CheckStatus
    release: ReleaseInfo | None = None
    error_message: str = ""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc).replace(microsecond=0)
    return normalized.isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class _UpdateSettings(Protocol):
    last_update_check: str
    dismissed_update_version: str


class UpdateCheckService:
    """Apply throttling and persisted dismissal policy around an update checker."""

    def __init__(
        self,
        settings: _UpdateSettings,
        save_settings: Callable[[], None],
        *,
        current_version: str,
        checker: UpdateChecker | None = None,
        now: Callable[[], datetime] = _utc_now,
        interval: timedelta = CHECK_INTERVAL,
    ):
        self._settings = settings
        self._save_settings = save_settings
        self._current_version = current_version
        self._checker = checker or UpdateChecker()
        self._now = now
        self._interval = interval

    def automatic_check_is_due(self) -> bool:
        last_check = _parse_timestamp(self._settings.last_update_check)
        if last_check is None:
            return True
        elapsed = self._now().astimezone(timezone.utc) - last_check
        return elapsed >= self._interval

    def check(self, *, manual: bool = False) -> CheckResult:
        if not manual and not self.automatic_check_is_due():
            return CheckResult(CheckStatus.THROTTLED)

        # Persist the attempt before touching the network.  Offline users are not
        # subjected to a fresh timeout on every launch.
        self._settings.last_update_check = _format_timestamp(self._now())
        self._save_settings()

        try:
            release = self._checker.available_update(self._current_version)
        except Exception as exc:
            # Automatic checks must never affect application usability.  Manual checks
            # receive a concise result while details stay in debug logs.
            LOGGER.debug("Update check failed: %s", exc, exc_info=True)
            return CheckResult(
                CheckStatus.ERROR,
                error_message="Could not check for updates. Check your network connection and try again.",
            )

        if release is None:
            return CheckResult(CheckStatus.UP_TO_DATE)
        if not manual and str(release.version) == self._settings.dismissed_update_version:
            return CheckResult(CheckStatus.DISMISSED, release)
        return CheckResult(CheckStatus.AVAILABLE, release)

    def dismiss(self, release: ReleaseInfo) -> None:
        self._settings.dismissed_update_version = str(release.version)
        self._save_settings()
