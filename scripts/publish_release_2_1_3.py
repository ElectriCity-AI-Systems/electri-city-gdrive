#!/usr/bin/env python3
"""Publish only the owner-approved v2.1.3 after its exact commit passes CI."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPOSITORY = "ElectriCity-AI-Systems/electri-city-gdrive"
VERSION = "2.1.3"
TAG = "v" + VERSION
SEED_SHA256 = "24308dce5f23bd74a50bc50f3d3abbed8439c39a7f65fdd1d938c0b6636bd87e"
CLIENT_SHA256 = "180f96b5506b90aee5ca4fc8d852d394248f2a325b0a4e18e923d590942ea283"
CLIENT_PATH = "opt/electridrive/_internal/electridrive/google_api/client_baked.json"
REQUIRED_JOBS = {f"test ({v})" for v in ("3.10", "3.11", "3.12", "3.13", "3.14")} | {
    "debian-package (ubuntu-22.04)", "debian-package (ubuntu-24.04)"
}


def command(*args: str, **kwargs) -> str:
    result = subprocess.run(args, text=True, capture_output=True, **kwargs)
    if result.returncode:
        raise RuntimeError(f"Command failed: {args!r}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def api(path: str):
    return json.loads(command("gh", "api", f"repos/{REPOSITORY}/{path}", "--method", "GET"))


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def download(url: str, path: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "ElectriDrive-release-verification"})
    with urllib.request.urlopen(request, timeout=90) as source, path.open("wb") as target:
        shutil.copyfileobj(source, target)


def payload(package: Path) -> dict:
    result = {}
    with subprocess.Popen(["dpkg-deb", "--fsys-tarfile", str(package)], stdout=subprocess.PIPE) as process:
        assert process.stdout is not None
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for member in archive:
                name = member.name.removeprefix("./").rstrip("/")
                if not name:
                    continue
                checksum = None
                if member.isfile():
                    stream = archive.extractfile(member)
                    assert stream is not None
                    checksum = hashlib.sha256()
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        checksum.update(block)
                    checksum = checksum.hexdigest()
                result[name] = (member.type.decode(), member.mode, member.linkname, checksum)
        if process.wait() != 0:
            raise RuntimeError("Could not inspect the package payload")
    return result


def validate_assets(release: dict, expected: dict, commit: str, draft: bool) -> None:
    assert release["tag_name"] == TAG
    assert release["target_commitish"] == commit
    assert release["draft"] is draft and release["prerelease"] is False
    assets = {asset["name"]: asset for asset in release["assets"]}
    assert set(assets) == set(expected), "Unexpected release assets"
    for name, checksum in expected.items():
        assert assets[name]["state"] == "uploaded"
        assert assets[name]["digest"] == "sha256:" + checksum, "Uploaded asset digest mismatch"


def main() -> None:
    assert os.environ.get("GITHUB_REPOSITORY") == REPOSITORY
    assert os.environ.get("GITHUB_REF") == "refs/heads/main"
    commit = os.environ["GITHUB_SHA"]
    assert command("git", "rev-parse", "HEAD").strip() == commit
    assert tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"] == VERSION
    from electridrive import __version__
    from electridrive.config import APP_VERSION
    assert __version__ == APP_VERSION == VERSION
    assert not any(r["tag_name"] == TAG for r in api("releases?per_page=100")), "Release already exists"
    assert not any(t["name"] == TAG for t in api("tags?per_page=100")), "Tag already exists"
    print(f"Waiting for all seven CI jobs on {commit}", flush=True)
    for _ in range(160):
        runs = api(f"actions/runs?head_sha={commit}&event=push&per_page=100")["workflow_runs"]
        candidates = [run for run in runs if run["name"] == "CI" and run["head_sha"] == commit]
        if candidates:
            run = max(candidates, key=lambda item: item["id"])
            if run["status"] == "completed":
                assert run["conclusion"] == "success", "CI failed; release will not be published"
                jobs = api(f"actions/runs/{run['id']}/jobs?per_page=100")["jobs"]
                observed = {job["name"]: job for job in jobs}
                assert REQUIRED_JOBS <= observed.keys()
                assert all(observed[name]["conclusion"] == "success" for name in REQUIRED_JOBS)
                break
        time.sleep(15)
    else:
        raise RuntimeError("CI did not complete before the release deadline")
    print(f"CI run {run['id']} passed all required jobs", flush=True)
    output = Path("dist/release-2.1.3").resolve()
    output.mkdir(parents=True, exist_ok=True)
    os.environ["SOURCE_DATE_EPOCH"] = command("git", "log", "-1", "--format=%ct").strip()
    with tempfile.TemporaryDirectory(prefix="electridrive-release-") as directory:
        work = Path(directory)
        artifact = f"electridrive-debian-ubuntu-24.04-{commit}"
        command("gh", "run", "download", str(run["id"]), "--name", artifact, "--dir", str(work / "ci"), "--repo", REPOSITORY)
        original = work / "ci" / f"electridrive_{VERSION}_amd64.deb"
        assert original.is_file()
        seed = work / "public-client-seed.deb"
        download(f"https://github.com/{REPOSITORY}/releases/download/v2.1.2/electridrive_2.1.2_amd64.deb", seed)
        assert digest(seed) == SEED_SHA256, "Public seed package digest mismatch"
        command("dpkg-deb", "--extract", str(seed), str(work / "seed"))
        client = work / "seed" / CLIENT_PATH
        assert digest(client) == CLIENT_SHA256
        root = work / "package"
        command("dpkg-deb", "--raw-extract", str(original), str(root))
        destination = root / CLIENT_PATH
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(client, destination)
        destination.chmod(0o644)
        package = output / original.name
        command("dpkg-deb", "--build", "--root-owner-group", str(root), str(package))
        before, after = payload(original), payload(package)
        assert all(after.get(name) == entry for name, entry in before.items()), "CI payload was altered"
        additions = set(after) - set(before)
        allowed = {CLIENT_PATH, str(Path(CLIENT_PATH).parent), str(Path(CLIENT_PATH).parent.parent)}
        assert CLIENT_PATH in additions and additions <= allowed, "Unexpected added payload"
        command("python3", "scripts/verify_deb.py", str(package), "--expected-version", VERSION, "--max-glibc", "2.35", "--require-baked-client")
        command("python3", "scripts/check_desktop_runtime.py", str(root / "opt/electridrive/_internal"))
        profile = work / "profile"
        runtime = profile / "runtime"
        runtime.mkdir(parents=True, mode=0o700)
        environment = dict(os.environ, XDG_CONFIG_HOME=str(profile / "config"),
                           XDG_STATE_HOME=str(profile / "state"), XDG_CACHE_HOME=str(profile / "cache"),
                           XDG_RUNTIME_DIR=str(runtime), QT_QPA_PLATFORM="offscreen",
                           PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring")
        binary = root / "opt/electridrive/electridrive"
        doctor = command(str(binary), "--doctor", env=environment)
        assert "Python runtime:   3.12.15" in doctor.splitlines()
        assert "FUSE available:   yes" in doctor.splitlines()
        smoke = subprocess.run(["timeout", "5", str(binary)], env=environment, capture_output=True, text=True)
        assert smoke.returncode == 124, "Packaged GUI did not stay running"
        assert not any(message in smoke.stderr for message in ("undefined symbol:", "Failed to load module:", "Traceback (most recent call last)"))
        print(doctor, flush=True)
        checksums = output / "SHA256SUMS.txt"
        checksums.write_text(f"{digest(package)}  {package.name}\n")
        expected = {package.name: digest(package), checksums.name: digest(checksums)}
        record = dict(commit=commit, ci_run_id=run["id"], artifact=artifact,
                      original_ci_sha256=digest(original), release_assets=expected,
                      added_payload=sorted(additions), original_payload_preserved=True,
                      python_runtime="3.12.15", max_glibc="2.35", app_client_unchanged=True)
        (output / "verification.json").write_text(json.dumps(record, indent=2) + "\n")
    notes = output / "release-notes.md"
    notes.write_text(Path("packaging/release-2.1.3.md").read_text()
                     + f"\nCI: https://github.com/{REPOSITORY}/actions/runs/{run['id']}\n"
                     + "\nSHA256:\n\n" + chr(96) * 3 + "text\n" + checksums.read_text() + chr(96) * 3 + "\n"
                     + f"\nSource commit: {commit}.\n")
    command("gh", "release", "create", TAG, str(package), str(checksums), "--repo", REPOSITORY,
            "--draft", "--target", commit, "--title", "ElectriDrive 2.1.3 — safer transfers and Ubuntu desktop fixes",
            "--notes-file", str(notes))
    release = next(r for r in api("releases?per_page=100") if r["tag_name"] == TAG)
    validate_assets(release, expected, commit, True)
    tags = [tag for tag in api("tags?per_page=100") if tag["name"] == TAG]
    assert not tags or tags[0]["commit"]["sha"] == commit
    command("gh", "release", "edit", TAG, "--repo", REPOSITORY, "--draft=false", "--latest", "--target", commit)
    published = api(f"releases/{release['id']}")
    validate_assets(published, expected, commit, False)
    assert api("releases/latest")["id"] == published["id"]
    tags = [tag for tag in api("tags?per_page=100") if tag["name"] == TAG]
    assert len(tags) == 1 and tags[0]["commit"]["sha"] == commit
    with tempfile.TemporaryDirectory(prefix="electridrive-public-download-") as directory:
        for asset in published["assets"]:
            target = Path(directory) / asset["name"]
            download(asset["browser_download_url"], target)
            assert digest(target) == expected[asset["name"]], "Anonymous public download digest mismatch"
    print(f"Published and verified: {published['html_url']}", flush=True)


if __name__ == "__main__":
    main()
