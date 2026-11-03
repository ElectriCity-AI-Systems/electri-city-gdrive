import sys


def main() -> int:
    argv = sys.argv[1:]
    if argv == ["--doctor"]:
        from electridrive.cli import main as cli_main

        return cli_main(["doctor"])
    # The packaged (PyInstaller) binary has no separate CLI executable, so any
    # other subcommand (list, sync, qa-init, qa-create-folder, ...) must be
    # dispatched here too; only a bare launch (optionally with --qa-profile
    # alone) starts the GUI. --qa-profile is consumed first so a following
    # subcommand reaches electridrive.cli already resolved to one profile.
    from electridrive import qa_policy as qa

    remaining = qa.bootstrap_profile(argv)
    if remaining:
        from electridrive.cli import main as cli_main

        return cli_main(remaining)
    from electridrive.ui.app import run_app

    return run_app()


if __name__ == "__main__":
    raise SystemExit(main())
