#!/usr/bin/env bash
# Run the unit tests (no tmux needed). The tmux end-to-end check is tests/regress_hooks.sh.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
python3 -m py_compile ../agent_state.py
python3 test_tracker.py
python3 test_approvals.py
