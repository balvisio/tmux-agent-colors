#!/usr/bin/env bash
# End-to-end check of the hook entry points against a scratch tmux window: real tmux pane
# options, real per-pane locking, concurrent hook processes. Needs a running tmux server.
# The poller may clear the scratch pane between steps (it has no agent process), so run this
# with the poller stopped, or accept an occasional empty line and re-run.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmux list-sessions >/dev/null 2>&1 || { echo "no tmux server running" >&2; exit 2; }

P=$(tmux new-window -d -P -F '#{pane_id}' -n tac-test)
trap 'tmux kill-window -t "$P" 2>/dev/null' EXIT
H() { printf '%s' "$2" | TMUX_PANE=$P "$ROOT/agent_state.py" "$1"; }
# grep exits 1 when a pane has no state left (e.g. after SessionEnd); that is a valid result,
# not an error, so do not let pipefail abort the script there.
show() { echo "-- $1"; { tmux show-options -p -t "$P" | grep -E 'as_(render|busy|bg|jobs|replied|asking|calls|finished_at)' || true; } | tr '\n' ' '; echo; }

echo "=== claude: turn, background task, finish ==="
H claude-hook '{"hook_event_name":"SessionStart","session_id":"s1","source":"startup"}'; show "SessionStart (expect render I)"
H claude-hook '{"hook_event_name":"UserPromptSubmit","session_id":"s1"}'; show "prompt (expect B)"
H claude-hook '{"hook_event_name":"Stop","background_tasks":[{"id":"m1"}],"session_crons":[]}'; show "Stop with a Monitor pending (expect bg 1, render B)"
H claude-hook '{"hook_event_name":"PreToolUse","session_id":"s1","tool_name":"Bash","tool_use_id":"t","tool_input":{"command":"x"}}'
H claude-hook '{"hook_event_name":"Stop"}'; show "Stop without background fields (expect bg still 1)"
H claude-hook '{"hook_event_name":"Stop","background_tasks":[],"session_crons":[]}'; show "Stop clean (expect bg 0, finished_at set)"

echo "=== claude: concurrent approvals matched by identity ==="
H claude-hook '{"hook_event_name":"UserPromptSubmit","session_id":"s1"}'
for c in A B; do cmd=$([ "$c" = A ] && echo ls || echo pwd)
  (printf '{"hook_event_name":"PreToolUse","session_id":"s1","tool_name":"Bash","tool_use_id":"toolu_%s","tool_input":{"command":"%s"}}' "$c" "$cmd" | TMUX_PANE=$P "$ROOT/agent_state.py" claude-hook) &
done; wait
for cmd in ls pwd; do
  (printf '{"hook_event_name":"PermissionRequest","session_id":"s1","tool_name":"Bash","tool_input":{"command":"%s"}}' "$cmd" | TMUX_PANE=$P "$ROOT/agent_state.py" claude-hook) &
done; wait
show "two concurrent calls and approvals (expect perm:toolu_A and perm:toolu_B)"
H claude-hook '{"hook_event_name":"PostToolUse","session_id":"s1","tool_name":"Bash","tool_use_id":"toolu_A"}'; show "A finished (expect perm:toolu_B only)"
H claude-hook '{"hook_event_name":"PostToolUse","session_id":"s1","tool_name":"Bash","tool_use_id":"toolu_B"}'; show "B finished (expect no asking)"
H claude-hook '{"hook_event_name":"SessionEnd"}'; show "SessionEnd (expect nothing)"

echo "=== codex: background job keeps yellow after the reply; interrupt is idle ==="
H codex-hook '{"hook_event_name":"Stop"}'; show "Stop before SessionStart (expect render P)"
H codex-hook '{"hook_event_name":"SessionStart","session_id":"c1","source":"startup"}'
H codex-hook '{"hook_event_name":"UserPromptSubmit"}'
H codex-hook '{"hook_event_name":"PostToolUse","tool_name":"exec","tool_use_id":"e1","tool_response":{"output":[{"type":"input_text","text":"{\"chunk_id\":\"q\",\"wall_time_seconds\":1.0,\"session_id\":17527,\"original_token_count\":0,\"output\":\"says {ready} \\\"ok\\\"\"}"}]}}'; show "launch (expect jobs 17527)"
H codex-hook '{"hook_event_name":"Stop"}'; show "Stop with job (expect replied 1, no finished_at, render B)"
H codex-hook '{"hook_event_name":"Interrupt"}'; show "Interrupt (expect replied 0, still B because the job is outstanding)"
H codex-hook '{"hook_event_name":"SessionEnd"}'
echo "done"
