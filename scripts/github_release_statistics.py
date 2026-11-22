#!/usr/bin/env python3
"""Read-only GitHub release download snapshot. Counts downloads, not customers."""
import csv
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = "ElectriCity-AI-Systems/electri-city-gdrive"
INSTALLER_SUFFIXES = (".deb", ".appimage")

def fetch_releases(repo=REPO):
    all_releases = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{repo}/releases?per_page=100&page={page}"
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "electridrive-release-stats"}
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=20) as response:
            batch = json.load(response)
        if not isinstance(batch, list):
            raise ValueError("Unexpected GitHub API response")
        all_releases.extend(batch)
        if len(batch) < 100:
            return all_releases
        page += 1

def snapshot(releases, timestamp=None):
    timestamp = timestamp or dt.datetime.now(dt.timezone.utc).isoformat()
    rows = []
    for release in releases:
        if release.get("draft"):
            continue
        for asset in release.get("assets", []):
            name = str(asset.get("name", ""))
            if not name.lower().endswith(INSTALLER_SUFFIXES):
                continue
            rows.append({
                "captured_utc": timestamp,
                "tag": release.get("tag_name", ""),
                "prerelease": bool(release.get("prerelease")),
                "asset": name,
                "downloads_total": int(asset.get("download_count", 0)),
                "asset_id": int(asset["id"]),
            })
    return rows

def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("release-statistics")
    out.mkdir(parents=True, exist_ok=True)
    rows = snapshot(fetch_releases())
    (out / "downloads.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")
    with (out / "downloads.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["captured_utc", "tag", "prerelease", "asset", "downloads_total", "asset_id"])
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({"installer_downloads_total": sum(r["downloads_total"] for r in rows),
                      "installer_assets": len(rows), "snapshot_utc": rows[0]["captured_utc"] if rows else None}))
    print("Downloads are not unique customers or active installations.")

if __name__ == "__main__":
    main()
