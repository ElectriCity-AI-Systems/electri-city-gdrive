from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.error import URLError

from electridrive.config import Settings, load_settings, save_settings
from electridrive.updater.update_checker import (
    GITHUB_RELEASES_API_URL,
    CheckStatus,
    GitHubReleaseSource,
    SemanticVersion,
    UpdateChecker,
    UpdateCheckService,
    find_latest_stable_release,
)


def _payload(*versions: str):
    return [
        {"tag_name": f"v{version}", "draft": False, "prerelease": False}
        for version in versions
    ]


def _source_from(payload):
    body = json.dumps(payload).encode("utf-8")
    return GitHubReleaseSource(transport=lambda _request, _timeout: body)


def _service(
    payload,
    *,
    current="2.1.0",
    now=datetime(2026, 8, 24, 12, tzinfo=timezone.utc),
    settings=None,
):
    settings = settings or SimpleNamespace(
        last_update_check="", dismissed_update_version=""
    )
    saves = []
    checker = UpdateChecker(_source_from(payload))
    service = UpdateCheckService(
        settings,
        lambda: saves.append(settings.last_update_check),
        current_version=current,
        checker=checker,
        now=lambda: now,
    )
    return service, settings, saves


def test_current_version_equal_to_latest_has_no_update():
    checker = UpdateChecker(_source_from(_payload("2.1.0")))
    assert checker.available_update("2.1.0") is None


def test_newer_latest_release_is_available():
    checker = UpdateChecker(_source_from(_payload("2.1.1", "2.1.0")))
    release = checker.available_update("2.1.0")
    assert release is not None
    assert str(release.version) == "2.1.1"


def test_current_version_newer_than_latest_never_downgrades():
    checker = UpdateChecker(_source_from(_payload("2.1.0")))
    assert checker.available_update("2.2.0") is None


def test_prerelease_and_draft_releases_are_ignored():
    payload = [
        {"tag_name": "v3.0.0", "draft": True, "prerelease": False},
        {"tag_name": "v2.2.0-beta.1", "draft": False, "prerelease": False},
        {"tag_name": "v2.2.0", "draft": False, "prerelease": True},
        {"tag_name": "v2.1.1", "draft": False, "prerelease": False},
    ]
    latest = find_latest_stable_release(payload)
    assert latest is not None
    assert str(latest.version) == "2.1.1"


def test_malformed_release_entries_are_handled_safely():
    malformed = [None, "2.2.0", {}, {"tag_name": 123}, {"tag_name": "banana"}]
    checker = UpdateChecker(_source_from(malformed))
    assert checker.available_update("2.1.0") is None


def test_malformed_top_level_response_becomes_safe_error_result():
    service, _, _ = _service({"message": "unexpected response"})
    result = service.check()
    assert result.status is CheckStatus.ERROR


def test_network_failure_does_not_escape_automatic_check():
    source = GitHubReleaseSource(
        transport=lambda _request, _timeout: (_ for _ in ()).throw(
            URLError("offline")
        )
    )
    checker = UpdateChecker(source)
    settings = SimpleNamespace(last_update_check="", dismissed_update_version="")
    service = UpdateCheckService(
        settings,
        lambda: None,
        current_version="2.1.0",
        checker=checker,
    )
    assert service.check().status is CheckStatus.ERROR
    assert settings.last_update_check


def test_dismissed_version_stays_suppressed_for_automatic_checks():
    settings = SimpleNamespace(
        last_update_check="", dismissed_update_version="2.1.1"
    )
    service, _, _ = _service(_payload("2.1.1"), settings=settings)
    result = service.check()
    assert result.status is CheckStatus.DISMISSED


def test_newer_version_after_dismissed_version_notifies_again():
    settings = SimpleNamespace(
        last_update_check="", dismissed_update_version="2.1.1"
    )
    service, _, _ = _service(_payload("2.1.2", "2.1.1"), settings=settings)
    result = service.check()
    assert result.status is CheckStatus.AVAILABLE
    assert str(result.release.version) == "2.1.2"


def test_dismissal_is_persisted_as_normalized_semantic_version():
    service, settings, saves = _service(_payload("2.1.1"))
    result = service.check()
    service.dismiss(result.release)
    assert settings.dismissed_update_version == "2.1.1"
    assert len(saves) == 2


def test_automatic_checks_are_throttled_for_24_hours():
    now = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)
    calls = []

    def transport(_request, _timeout):
        calls.append(True)
        return json.dumps(_payload("2.1.1")).encode()

    settings = SimpleNamespace(
        last_update_check=(now - timedelta(hours=23, minutes=59)).isoformat(),
        dismissed_update_version="",
    )
    service = UpdateCheckService(
        settings,
        lambda: None,
        current_version="2.1.0",
        checker=UpdateChecker(GitHubReleaseSource(transport=transport)),
        now=lambda: now,
    )
    assert service.check().status is CheckStatus.THROTTLED
    assert calls == []


def test_automatic_check_runs_at_24_hour_boundary_and_persists_timestamp():
    now = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)
    settings = SimpleNamespace(
        last_update_check=(now - timedelta(hours=24)).isoformat(),
        dismissed_update_version="",
    )
    service, settings, saves = _service(
        _payload("2.1.0"), now=now, settings=settings
    )
    assert service.check().status is CheckStatus.UP_TO_DATE
    assert settings.last_update_check == "2026-08-24T12:00:00Z"
    assert saves == ["2026-08-24T12:00:00Z"]


def test_manual_check_bypasses_throttle_and_reports_available_update():
    now = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)
    settings = SimpleNamespace(
        last_update_check=now.isoformat(), dismissed_update_version="2.1.1"
    )
    service, _, _ = _service(_payload("2.1.1"), now=now, settings=settings)
    result = service.check(manual=True)
    assert result.status is CheckStatus.AVAILABLE


def test_manual_network_failure_returns_concise_error():
    source = GitHubReleaseSource(
        transport=lambda _request, _timeout: (_ for _ in ()).throw(
            TimeoutError("private low-level detail")
        )
    )
    service = UpdateCheckService(
        SimpleNamespace(last_update_check="", dismissed_update_version=""),
        lambda: None,
        current_version="2.1.0",
        checker=UpdateChecker(source),
    )
    result = service.check(manual=True)
    assert result.status is CheckStatus.ERROR
    assert "network connection" in result.error_message
    assert "private low-level detail" not in result.error_message


def test_manual_up_to_date_result_is_explicit():
    service, _, _ = _service(_payload("2.1.0"))
    assert service.check(manual=True).status is CheckStatus.UP_TO_DATE


def test_update_state_round_trips_through_settings_file(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    settings = Settings(
        last_update_check="2026-08-24T12:00:00Z",
        dismissed_update_version="2.1.1",
    )
    save_settings(settings)
    restored = load_settings()
    assert restored.last_update_check == "2026-08-24T12:00:00Z"
    assert restored.dismissed_update_version == "2.1.1"


def test_semantic_numeric_components_are_not_compared_lexically():
    assert SemanticVersion.parse("2.1.9") < SemanticVersion.parse("2.1.10")


def test_release_url_is_canonical_and_metadata_url_is_never_trusted():
    payload = [
        {
            "tag_name": "v2.1.1",
            "draft": False,
            "prerelease": False,
            "html_url": "https://evil.example/download",
        }
    ]
    release = find_latest_stable_release(payload)
    assert release.page_url == (
        "https://github.com/ElectriCity-AI-Systems/"
        "electri-city-gdrive/releases/tag/v2.1.1"
    )


def test_only_official_public_releases_endpoint_is_requested():
    requests = []

    def transport(request, timeout):
        requests.append((request, timeout))
        return b"[]"

    assert GitHubReleaseSource(transport=transport).latest_stable() is None
    assert len(requests) == 1
    assert requests[0][0].full_url == GITHUB_RELEASES_API_URL
    assert requests[0][0].get_method() == "GET"
    assert requests[0][0].get_header("Authorization") is None
