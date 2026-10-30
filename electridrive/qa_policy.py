"""Persistent opt-in QA capabilities. Normal mode has no additional restrictions."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
from uuid import uuid4


class QaSafetyError(PermissionError):
    pass


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
    except OSError as exc:
        raise QaSafetyError("QA safety stop: required policy file unavailable") from exc


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
            pass
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
        except (ValueError, KeyError, TypeError) as exc:
            raise QaSafetyError("QA safety stop: invalid policy") from exc
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
        except (ValueError, KeyError, TypeError) as exc:
            raise QaSafetyError("QA safety stop: invalid cloud capabilities") from exc
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
            except ValueError as exc:
                raise QaSafetyError("QA safety stop: invalid profile marker") from exc
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
                        pass
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
                    pass


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
        except ValueError as exc:
            raise QaSafetyError("QA safety stop: invalid existing isolated settings") from exc
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
                except (KeyError, ValueError, TypeError) as exc:
                    raise QaSafetyError("QA safety stop: invalid discovery marker") from exc
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
