# tmux-agent-colors

Colours each tmux window box by what the Claude Code and Codex panes inside it are doing,
so you can see from the status bar which agents are thinking, which are waiting on you, and
which have finished everything.

| Colour     | Meaning |
|------------|---------|
| violet     | a question or approval is waiting for you in some pane |
| yellow     | some agent is thinking, waiting on background work (a Monitor, a backgrounded command), or finished in the last few seconds |
| dark green | every agent in the window has finished and you have not visited the window since |
| blue       | agents present: idle and already seen, or present with unknown state (unknown never turns green) |
| default    | no agent in the window |

Green needs every pane to be done. With a [kibitz](https://github.com/balvisio/kibitz)
reviewer in the window, the window stays yellow until Codex has replied too; a Claude pane
with a Monitor armed stays yellow until the Monitor fires and that turn ends; a Codex pane
whose command outlived its tool call stays yellow until that process exits. Dark green clears
to blue the moment you visit the window.

## Install

```
git clone git@github.com:balvisio/tmux-agent-colors.git ~/repos/tmux-agent-colors
~/repos/tmux-agent-colors/install.sh
```

The installer is idempotent. It generates `agent-status.tmux` from the template with the
python3 on your PATH and the checkout location, merges the hook fragments into
`~/.claude/settings.json` and `~/.codex/hooks.json` (other hooks are left alone), adds one
`source-file` line to `~/.tmux.conf`, and, if a tmux server is running, applies everything
to it and starts the poller. Re-run it after editing or moving the checkout.
`install.sh uninstall` reverses all of it.

Requirements: tmux 3.3 or newer (nested `#{P:}` loops in window formats; tested on 3.7b),
python3, macOS or Linux. Codex hooks need `[features] hooks = true` in `~/.codex/config.toml`
and Codex may ask you to trust the new hooks on its next start. Agents already running keep
working through the poller but only get the hook-driven parts (instant transitions,
questions in violet, background-task awareness) after a restart.

## How it works

State lives in `@as_*` tmux pane options, so it dies with the pane and follows the pane when
you join it into another window. The window colour is derived inside the tmux format from
the per-pane render letters, so moves and splits need no bookkeeping.

**Hooks** give instant transitions. Prompts and tool calls set busy; Stop clears it; permission
and question events add a pending entry; Claude's Stop payload says whether background tasks
or scheduled wake-ups are still outstanding.

**The poller** (every 3 s, started by the tmux snippet, one instance per tmux socket) owns
presence and identity (process tree under each pane, PID plus start time), reconciles busy
and idle against Claude's `~/.claude/sessions/<pid>.json` and Codex rollout logs, tracks
Codex background jobs, promotes finished panes after a 5 s settle period, and acknowledges
finished panes in visible windows.

The completion gate per pane is: replied, not thinking, nothing outstanding and the
outstanding list known to be complete, nothing asked.

### Outstanding work

For Claude, the Stop payload's `background_tasks` and `session_crons` lists are authoritative
and are only ever changed by a later Stop; a payload without those fields leaves the flag as
it was.

For Codex, jobs are tracked from its rollout log and from hook tool responses. An exec result
envelope with a `session_id` and no integer `exit_code` means the process outlived its yield
time and registers a job; a later `item_completed` CommandExecution record with that
`process_id` and a terminal status, or an envelope with an integer `exit_code`, retires it.
Codex writes that record even when the process exits after the turn ended (verified: reply,
then process exit record 41 s later, yellow in between). Session ids are Codex's own handles,
not OS pids, so there is no per-job liveness check; as a safety net, once tracked jobs are
older than 10 s and the Codex process has no command children left, the list is treated as
stale. Envelopes are decoded as JSON and recognised by their own fields whether or not they
carry a session id, with their output treated as opaque text, so a command printing an
example chunk cannot create a phantom job and braces or quotes in output cannot hide one.
Green requires the rollout to have been scanned in full at least once; hooks alone never
satisfy the gate.

### Questions and approvals

Claude permission prompts wait 5 s before turning violet so an auto-accept script (such as a
Hammerspoon one) can handle them first; Claude's session file reports `waiting` while a
dialog is on screen and `busy` once answered, which both confirms a pending prompt and clears
it. Codex approvals and explicit questions turn violet immediately.

Approvals are matched to calls by identity. PreToolUse records each call under a signature of
session, tool and input; a PermissionRequest, which carries no call id, binds to the single
recorded call with the same signature that does not already own an approval, or waits for
that call's PreToolUse if it arrives later; completion retires exactly that call's approval.
Associations are one-to-one and never overwritten. Ambiguous cases (identical concurrent
inputs) stay pending until Stop, a new prompt, or Claude's session file leaving `waiting`
clears them. There is no tool-name fallback, so violet can stay on too long but never clear
too early.

### Other rules

- Every pane update runs under a per-pane lock, so concurrent hooks cannot drop each other's
  entries.
- Codex `Interrupt` (and `turn_aborted` in the rollout) means idle, not a reply: blue, never
  dark green.
- Idle needs evidence (SessionStart, Stop, Interrupt, Claude's file saying idle, a Codex
  `task_complete`). An agent pane with no evidence renders blue and blocks green.
- An agent replaced in its pane without SessionStart/SessionEnd reaching us is detected by PID
  and start time, and all inherited state is dropped.

## Files

- `agent_state.py`: hook entry points, poller, state logic.
- `agent-status.tmux.in`: colours, window formats, the select-window acknowledgement hook, and
  the poller launch. Edit this, then re-run `install.sh`; `agent-status.tmux` is generated.
- `claude-hooks.json`, `codex-hooks.json`: hook fragments merged by the installer.
- `install.sh`: install, update, uninstall.
- `tests/run.sh`: unit tests; `tests/regress_hooks.sh`: end-to-end hook check on a scratch tmux
  window.

Runtime state and the log live in `~/.cache/tmux-agent-colors/`. Every render transition is
logged with the event that caused it.

## Commands

```
python3 ~/repos/tmux-agent-colors/agent_state.py status   # per-pane table
python3 ~/repos/tmux-agent-colors/agent_state.py poll     # one poller pass
python3 ~/repos/tmux-agent-colors/agent_state.py clear    # drop all state
~/repos/tmux-agent-colors/install.sh uninstall
```

Tunables (settle time, permission debounce, poll interval, job grace) are constants at the
top of `agent_state.py`; colours are the four `@as_style_*` lines in `agent-status.tmux.in`.

## License

MIT, see `LICENSE`.
