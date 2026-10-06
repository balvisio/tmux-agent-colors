#!/usr/bin/env bash
# install.sh: wire tmux-agent-colors into Claude Code, Codex and tmux. Idempotent.
#   ./install.sh            install, or update after editing any file (also restarts the poller)
#   ./install.sh uninstall  remove hooks, tmux wiring, the poller and all pane state
#
# Everything is derived from where this checkout lives and which python3 is on PATH, so the
# repository can be cloned anywhere. Re-run after moving it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$ROOT/agent_state.py"
TEMPLATE="$ROOT/agent-status.tmux.in"
SNIPPET="$ROOT/agent-status.tmux"
TMUX_CONF="${TMUX_CONF:-$HOME/.tmux.conf}"
CLAUDE_SETTINGS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
CODEX_HOOKS="${CODEX_HOME:-$HOME/.codex}/hooks.json"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/tmux-agent-colors"
MARKER="agent_state.py"
COMMENT_LINE="# tmux-agent-colors: colour window boxes by Claude/Codex pane state"
SOURCE_LINE="source-file $SNIPPET"

info() { printf '[tmux-agent-colors] %s\n' "$*"; }
die()  { printf '[tmux-agent-colors] error: %s\n' "$*" >&2; exit 1; }

# Remove every hook entry whose command mentions agent_state.py, then (mode=merge) append the
# groups from the fragment with @ROOT@ substituted. Everyone else's hooks are left untouched.
merge_hooks() {
  local fragment="$1" target="$2" mode="${3:-merge}"
  python3 - "$fragment" "$target" "$mode" "$MARKER" "$ROOT" <<'PY'
import json, sys
from pathlib import Path
fragment, target, mode, marker, root = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
data = {}
if target.exists():
    try:
        data = json.loads(target.read_text() or "{}")
    except json.JSONDecodeError as e:
        sys.exit(f"{target} is not valid JSON: {e}")
if not isinstance(data, dict):
    sys.exit(f"{target} is not a JSON object")
hooks = data.setdefault("hooks", {})
ours = lambda h: isinstance(h, dict) and marker in str(h.get("command", ""))
for event in list(hooks):
    groups = hooks.get(event)
    if not isinstance(groups, list):
        continue
    kept = []
    for g in groups:
        if isinstance(g, dict) and isinstance(g.get("hooks"), list):
            inner = [h for h in g["hooks"] if not ours(h)]
            if not inner:
                continue
            g = dict(g, hooks=inner)
        kept.append(g)
    if kept:
        hooks[event] = kept
    else:
        del hooks[event]
if mode == "merge":
    frag = json.loads(fragment.read_text().replace("@ROOT@", root))["hooks"]
    for event, groups in frag.items():
        hooks.setdefault(event, []).extend(groups)
if not hooks:
    data.pop("hooks", None)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(data, indent=2) + "\n")
PY
}

# Keep exactly one source line for our snippet in ~/.tmux.conf (mode=remove drops it).
edit_tmux_conf() {
  local mode="${1:-add}"
  python3 - "$TMUX_CONF" "$COMMENT_LINE" "$SOURCE_LINE" "$mode" <<'PY'
import sys
from pathlib import Path
conf, comment, source, mode = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
lines = conf.read_text().splitlines() if conf.exists() else []
kept = [l for l in lines if "agent-status.tmux" not in l and l.strip() != comment]
while kept and not kept[-1].strip():
    kept.pop()
if mode == "add":
    kept += ["", comment, source]
conf.write_text("\n".join(kept) + "\n")
PY
}

stop_daemon() {
  local lock pid
  for lock in "$CACHE_DIR"/daemon-*.lock; do
    [[ -f "$lock" ]] || continue
    pid=$(cat "$lock" 2>/dev/null || true)
    if [[ -n "$pid" ]] && kill "$pid" 2>/dev/null; then
      info "stopped poller pid $pid"
      sleep 1
    fi
    rm -f "$lock"
  done
}

tmux_running() { tmux list-sessions >/dev/null 2>&1; }

cmd_install() {
  local py
  py="$(command -v python3 || true)"
  [[ -n "$py" ]] || die "python3 not found on PATH"
  command -v tmux >/dev/null 2>&1 || die "tmux not found on PATH"
  chmod +x "$SCRIPT" "$ROOT/install.sh"

  info "generating $SNIPPET (python: $py, root: $ROOT)"
  sed -e "s|@PYTHON@|$py|g" -e "s|@ROOT@|$ROOT|g" "$TEMPLATE" > "$SNIPPET"

  info "merging Claude Code hooks into $CLAUDE_SETTINGS"
  merge_hooks "$ROOT/claude-hooks.json" "$CLAUDE_SETTINGS"
  info "merging Codex hooks into $CODEX_HOOKS"
  merge_hooks "$ROOT/codex-hooks.json" "$CODEX_HOOKS"

  info "pointing $TMUX_CONF at the snippet"
  edit_tmux_conf add

  if tmux_running; then
    stop_daemon
    info "applying to the running tmux server (formats, select-window hook, poller)"
    tmux source-file "$SNIPPET"
    "$py" "$SCRIPT" poll || true
  else
    info "no tmux server running; the snippet loads with the next server start"
  fi

  cat <<EOF

Done. Notes:
  - Claude Code and Codex read hooks at startup: sessions already running keep working
    through the poller (presence, busy/idle and background jobs from their on-disk state)
    but only pick up the full hook-driven behaviour (questions in violet, background-task
    awareness, instant transitions) after they are restarted.
  - Codex may ask you to trust the new hooks the next time it starts.
  - Inspect state with:  $py $SCRIPT status      log: $CACHE_DIR/log
EOF
}

cmd_uninstall() {
  info "removing hook entries"
  merge_hooks "$ROOT/claude-hooks.json" "$CLAUDE_SETTINGS" remove
  merge_hooks "$ROOT/codex-hooks.json" "$CODEX_HOOKS" remove
  if [[ -f "$TMUX_CONF" ]]; then
    info "removing the source line from $TMUX_CONF"
    edit_tmux_conf remove
  fi
  stop_daemon
  if tmux_running; then
    python3 "$SCRIPT" clear || true
    tmux set-option -gu window-status-format \; set-option -gu window-status-current-format \; \
         set-hook -gu after-select-window 2>/dev/null || true
    info "tmux formats reset to defaults; re-source your own settings if you had custom ones"
  fi
  rm -f "$SNIPPET"
  info "done (pane state cleared; $CACHE_DIR left in place for the log)"
}

case "${1:-install}" in
  install)   cmd_install ;;
  uninstall) cmd_uninstall ;;
  *) echo "usage: $0 [install|uninstall]" >&2; exit 2 ;;
esac
