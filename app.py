import sys


def main() -> int:
    if sys.argv[1:] == ["--doctor"]:
        from electridrive.cli import main as cli_main

        return cli_main(["doctor"])
    from electridrive.ui.app import run_app

    return run_app()


if __name__ == "__main__":
    raise SystemExit(main())
