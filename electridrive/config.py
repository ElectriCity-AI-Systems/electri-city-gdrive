from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

APP_ID = "electridrive"
APP_NAME = "ElectriDrive"
APP_TAGLINE = "Electric-City Drive for Linux"
APP_VERSION = "2.0.0"

DEFAULT_SCOPE = "https://www.googleapis.com/auth/drive.file"
FULL_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"

# --- ElectriDrive Pro (full-Drive tier) marketing config (all overridable) ---------
# "Pay what you want" donation model — undercuts every competitor (overGrive $4.99,
# Insync $29.99). Supporters donate, then receive a license key to activate Pro.
PRO_PRICE = os.environ.get("ELECTRIDRIVE_PRO_PRICE", "Pay what you want")
PRO_URL = os.environ.get(
    "ELECTRIDRIVE_PRO_URL",
    "https://www.paypal.com/donate/?hosted_button_id=SATEMACLEGSTL",
)
PRO_COMPARE = "name your price · vs overGrive $4.99 · Insync $29.99"


def xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def xdg_state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))


def xdg_cache_home() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))


@dataclass(frozen=True)
class AppPaths:
    config_dir: Path
    state_dir: Path
    cache_dir: Path
    credentials_file: Path
    token_fallback_file: Path
    database_file: Path
    log_file: Path
    settings_file: Path
    vfs_cache_dir: Path


def get_paths() -> AppPaths:
    config_dir = xdg_config_home() / APP_ID
    state_dir = xdg_state_home() / APP_ID
    cache_dir = xdg_cache_home() / APP_ID
    vfs_cache_dir = cache_dir / "vfs"
    for path in (config_dir, state_dir, cache_dir, vfs_cache_dir):
        path.mkdir(parents=True, exist_ok=True)
    return AppPaths(
        config_dir=config_dir,
        state_dir=state_dir,
        cache_dir=cache_dir,
        credentials_file=config_dir / "credentials.json",
        token_fallback_file=config_dir / "token.json",
        database_file=state_dir / "sync_state.sqlite3",
        log_file=state_dir / "electridrive.jsonl",
        settings_file=config_dir / "settings.json",
        vfs_cache_dir=vfs_cache_dir,
    )


def selected_scopes() -> list[str]:
    """Return OAuth scopes.

    The product default is ``drive.file`` (non-sensitive): the app sees only files it
    creates and files/folders the user explicitly grants via the Google Picker. This
    needs **no CASA security assessment**, so the app can be distributed globally with a
    single built-in OAuth client.

    Power users / self-hosters who supply their own OAuth client can opt into full
    ``drive`` access (a *restricted* scope, requires Google verification + CASA before
    public release) with::

        ELECTRIDRIVE_SCOPE=drive
    """
    env = os.environ.get("ELECTRIDRIVE_SCOPE")
    raw = (env if env is not None else _saved_scope()).strip().lower()
    if raw not in {"full", "drive"}:
        return [DEFAULT_SCOPE]
    if env is not None:
        return [FULL_DRIVE_SCOPE]  # explicit env override = developer/power escape hatch
    # Full Drive from settings is gated: needs a valid Pro license OR a user-supplied
    # OAuth client (power user / self-host). Otherwise fall back to safe per-file access.
    if get_paths().credentials_file.exists() or _has_valid_license():
        return [FULL_DRIVE_SCOPE]
    return [DEFAULT_SCOPE]


def _saved_scope() -> str:
    try:
        return load_settings().scope or "file"
    except Exception:
        return "file"


def _has_valid_license() -> bool:
    try:
        from electridrive import licensing
        return licensing.is_valid(load_settings().license_key)
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Persisted user settings (config_dir/settings.json)
# --------------------------------------------------------------------------- #

@dataclass
class SyncPair:
    """A configured local<->remote two-way sync relationship."""

    local_path: str
    remote_folder: str
    direction: str = "two_way"  # two_way | up_only | down_only
    enabled: bool = True
    delete_policy: str = "trash"  # off | trash (never permanent in auto-sync)


@dataclass
class Settings:
    theme: str = "electric_dark"  # electric_dark | electric_light
    scope: str = "file"           # access mode: "file" (drive.file) | "drive" (Pro/full)
    license_key: str = ""         # ElectriDrive Pro license (Ed25519-signed)
    follow_system_theme: bool = False
    default_remote_folder: str = "ElectriDrive"
    sync_pairs: list[SyncPair] = field(default_factory=list)
    mountpoint: str = str(Path.home() / "ElectriDrive")
    vfs_writable: bool = False  # writing through the FUSE mount is experimental
    cache_limit_mb: int = 4096
    last_remote_folder_id: str = "root"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        pairs = [SyncPair(**p) for p in data.get("sync_pairs", []) if isinstance(p, dict)]
        known = {f for f in cls().__dict__ if f != "sync_pairs"}
        kwargs = {k: v for k, v in data.items() if k in known}
        return cls(sync_pairs=pairs, **kwargs)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data


def load_settings() -> Settings:
    path = get_paths().settings_file
    if not path.exists():
        return Settings()
    try:
        return Settings.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:  # corrupt settings should never crash the app
        LOGGER.warning("Could not read settings, using defaults: %s", exc)
        return Settings()


def save_settings(settings: Settings) -> None:
    path = get_paths().settings_file
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
