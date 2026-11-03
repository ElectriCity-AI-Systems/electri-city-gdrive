"""Local service-user queue runner. Initialization never sends email."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import license_server as server  # noqa: E402
from server.license_recovery import load_service_env  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Retry definite license-delivery failures')
    parser.add_argument('--initialize', action='store_true',
                        help='import the legacy ledger without delivering any receipts')
    parser.add_argument('--env-file', type=Path,
                        help='optional service EnvironmentFile (parsed, never executed)')
    args = parser.parse_args(argv)
    if hasattr(os, 'geteuid') and os.geteuid() == 0:
        print('STOP: run as electridrive-license user, never as root', file=sys.stderr)
        return 2
    try:
        if args.env_file:
            load_service_env(args.env_file)
        if args.initialize:
            with server.processing_lock():
                server._initialize_deliveries()
        else:
            server.retry_deliveries()
        return 0
    except Exception as exc:
        print(f'STOP: queue operation failed ({type(exc).__name__}).', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
