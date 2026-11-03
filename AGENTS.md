# Repository Guidelines

## Project Structure & Module Organization

ElectriDrive is a Python 3.10+ Google Drive client for Linux.

- `electridrive/`: PySide6 UI in `ui/`, Drive integration in `google_api/`, synchronization in `sync/`, transfers in `transfers/`, SQLite storage in `storage/`, and FUSE mounting in `vfs/`.
- `tests/`: pytest suites and the shared in-memory `FakeDrive` in `tests/fakes.py`.
- `assets/`: icons and screenshots. `scripts/` and `packaging/` contain development helpers and distribution configuration; `systemd/` contains sync service/timer units.
- `server/`: optional Flask license-delivery service; see `server/README.md`.

## Build, Test, and Development Commands

Run commands from the repository root:

- `bash scripts/install_dev.sh`: create `.venv`, install dependencies, and run tests.
- `source .venv/bin/activate`: activate the environment before Python commands.
- `python app.py`: launch the desktop UI; `bash scripts/run_ui.sh` activates the environment automatically.
- `python -m electridrive.cli doctor`: inspect configuration and FUSE availability.
- `python -m pytest -q`: run the full test suite.
- `bash scripts/build_release.sh`: build AppImage and Debian packages plus checksums in `dist/`; recreates `build/` and `dist/`.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` for modules/functions/variables, `PascalCase` for classes, and `UPPER_SNAKE_CASE` for constants. Follow surrounding quote and import styles; add type hints to new interfaces. No formatter or linter is configured.

Keep core logic independent of Qt. Route Drive operations through `DriveClientProtocol` and preserve lazy imports of heavy dependencies so core tests remain headless.

## Testing Guidelines

Use pytest, `tests/test_<module>.py`, and descriptive `test_<behavior>` functions. Reuse `FakeDrive`, `tmp_path`, and `monkeypatch` for isolated tests. Run focused checks with `python -m pytest tests/test_twoway.py -q`. Cover changed behavior, including conflicts, exclusions, and recoverable deletions. No coverage threshold is configured.

## Commit & Pull Request Guidelines

This source snapshot has no Git history or PR template, so existing conventions cannot be verified. Use focused commits with imperative subjects, such as `Fix conflict copy naming`. Include the problem, changes, test commands/results, linked issues when applicable, and screenshots for UI changes in PRs.

## Security & Configuration

Keep OAuth credentials, tokens, signing keys, and generated client JSON out of commits. Use environment variables or `~/.config/electridrive/` for local configuration. Preserve `drive.file` as the default scope, recoverable trash operations, conflict copies, and read-only mounts by default.
