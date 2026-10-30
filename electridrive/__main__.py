from electridrive import qa_policy as qa
import sys

if __name__ == "__main__":
    sys.argv = [sys.argv[0], *qa.bootstrap_profile(sys.argv[1:])]
    from electridrive.ui.app import run_app
    raise SystemExit(run_app())
