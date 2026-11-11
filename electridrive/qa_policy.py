"""Persistent opt-in QA capabilities. Normal mode has no additional restrictions."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import stat
import sys
from uuid import uuid4


_QA_SAFETY_LOGGER = logging.getLogger("electridrive.qa.safety")
_SAFETY_DIAGNOSTICS = {
    "an absolute path without traversal is required":
        ("filesystem", "absolute-path-required", "A path must be absolute and contain no traversal components."),
    "symlink components are forbidden":
        ("filesystem", "symlink-component", "A protected path contains a symbolic link."),
    "a registered directory is unavailable":
        ("filesystem", "registered-directory-identity", "A registered directory is unavailable or has the wrong type."),
    "unsafe profile file ownership, links or permissions":
        ("profile", "profile-file-integrity", "A protected profile file has unsafe ownership, links, type, or permissions."),
    "required policy file unavailable":
        ("profile", "policy-file-available", "A required QA policy file could not be safely read."),
    "invalid policy":
        ("profile", "policy-format", "The QA policy is malformed or inconsistent."),
    "invalid cloud capability list":
        ("cloud", "cloud-capability-list-format", "The registered QA cloud capability list is malformed."),
    "invalid cloud capability":
        ("cloud", "cloud-capability-format", "A registered QA cloud capability is malformed."),
    "invalid cloud capabilities":
        ("cloud", "cloud-capabilities-format", "The registered QA cloud capabilities are malformed."),
    "registered profile or data directory changed":
        ("profile", "profile-directory-identity", "The registered profile or local data directory changed."),
    "data and profile directories must be disjoint":
        ("profile", "profile-data-separation", "The QA profile and synthetic data directory overlap."),
    "policy changed while QA mode was active":
        ("profile", "policy-immutability", "The active QA policy changed after validation."),
    "invalid profile marker":
        ("profile", "profile-marker-format", "An isolated profile marker is malformed."),
    "missing or changed QA profile marker":
        ("profile", "profile-marker-integrity", "An isolated profile marker is missing or changed."),
    "cloud capabilities changed while QA mode was active":
        ("cloud", "cloud-capability-immutability", "The registered cloud capability list changed while QA mode was active."),
    "cloud root ID is not explicitly registered":
        ("cloud", "root-explicit-registration", "The cloud root ID is not registered for this QA profile."),
    "new synthetic cloud root could not be validated":
        ("cloud", "new-root-validation", "A newly created synthetic cloud root did not match its required metadata."),
    "local path is outside the synthetic QA data directory":
        ("filesystem", "local-path-boundary", "The requested local path is outside the registered synthetic data directory."),
    "unsupported local entry":
        ("filesystem", "local-entry-type", "The local QA entry is not a supported regular file or directory."),
    "hardlinked sources are forbidden":
        ("filesystem", "hardlink-refusal", "A protected source file has multiple hard links."),
    "local QA path is unavailable":
        ("filesystem", "local-path-available", "A required local QA path is unavailable."),
    "data directory identity changed":
        ("filesystem", "data-directory-identity", "The registered data directory changed during file access."),
    "source is not an independent regular QA file":
        ("filesystem", "source-file-integrity", "The upload source is not an independent regular QA file."),
    "download parent directory changed":
        ("filesystem", "download-parent-identity", "The download destination directory changed during transfer."),
    "dedicated, disjoint profile and data directories are required":
        ("profile", "profile-data-initialization", "QA requires dedicated, disjoint profile and data directories."),
    "policy already exists or data directory is not empty":
        ("profile", "profile-initialization-state", "QA initialization requires an unused policy and empty synthetic data directory."),
    "QA profile must be private and owned by the current user":
        ("profile", "profile-permissions", "The QA profile must be private and owned by the current user."),
    "invalid existing isolated settings":
        ("profile", "initial-settings-format", "Existing isolated settings are malformed."),
    "only an empty isolated file-scope profile can be sealed":
        ("profile", "profile-sealing-preconditions", "Only an empty isolated file-scope profile can be sealed."),
    "QA profile cannot be changed in a running process":
        ("profile", "active-profile-immutability", "The QA profile cannot change during a running process."),
    "invalid discovery marker":
        ("profile", "profile-discovery-marker", "A QA profile discovery marker is malformed."),
    "conflicting QA profile markers":
        ("profile", "profile-marker-consistency", "QA profile discovery markers disagree."),
    "malformed QA sync pair":
        ("sync", "sync-pair-format", "A configured QA sync pair is malformed."),
    "pair needs a registered QA cloud ID and deletion policy off":
        ("sync", "sync-pair-scope", "A QA sync pair must use a registered root and disable deletion."),
    "QA mode requires isolated drive.file scope":
        ("auth", "qa-oauth-scope", "QA mode requires the isolated per-file OAuth scope."),
    "malformed isolated settings":
        ("profile", "settings-format", "The isolated settings file is malformed."),
    "ordinary log handler cannot be inherited by QA mode":
        ("logging", "qa-log-isolation", "An ordinary log handler cannot be used in QA mode."),
    "unsafe QA log file":
        ("logging", "qa-log-file-integrity", "The QA log file has unsafe ownership, links, or type."),
    "--qa-profile requires a directory":
        ("profile", "qa-profile-argument", "The QA profile option requires a directory."),
    "conflicting QA profile options":
        ("profile", "qa-profile-argument-consistency", "Conflicting QA profile options were supplied."),
    "conflicting inherited QA profile":
        ("profile", "qa-profile-environment-consistency", "The requested and inherited QA profiles differ."),
    "operation is outside registered synthetic cloud trees":
        ("cloud", "registered-cloud-tree-boundary", "The operation is outside the registered synthetic cloud trees."),
    "cloud-client-profile-mismatch":
        ("cloud", "cloud-client-profile-mismatch", "The Drive client is not bound to the selected QA profile."),
    "active-qa-profile-mismatch":
        ("cloud", "active-qa-profile-mismatch", "The active QA profile does not match the registered cloud facade."),
    "raw-cloud-service-disabled":
        ("cloud", "raw-cloud-service-disabled", "Direct access to the raw Drive service is disabled in QA mode."),
    "cloud-search-disabled":
        ("cloud", "cloud-search-disabled", "Cloud-wide search is disabled in QA mode."),
    "cloud-list-all-disabled":
        ("cloud", "cloud-list-all-disabled", "Cloud-wide file listing is disabled in QA mode."),
    "cloud-starred-list-disabled":
        ("cloud", "cloud-starred-list-disabled", "Listing starred cloud objects is disabled in QA mode."),
    "cloud-trash-list-disabled":
        ("cloud", "cloud-trash-list-disabled", "Listing cloud trash is disabled in QA mode."),
    "cloud-trash-disabled":
        ("cloud", "cloud-trash-disabled", "Moving cloud objects to trash is disabled in QA mode."),
    "cloud-untrash-disabled":
        ("cloud", "cloud-untrash-disabled", "Restoring cloud objects from trash is disabled in QA mode."),
    "cloud-metadata-id-mismatch":
        ("cloud", "cloud-metadata-id-mismatch", "The returned cloud metadata ID does not match the requested ID."),
    "cloud-object-trashed":
        ("cloud", "cloud-object-trashed", "A registered cloud object is marked as trashed."),
    "cloud-root-not-folder":
        ("cloud", "cloud-root-not-folder", "The registered cloud root is not a folder."),
    "cloud-root-name-mismatch":
        ("cloud", "cloud-root-name-mismatch", "The registered cloud root name changed."),
    "cloud-root-parent-mismatch":
        ("cloud", "cloud-root-parent-mismatch", "The registered cloud root parent changed."),
    "cloud-root-nonce-mismatch":
        ("cloud", "cloud-root-nonce-mismatch", "The registered cloud root QA nonce does not match the isolated profile."),
    "cloud-parent-mismatch":
        ("cloud", "cloud-parent-mismatch", "A cloud object is not in its registered parent folder."),
    "cloud-parent-cycle":
        ("cloud", "cloud-parent-cycle", "The registered cloud parent chain contains a cycle."),
    "cloud-parent-not-folder":
        ("cloud", "cloud-parent-not-folder", "The registered cloud parent is not a folder."),
    "cloud-id-not-registered":
        ("cloud", "cloud-id-not-registered", "The cloud object ID is not registered for this QA profile."),
    "cloud-path-not-registered":
        ("cloud", "cloud-path-not-registered", "The cloud path does not identify a registered QA root."),
    "cloud-root-pagination-disabled":
        ("cloud", "cloud-root-pagination-disabled", "Pagination tokens are not accepted when listing registered QA roots."),
    "cloud-page-token-repeated":
        ("cloud", "cloud-page-token-repeated", "The cloud listing returned a repeated pagination token."),
    "cloud-object-name-invalid":
        ("cloud", "cloud-object-name-invalid", "A cloud object name is not a valid single path component."),
    "cloud-update-target-unsupported":
        ("cloud", "cloud-update-target-unsupported", "The cloud update target is not a supported regular file."),
    "cloud-update-id-changed":
        ("cloud", "cloud-update-id-changed", "The cloud update returned an unexpected object ID."),
    "unsafe database lock":
        ("database", "qa-database-lock-integrity", "The QA database lock has unsafe ownership, links, type, or permissions."),
    "database lock replaced":
        ("database", "qa-database-lock-identity", "The QA database lock changed after it was opened."),
    "database already in use":
        ("database", "qa-database-exclusive-owner", "Another process already owns the QA database lock."),
    "ordinary database cannot be reused":
        ("database", "qa-database-isolation", "An ordinary database cannot be reused in QA mode."),
    "database profile changed":
        ("database", "qa-database-profile", "The active database profile changed."),
    "database directory or lock changed":
        ("database", "qa-database-path-identity", "The QA database directory or lock identity changed."),
    "unexpected database snapshot":
        ("database", "qa-database-snapshot-state", "An unexpected QA database snapshot appeared."),
    "database snapshot changed":
        ("database", "qa-database-snapshot-integrity", "The QA database snapshot changed after validation."),
    "malformed database snapshot":
        ("database", "qa-database-snapshot-format", "The QA database snapshot has an invalid format or schema."),
    "FUSE is disabled":
        ("runtime", "qa-fuse-disabled", "FUSE access is disabled in QA mode."),
    "filesystem transfer journals are disabled":
        ("transfers", "qa-transfer-journal-disabled", "Filesystem transfer journals are disabled in QA mode."),
    "pre-existing ordinary Google client forbidden":
        ("auth", "qa-client-provenance", "An ordinary Google client cannot be reused in QA mode."),
    "injected Google credentials/service forbidden":
        ("auth", "qa-client-injection", "Injected Google credentials or service objects are not allowed in QA mode."),
    "select --qa-profile first":
        ("profile", "qa-profile-required", "An explicit QA profile must be selected first."),
    "only per-file access is supported":
        ("auth", "qa-scope-change", "QA mode does not permit changing the isolated per-file OAuth scope."),
    "Drive Picker is disabled; use registered QA roots":
        ("cloud", "qa-picker-disabled", "Drive Picker is disabled; use an explicitly registered QA root."),
    "log must belong to the isolated profile":
        ("logging", "qa-log-destination", "The log destination must belong to the isolated QA profile."),
    "deletion is forbidden":
        ("sync", "qa-deletion-disabled", "Deletion operations are forbidden in QA mode."),
}
_UNKNOWN_DIAGNOSTIC = (
    "internal", "unclassified-qa-safety-guard",
    "An unclassified QA safety guard rejected the operation.",
)
_SAFETY_DIAGNOSTICS_BY_RULE = {
    rule: (category, rule, reason)
    for category, rule, reason in _SAFETY_DIAGNOSTICS.values()
}


class QaSafetyError(PermissionError):
    """A fail-closed QA stop with diagnostics restricted to safe static metadata."""

    def __init__(self, reason: str = "", *, rule: str | None = None):
        prefix = "QA safety stop: "
        normalized = reason.removeprefix(prefix).removeprefix("QA safety: ")
        diagnostic = (
            _SAFETY_DIAGNOSTICS_BY_RULE.get(rule, _UNKNOWN_DIAGNOSTIC)
            if rule is not None
            else _SAFETY_DIAGNOSTICS.get(normalized, _UNKNOWN_DIAGNOSTIC)
        )
        category, rule, safe_reason = diagnostic
        self.category = category
        self.rule = rule
        self.reason = safe_reason
        super().__init__(f"QA safety stop [{category}/{rule}]: {safe_reason}")
        _QA_SAFETY_LOGGER.warning(
            "QA safety stop category=%s rule=%s reason=%s",
            category,
            rule,
            safe_reason,
            extra={
                "qa_safety_category": category,
                "qa_safety_rule": rule,
                "qa_safety_reason": safe_reason,
            },
        )


def log_exception(logger, message, *args, level=logging.ERROR):
    """Avoid adding contextual data when a structured QA stop is already logged."""
    if isinstance(sys.exc_info()[1], QaSafetyError):
        return
    logger.log(level, message, *args, exc_info=True)


POLICY = ".electridrive-qa.json"
REMOTES = ".electridrive-qa-remotes.json"
MARKER = "qa-mode.json"
_active = None


def _stop(reason):
    raise QaSafetyError("QA safety stop: " + reason)


def _absolute(value):
    raw = str(value)
    path = Path(raw)
    if not path.is_absolute() or any(p in {".", ".."} for p in raw.split("/")):
        _stop("an absolute path without traversal is required")
    return path


def _no_links(path):
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            _stop("symlink components are forbidden")


def _identity(path):
    _no_links(path)
    s = path.stat()
    if not stat.S_ISDIR(s.st_mode):
        _stop("a registered directory is unavailable")
    return [s.st_dev, s.st_ino]


def check_profile_file(path, *, strict=False):
    """Check metadata before opening a QA policy/settings/credential file."""
    _no_links(path)
    if path.exists():
        s = path.lstat()
        if (not stat.S_ISREG(s.st_mode) or s.st_nlink != 1
                or s.st_uid != os.getuid() or (strict and s.st_mode & 0o077)):
            _stop("unsafe profile file ownership, links or permissions")


def _open_directory(path):
    """Walk from / using directory descriptors; never follow a swapped ancestor."""
    path = _absolute(path)
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(path, *, strict=True):
    check_profile_file(path, strict=strict)
    try:
        directory = _open_directory(path.parent)
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as stream:
                s = os.fstat(stream.fileno())
                if (not stat.S_ISREG(s.st_mode) or s.st_nlink != 1
                        or s.st_uid != os.getuid() or (strict and s.st_mode & 0o077)):
                    _stop("unsafe profile file ownership, links or permissions")
                return stream.read()
        finally:
            os.close(directory)
    except OSError:
        raise QaSafetyError("QA safety stop: required policy file unavailable") from None


def _write(path, value):
    _no_links(path.parent)
    data = json.dumps(value, indent=2).encode()
    directory = _open_directory(path.parent)
    name = ".qa-write-" + uuid4().hex
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path.name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        try:
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass  # already moved into place by the os.replace() above
        os.close(directory)
    return data


class QaPolicy:
    def __init__(self, profile):
        self.profile = _absolute(profile)
        raw = _read(self.profile / POLICY)
        try:
            data = json.loads(raw)
            self.local_root = _absolute(data["local_root"])
            self.nonce = data["nonce"]
            self._profile_identity = data["profile_identity"]
            self._local_identity = data["local_identity"]
            if (data["version"] != 1 or data["profile"] != str(self.profile)
                    or not isinstance(self.nonce, str) or len(self.nonce) != 64):
                _stop("invalid policy")
        except (ValueError, KeyError, TypeError):
            raise QaSafetyError("QA safety stop: invalid policy") from None
        self._digest = hashlib.sha256(raw).hexdigest()
        remote_raw = _read(self.profile / REMOTES)
        self._remote_digest = hashlib.sha256(remote_raw).hexdigest()
        try:
            self._roots = json.loads(remote_raw)["roots"]
            if not isinstance(self._roots, dict):
                _stop("invalid cloud capability list")
            for rid, spec in self._roots.items():
                if (not isinstance(rid, str) or not rid or rid == "root" or "/" in rid
                        or not isinstance(spec["name"], str)
                        or not isinstance(spec["parents"], list) or len(spec["parents"]) != 1
                        or not isinstance(spec["parents"][0], str) or not spec["parents"][0]):
                    _stop("invalid cloud capability")
        except (ValueError, KeyError, TypeError):
            raise QaSafetyError("QA safety stop: invalid cloud capabilities") from None
        self.verify()

    def verify(self):
        if (_identity(self.profile) != self._profile_identity
                or _identity(self.local_root) != self._local_identity
                or self.profile.stat().st_uid != os.getuid()
                or self.profile.stat().st_mode & 0o077):
            _stop("registered profile or data directory changed")
        if self.profile.is_relative_to(self.local_root) or self.local_root.is_relative_to(self.profile):
            _stop("data and profile directories must be disjoint")
        if hashlib.sha256(_read(self.profile / POLICY)).hexdigest() != self._digest:
            _stop("policy changed while QA mode was active")
        expected = {"profile": str(self.profile), "policy_sha256": self._digest}
        for area in ("config", "state"):
            try:
                marker = json.loads(_read(self.profile / area / "electridrive" / MARKER))
            except ValueError:
                raise QaSafetyError("QA safety stop: invalid profile marker") from None
            if marker != expected:
                _stop("missing or changed QA profile marker")
        if hashlib.sha256(_read(self.profile / REMOTES)).hexdigest() != self._remote_digest:
            _stop("cloud capabilities changed while QA mode was active")

    @property
    def remote_ids(self):
        self.verify()
        return tuple(self._roots)

    def root_spec(self, rid):
        self.verify()
        if rid not in self._roots:
            _stop("cloud root ID is not explicitly registered")
        return self._roots[rid].copy()

    def register_new_root(self, remote, expected_name):
        self.verify()
        if (not remote.is_folder or remote.trashed or remote.name != expected_name
                or len(remote.parents) != 1
                or remote.app_properties.get("electridrive_qa_nonce") != self.nonce):
            _stop("new synthetic cloud root could not be validated")
        self._roots[remote.id] = {"name": expected_name, "parents": list(remote.parents)}
        raw = _write(self.profile / REMOTES, {"roots": self._roots})
        self._remote_digest = hashlib.sha256(raw).hexdigest()
        return "qa:" + remote.id

    def local(self, value, *, exists=True):
        self.verify()
        path = _absolute(value)
        if not path.is_relative_to(self.local_root):
            _stop("local path is outside the synthetic QA data directory")
        _no_links(path)
        if path.exists():
            s = path.lstat()
            if not (stat.S_ISREG(s.st_mode) or stat.S_ISDIR(s.st_mode)):
                _stop("unsupported local entry")
            if stat.S_ISREG(s.st_mode) and s.st_nlink != 1:
                _stop("hardlinked sources are forbidden")
        elif exists:
            _stop("local QA path is unavailable")
        return path

    @contextmanager
    def directory_fd(self, path, *, create=False):
        path = self.local(path, exists=not create)
        fd = _open_directory(self.local_root)
        try:
            s = os.fstat(fd)
            if [s.st_dev, s.st_ino] != self._local_identity:
                _stop("data directory identity changed")
            for part in path.relative_to(self.local_root).parts:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass  # idempotent: the directory only needs to exist
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def source(self, path):
        path = self.local(path)
        with self.directory_fd(path.parent) as parent:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                s = os.fstat(stream.fileno())
                if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
                    _stop("source is not an independent regular QA file")
                yield stream

    @contextmanager
    def atomic_writer(self, path):
        path = self.local(path, exists=False)
        with self.directory_fd(path.parent, create=True) as parent:
            temporary = ".qa-transfer-" + uuid4().hex
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
                with os.fdopen(fd, "wb") as stream:
                    yield stream
                    stream.flush()
                    os.fsync(stream.fileno())
                self.local(path, exists=False)
                # The held directory may have been renamed outside the data root
                # and replaced by a different real directory (without symlinks).
                current = _open_directory(path.parent)
                try:
                    held, now = os.fstat(parent), os.fstat(current)
                    if (held.st_dev, held.st_ino) != (now.st_dev, now.st_ino):
                        _stop("download parent directory changed")
                finally:
                    os.close(current)
                os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass  # already linked into place by the os.replace() above


def initialize(profile, local_root):
    profile, local_root = _absolute(profile), _absolute(local_root)
    _no_links(profile)
    _no_links(local_root)
    if (profile == Path.home() or local_root == Path.home() or profile == Path("/")
            or local_root == Path("/") or profile.is_relative_to(local_root)
            or local_root.is_relative_to(profile)):
        _stop("dedicated, disjoint profile and data directories are required")
    reserved = [profile / POLICY, profile / REMOTES,
                *(profile / area / "electridrive" / MARKER for area in ("config", "state"))]
    if any(p.exists() or p.is_symlink() for p in reserved) or (local_root.exists() and any(local_root.iterdir())):
        _stop("policy already exists or data directory is not empty")
    if profile.exists() and (profile.stat().st_mode & 0o077 or profile.stat().st_uid != os.getuid()):
        _stop("QA profile must be private and owned by the current user")
    settings = profile / "config/electridrive/settings.json"
    if settings.exists():
        try:
            data = json.loads(_read(settings, strict=False))
        except ValueError:
            raise QaSafetyError("QA safety stop: invalid existing isolated settings") from None
        if data.get("sync_pairs") or data.get("scope", "file") != "file":
            _stop("only an empty isolated file-scope profile can be sealed")
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    local_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for area in ("config", "state", "cache"):
        directory = profile / area / "electridrive"
        _no_links(directory)
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    data = {"version": 1, "profile": str(profile), "local_root": str(local_root),
            "profile_identity": _identity(profile), "local_identity": _identity(local_root),
            "nonce": uuid4().hex + uuid4().hex}
    raw = _write(profile / POLICY, data)
    _write(profile / REMOTES, {"roots": {}})
    marker = {"profile": str(profile), "policy_sha256": hashlib.sha256(raw).hexdigest()}
    for area in ("config", "state"):
        _write(profile / area / "electridrive" / MARKER, marker)
    return QaPolicy(profile)


def current_policy():
    global _active
    requested = os.environ.get("ELECTRIDRIVE_QA_PROFILE")
    if _active is not None:
        if requested is not None and _absolute(requested) != _active.profile:
            _stop("QA profile cannot be changed in a running process")
        _active.verify()
        return _active
    profiles = []
    if requested is not None:
        profiles.append(_absolute(requested))
    else:
        for key, default in (("XDG_CONFIG_HOME", Path.home()/".config"),
                             ("XDG_STATE_HOME", Path.home()/".local/state")):
            marker = Path(os.environ.get(key, default)) / "electridrive" / MARKER
            if marker.exists() or marker.is_symlink():
                try:
                    profiles.append(_absolute(json.loads(_read(marker))["profile"]))
                except (KeyError, ValueError, TypeError):
                    raise QaSafetyError("QA safety stop: invalid discovery marker") from None
    if not profiles:
        return None
    if len(set(profiles)) != 1:
        _stop("conflicting QA profile markers")
    _active = QaPolicy(profiles[0])
    return _active


def require_local(path, *, exists=True):
    policy = current_policy()
    return policy.local(path, exists=exists) if policy else Path(path)


def validate_pair(pair):
    policy = current_policy()
    if policy:
        if (not isinstance(pair.local_path, str) or not isinstance(pair.remote_folder, str)
                or not isinstance(pair.direction, str) or not isinstance(pair.enabled, bool)
                or not isinstance(pair.include_all_files, bool)
                or not isinstance(pair.auto_select_newest_duplicate, bool)
                or not isinstance(pair.latest_wins, bool)
                or not isinstance(pair.canonical_remote_ids, dict)
                or any(not isinstance(k, str) or not isinstance(v, str)
                       for k, v in pair.canonical_remote_ids.items())):
            _stop("malformed QA sync pair")
        policy.local(pair.local_path, exists=False)
        if (pair.direction not in {"up_only", "down_only", "two_way"}
                or pair.delete_policy != "off" or pair.remote_folder not in
                {"qa:"+rid for rid in policy.remote_ids}):
            _stop("pair needs a registered QA cloud ID and deletion policy off")


def validate_settings(settings):
    if current_policy():
        if settings.scope != "file":
            _stop("QA mode requires isolated drive.file scope")
        for pair in settings.sync_pairs:
            validate_pair(pair)


def validate_settings_data(data):
    """Do not let permissive normal-mode normalization discard malformed pairs."""
    if current_policy():
        if (not isinstance(data, dict) or not isinstance(data.get("sync_pairs", []), list)
                or any(not isinstance(pair, dict) for pair in data.get("sync_pairs", []))):
            _stop("malformed isolated settings")


def append_log(path, text):
    policy = current_policy()
    policy.verify()
    if Path(path) != policy.profile / "state/electridrive/electridrive.jsonl":
        _stop("ordinary log handler cannot be inherited by QA mode")
    directory = _open_directory(Path(path).parent)
    try:
        fd = os.open(Path(path).name, os.O_APPEND | os.O_WRONLY | os.O_CREAT
                     | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            s = os.fstat(stream.fileno())
            if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or s.st_uid != os.getuid():
                _stop("unsafe QA log file")
            stream.write(text)
    finally:
        os.close(directory)


def check_startup():
    if current_policy():
        from electridrive.config import load_settings
        validate_settings(load_settings())


def bootstrap_profile(argv):
    remaining = []
    index = 0
    selected = None
    while index < len(argv):
        arg = argv[index]
        if arg == "--qa-profile":
            index += 1
            if index >= len(argv):
                _stop("--qa-profile requires a directory")
            candidate = argv[index]
        elif arg.startswith("--qa-profile="):
            candidate = arg.split("=",1)[1]
        else:
            remaining.append(arg)
            index += 1
            continue
        if selected is not None and selected != candidate:
            _stop("conflicting QA profile options")
        selected = candidate
        index += 1
    if selected is not None:
        inherited = os.environ.get("ELECTRIDRIVE_QA_PROFILE")
        if inherited is not None and _absolute(inherited) != _absolute(selected):
            _stop("conflicting inherited QA profile")
        os.environ["ELECTRIDRIVE_QA_PROFILE"] = selected
    current_policy()
    return remaining


@contextmanager
def open_source(path):
    policy = current_policy()
    if policy:
        with policy.source(path) as stream:
            yield stream
    else:
        with Path(path).open("rb") as stream:
            yield stream


def mkdir(path):
    policy = current_policy()
    if policy:
        with policy.directory_fd(path, create=True):
            pass
    else:
        Path(path).mkdir(parents=True, exist_ok=True)


def copy_file(source, dest):
    policy = current_policy()
    if policy:
        with policy.source(source) as src, policy.atomic_writer(dest) as out:
            shutil.copyfileobj(src, out)
    else:
        shutil.copy2(source, dest)


@contextmanager
def scandir(path):
    policy = current_policy()
    if policy:
        with policy.directory_fd(path) as fd, os.scandir(fd) as entries:
            yield entries
    else:
        with os.scandir(path) as entries:
            yield entries


def restrict_client(client):
    policy = current_policy()
    if not policy:
        return client
    from electridrive.qa_drive import QaDriveClient
    if isinstance(client, QaDriveClient):
        client.verify()
        return client
    return QaDriveClient(client, policy)
