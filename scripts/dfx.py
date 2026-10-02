"""Run from the checkout without installing project dependencies."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

def _entry() -> None:
    # Inventory initialization can reject before CLI admission is available.
    # Keep this catch outside execution so admitted failures retain their meaning.
    try:
        from dfx_agent.provenance import verify_host_baseline

        verify_host_baseline()
        from dfx_agent.cli import main

        verify_host_baseline()
    except (OSError, ValueError):
        if len(sys.argv) > 1 and sys.argv[1] == "resume":
            print("Resume rejected before execution: execution source inventory is unavailable", file=sys.stderr)
            raise SystemExit(3) from None
        raise
    main()


if __name__ == "__main__":
    _entry()
