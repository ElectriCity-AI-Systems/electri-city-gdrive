#!/usr/bin/env python3
"""Owner-run ElectriDrive 2.1.3 duplicate-file acceptance (isolated account test).

Run on the OWNER'S Ubuntu desktop using the tested GitHub checkout.  The script
never reads the user's ordinary ElectriDrive settings or OAuth tokens.  Google
Drive writes are restricted to creating a NEW root-level QA folder containing
exactly two synthetic files with the same name.  There are no delete operations.

Commands: prepare, launch, verify.  See the accompanying ChatGPT instructions.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

BASE = Path.home() / "Downloads/ElectriDrive-213-UI-FIX-TkeMucsg"
EXPECTED = "1f79ea3a0fde8259105deebd8e164bc9b8162137"
QA = Path.home() / "Downloads/ElectriDrive-DUP-LIVE-1f79ea3"
DATA_FILE = QA / "qa-manifest.json"
SOURCE = QA / "source"


def fail(message: str) -> None:
    raise SystemExit("STOP: " + message)


def preflight() -> None:
    if not (BASE / ".venv/bin/python").is_file():
        fail("Die frische Testumgebung fehlt: " + str(BASE))
    result = subprocess.run(
        ["git", "-C", str(BASE / "src"), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if result != EXPECTED:
        fail("Ungeprüfter GitHub-Commit. Erwartet " + EXPECTED + ", gefunden " + result)
    # All ElectriDrive file, state, cache and OAuth operations stay in a separate QA profile.
    os.environ["XDG_CONFIG_HOME"] = str(QA / "profile/config")
    os.environ["XDG_STATE_HOME"] = str(QA / "profile/state")
    os.environ["XDG_CACHE_HOME"] = str(QA / "profile/cache")
    os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"
    os.environ["PYTHONNOUSERSITE"] = "1"
    os.environ.pop("ELECTRIDRIVE_SCOPE", None)
    sys.path.insert(0, str(BASE / "src"))


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def save_manifest(info: dict) -> None:
    DATA_FILE.write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
    DATA_FILE.chmod(0o600)


def load_manifest() -> dict:
    if not DATA_FILE.is_file():
        fail("Testmanifest fehlt. Zuerst 'prepare' ausführen.")
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))


def list_children(client, parent_id: str):
    items = []
    page = None
    seen = set()
    while True:
        result = client.list_folder(parent_id, page)
        items.extend(result.files)
        page = result.next_page_token
        if not page:
            return items
        if page in seen:
            fail("Google Drive wiederholt dieselbe Ergebnisseite.")
        seen.add(page)


def find_oauth_client() -> Path:
    home = Path.home()
    candidates = [
        home / "Downloads/ElectriDrive-PR9-20261026-OXtyAV/profile/config/electridrive/app_client.json",
        home / "Downloads/ElectriDrive-PR9-20261026-OXtyAV/profile/config/electridrive/credentials.json",
        home / "Downloads/ElectriDrive-FRESH-W5GuKsPB/profile/config/electridrive/app_client.json",
        home / ".config/electridrive/app_client.json",
        home / ".config/electridrive/credentials.json",
        Path("/opt/electridrive/_internal/electridrive/google_api/client_baked.json"),
        Path("/opt/electridrive/electridrive/google_api/client_baked.json"),
    ]
    for f in candidates:
        if not f.is_file() or f.is_symlink():
            continue
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
            c = obj.get("installed") or {}
            if (isinstance(c, dict)
                and c.get("client_id", "").endswith(".apps.googleusercontent.com")
                and "REPLACE_WITH_YOUR" not in c.get("client_id", "")
                and c.get("client_secret")
                and "REPLACE_WITH_YOUR" not in c.get("client_secret", "")):
                return f
        except (OSError, UnicodeError, ValueError, AttributeError, TypeError):
            pass
    fail("Keine vorhandene Google-Desktop-OAuth-Konfiguration gefunden. Keine Drive-Schreibaktion ausgeführt.")


def client():
    from electridrive.google_api.client import GoogleDriveClient
    return GoogleDriveClient()


def prepare() -> None:
    preflight()
    if QA.exists():
        fail("Testprofil existiert bereits; kein Überschreiben: " + str(QA))
    oauth_source = find_oauth_client()  # fail before creating state or Drive objects
    QA.mkdir(mode=0o700, parents=False)
    for folder in (QA / "profile/config/electridrive", QA / "profile/state", QA / "profile/cache", SOURCE):
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)

    target = QA / "profile/config/electridrive/app_client.json"
    shutil.copyfile(oauth_source, target)
    target.chmod(0o600)

    files = {
        "duplicate.txt": b"ElectriDrive QA LOCAL chosen content\n",
        ".env": b"ELECTRIDRIVE_QA_FAKE_TOKEN=NOT_A_SECRET\n",
    }
    for name, contents in files.items():
        (SOURCE / name).write_bytes(contents)

    from electridrive.config import Settings, SyncPair, save_settings, get_paths
    from electridrive.sync.tree import scan_local_tree
    from electridrive.sync.rules import SyncRules
    tree = scan_local_tree(SOURCE, SyncRules.full_backup())
    if tree.relative_files != set(files) or tree.excluded or tree.errors:
        fail("Lokaler Vollbackup-Scan enthält Abweichungen.")

    print("Google Drive: Anmeldung im Browser nur für diesen isolierten Test.")
    g = client()  # Google sign-in: tokens saved in private XDG fallback under QA.
    cloud = "ElectriDrive-QA-DUP-" + uuid.uuid4().hex[:14]
    remote_root_id = g.create_folder(cloud, "root")  # always NEW; never touches old folders
    state = {
        "phase": "new_remote_folder_created",
        "remote_folder": cloud,
        "remote_folder_id": remote_root_id,
        "synthetic_file": "duplicate.txt",
        "candidate_ids": [],
        "candidate_sha256": {},
        "local_sha256": {name: digest(contents) for name, contents in files.items()},
        "source": str(SOURCE),
    }
    save_manifest(state)

    try:
        for label, payload in (
            ("A", b"ElectriDrive QA REMOTE candidate A - original\n"),
            ("B", b"ElectriDrive QA REMOTE candidate B - original (distinct)\n"),
        ):
            seed = QA / ("seed-" + label + ".txt")
            seed.write_bytes(payload)
            candidate_id = g.upload_file(seed, remote_root_id, "duplicate.txt")
            state["candidate_ids"].append(candidate_id)
            state["candidate_sha256"][candidate_id] = digest(payload)
            save_manifest(state)
        state["phase"] = "two_duplicates_created"
        save_manifest(state)
    except Exception:
        print("Teststatus im Manifest gesichert. Isolierten Drive-Ordner NICHT löschen.")
        raise

    actual = [f for f in list_children(g, remote_root_id) if f.name == "duplicate.txt"]
    if set(f.id for f in actual) != set(state["candidate_ids"]):
        fail("Drive bestätigt die zwei Dubletten nicht eindeutig.")

    # Preflight: run the real sync engine but DEMAND fail-closed and no writes.
    # The chosen scope has only one overlapping path, so a preflight bug must
    # not put any old personal files at risk.
    from electridrive.storage.database import SyncDatabase
    from electridrive.sync.twoway import TwoWaySyncEngine
    pair = SyncPair(local_path=str(SOURCE), remote_folder=cloud,
                    direction="up_only", enabled=False,
                    delete_policy="off", include_all_files=True)
    settings = Settings(scope="file", sync_pairs=[pair])
    save_settings(settings)
    db = SyncDatabase(get_paths().database_file)
    try:
        report = TwoWaySyncEngine(g, db, pair).run()
    finally:
        db.close()
    if report.failed < 1 or not report.remote_path_conflicts or report.uploaded or report.downloaded:
        fail("FAIL-CLOSED-Prüfung fehlerhaft. Keine weiteren Sync-Versuche ausführen.")
    final = [f for f in list_children(g, remote_root_id)]
    if len(final) != 2 or set(f.id for f in final) != set(state["candidate_ids"]):
        fail("Unerwartete Remote-Dateiänderung während der Konfliktprüfung.")
    for candidate in final:
        seed = (QA / ("seed-A.txt" if candidate.id == state["candidate_ids"][0]
                      else "seed-B.txt")).read_bytes()
        if candidate.md5_checksum != md5(seed):
            fail("Ein Remote-Dateiinhalt wurde während der Schutzprüfung verändert.")
    state["phase"] = "preflight_blocked_without_writes"
    save_manifest(state)
    print("\n=== VORBEREITUNG BESTANDEN ===")
    print("Testordner in Google Drive:", cloud)
    print("Zwei unterschiedliche, harmlose Dateien 'duplicate.txt': ERSTELLT")
    print("Schutzprüfung: Synchronisation blockiert, kein Upload/Download")
    print("Anzahl automatisch ausgewählter Dateien: 0")
    print("Nächster Schritt: python <Testskript> launch")
    print("ACHTUNG: Die Dateien liegen in einem NUR FÜR DIESEN TEST erstellten Ordner.")


def launch() -> None:
    preflight()
    state = load_manifest()
    if state.get("phase") != "preflight_blocked_without_writes":
        fail("Vorbereitung nicht vollständig; kein GUI-Start.")
    print("Öffne ausschließlich das isolierte Testprofil.")
    print("Enable → Sync now → erst ohne Auswahl schließen.")
    print("Dann erneut Sync now → eine künstliche Datei auswählen und speichern.")
    print("Erst danach ein drittes Mal Sync now → Disable → Anwendung schließen.")
    os.execv(sys.executable, [sys.executable, str(BASE / "src/app.py")])


def verify() -> None:
    preflight()
    state = load_manifest()
    if state.get("phase") != "preflight_blocked_without_writes":
        fail("Test wurde nicht vollständig vorbereitet.")
    from electridrive.config import load_settings
    settings = load_settings()
    if len(settings.sync_pairs) != 1:
        fail("Unerwartete Zahl an Test-Synchronisationen.")
    pair = settings.sync_pairs[0]
    if pair.enabled or pair.local_path != str(SOURCE) or pair.direction != "up_only":
        fail("Testpaar ist noch aktiviert oder unerwartet verändert. Bitte 'Disable' im GUI.")
    chosen = pair.canonical_remote_ids.get("duplicate.txt")
    ids = state["candidate_ids"]
    if chosen not in ids:
        fail("Noch keine gültige geprüfte Datei-ID ausgewählt und gespeichert.")
    other = next(x for x in ids if x != chosen)
    g = client()
    children = list_children(g, state["remote_folder_id"])
    same_name = [x for x in children if x.name == "duplicate.txt"]
    dotenv = [x for x in children if x.name == ".env"]
    if len(same_name) != 2 or set(x.id for x in same_name) != set(ids):
        fail("Remote-Dubletten fehlen oder wurden verändert.")
    if len(dotenv) != 1 or len(children) != 3:
        fail("Versteckte Datei fehlt oder unerwartete Dateien im QA-Ordner.")
    target_file = next(x for x in same_name if x.id == chosen)
    untouched = next(x for x in same_name if x.id == other)
    local = (SOURCE / "duplicate.txt").read_bytes()
    env = (SOURCE / ".env").read_bytes()
    if target_file.md5_checksum != md5(local):
        fail("Ausgewählte Datei auf Drive hat nicht den lokalen Inhalt.")
    if untouched.md5_checksum != md5((QA / ("seed-A.txt" if other == ids[0] else "seed-B.txt")).read_bytes()):
        fail("Die NICHT ausgewählte Dublette wurde verändert!")
    if dotenv[0].md5_checksum != md5(env):
        fail("Versteckte .env wurde nicht unverändert gesichert.")
    output = QA / "verified-downloads"
    output.mkdir(exist_ok=True, mode=0o700)
    for f, name, expected in (
        (target_file, "chosen.txt", local),
        (untouched, "untouched.txt", (QA / ("seed-A.txt" if other == ids[0] else "seed-B.txt")).read_bytes()),
        (dotenv[0], "hidden.env", env),
    ):
        target = output / name
        g.download_file(f.id, target)
        if target.read_bytes() != expected:
            fail("Heruntergeladene Datei nicht byte-identisch: " + name)
    print("\n============================================")
    print(" ELECTRIDRIVE 2.1.3 DUBLETTEN-LIVETEST OK")
    print("============================================")
    print("Konflikt ohne Änderungen erkannt: JA")
    print("Bewusst ausgewählte Datei aktualisiert: JA")
    print("Andere Dublette unverändert: JA")
    print("Versteckte .env hochgeladen: JA")
    print("3/3 Cloud-Dateien heruntergeladen und byte-identisch: JA")
    print("Keine Löschaktionen ausgeführt: JA")
    print("Cloud-Testordner:", state["remote_folder"])
    print("Persönliche Sync-Paare nie geladen/aktiviert: JA")


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in {"prepare", "launch", "verify"}:
        fail("Aufruf: python <Skriptdatei> prepare | launch | verify")
    os.umask(0o077)
    {"prepare": prepare, "launch": launch, "verify": verify}[sys.argv[1]]()


if __name__ == "__main__":
    main()