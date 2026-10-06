#!/usr/bin/env python3
"""tmux-agent-colors: colour tmux window boxes by the state of Claude Code / Codex panes.

State lives in tmux pane options (prefix @as_), one set per pane, so it dies with the
pane and follows the pane when it is joined into another window. The tmux formats in
agent-status.tmux aggregate the per-pane render letter across each window.

Per-pane facts (kept separate on purpose; nothing clears a flag except its own resolver):
  kind        claude | codex                      (poller + hooks)
  pid/pstart  agent process identity              (poller)
  session     agent session id                    (hooks, or Claude's session file)
  transcript  rollout / transcript path           (hooks)
  hooked      1 if this session's SessionStart hook was seen (informational)
  busy        1 while a turn is running           (hooks; poller reconciles)
  asking      pending questions "id@ts/debounce"  (hooks; cleared per id)
  bg          1 while Claude reports background tasks / crons after Stop (hooks only)
  jobs        Codex processes that outlived their tool call: "pid@ts ..." (rollout + hooks)
  replied     1 once the agent's turn ended with a reply (Stop); 0 after Interrupt / new prompt
  roff        byte offset reached in the Codex rollout (incremental job scan)
  finished_at epoch when replied with nothing outstanding (settle timer + seen tracking)
  seen        1 once the window was visible after finishing
  render      Q asking | B busy/bg/settling | D finished unseen | I idle seen | P unknown

Rules worth knowing:
  - Every read-modify-write of a pane happens under a per-pane file lock (hooks of a
    parallel tool batch run concurrently).
  - Pending questions are removed only by their own resolution (same tool id or tool
    name, prompt, Stop, Interrupt) or by Claude's session file leaving "waiting".
  - Codex Interrupt and turn_aborted mean idle, not "replied": never dark green.
  - Idle (I) needs evidence; an agent with no evidence is P and blocks green.
  - A Claude Stop payload without background fields leaves the bg flag untouched.

Subcommands:
  claude-hook   Claude Code hook entry point (hook JSON on stdin)
  codex-hook    Codex hook entry point (hook JSON on stdin)
  poll          one pass: presence, identity, reconcile, settle, ack
  daemon        run poll forever (one instance per tmux socket)
  ack           mark finished panes in visible windows as seen
  status        print the per-pane state table
  clear         remove every @as_ option from every pane
"""
import contextlib
import fcntl
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import traceback
from collections import defaultdict, deque
from pathlib import Path

# ---- tunables -------------------------------------------------------------------------
POLL_INTERVAL = 3.0            # seconds between poller passes
SETTLE_SECONDS = 5.0           # a finished pane keeps rendering busy this long (relay handoff)
PERMISSION_DEBOUNCE = 5.0      # Claude permission prompts wait this long before violet
RECONCILE_STABLE_SECONDS = 3.0 # file-based truth must be this old before it overrides hooks
STALE_ASKING_SECONDS = 20.0    # drop pending asks once Claude's file says idle for this long
FILE_WAITING_IS_ASKING = True  # Claude session status "waiting" (stable 3 s) counts as a pending question

OPT_PREFIX = "@as_"
KEYS = ["kind", "pid", "pstart", "session", "transcript", "hooked", "busy", "asking", "calls", "bg",
        "jobs", "replied", "roff", "finished_at", "seen", "render"]
MAX_CALLS = 64                # in-flight tool calls remembered per pane (Stop clears them anyway)
JOB_GRACE_SECONDS = 10.0      # tracked Codex jobs must be this old before the tree check can retire them

CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "tmux-agent-colors"
LOG_PATH = CACHE_DIR / "log"
CLAUDE_SESSIONS = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "sessions"
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
KIBITZ_CACHE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "kibitz"

SOCKET = None  # tmux socket path; set from --socket or $TMUX


def find_tmux():
    """tmux's own run-shell has a minimal PATH, so fall back to the usual install locations."""
    found = shutil.which("tmux")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux"):
        if os.access(cand, os.X_OK):
            return cand
    return "tmux"


TMUX_BIN = find_tmux()


class TmuxGone(Exception):
    pass


# ---- logging --------------------------------------------------------------------------
def log(msg):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 1_000_000:
            LOG_PATH.write_text("")
        with LOG_PATH.open("a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except Exception:
        pass


# ---- tmux plumbing --------------------------------------------------------------------
def tmux(*args):
    cmd = [TMUX_BIN]
    if SOCKET:
        cmd += ["-S", SOCKET]
    cmd += list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise TmuxGone(str(e))
    if r.returncode != 0:
        err = r.stderr.strip()
        if "no server running" in err or "error connecting" in err or "no current client" in err:
            raise TmuxGone(err)
        return r.stdout, err
    return r.stdout, ""


def resolve_socket(explicit=None):
    global SOCKET
    if explicit:
        SOCKET = explicit
        return
    env = os.environ.get("TMUX", "")
    if env:
        SOCKET = env.split(",")[0] or None


@contextlib.contextmanager
def pane_lock(pane):
    """Serialise read-modify-write of one pane's state across concurrent hooks, the poller
    and ack. Claude runs the hooks of a parallel tool batch concurrently, so without this
    two hooks could each read the same list of pending questions and overwrite each other."""
    lock_dir = CACHE_DIR / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / f"pane-{pane.lstrip('%')}.lock").open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def read_state(pane):
    out, _ = tmux("show-options", "-p", "-t", pane)
    st = {}
    for line in out.splitlines():
        try:
            parts = shlex.split(line)
        except ValueError:
            parts = line.split(None, 1)
        if parts and parts[0].startswith(OPT_PREFIX):
            st[parts[0][len(OPT_PREFIX):]] = parts[1] if len(parts) > 1 else ""
    return st


def write_state(pane, old, new, why=""):
    """Write only the changed keys, in a single tmux invocation. Returns True if anything changed."""
    if (old.get("render") or "") != (new.get("render") or ""):
        log(f"{pane} {new.get('kind') or old.get('kind') or '?'}: "
            f"{old.get('render') or '-'} -> {new.get('render') or '-'}"
            f"{' (' + why + ')' if why else ''}")
    cmds = []
    for k in KEYS:
        nv = str(new.get(k, "") or "")
        ov = str(old.get(k, "") or "")
        if nv == ov:
            continue
        if cmds:
            cmds.append(";")
        if nv == "":
            cmds += ["set-option", "-pu", "-t", pane, OPT_PREFIX + k]
        else:
            cmds += ["set-option", "-p", "-t", pane, OPT_PREFIX + k, nv]
    if not cmds:
        return False
    _, err = tmux(*cmds)
    if err:
        log(f"write_state {pane}: {err}")
    return True


def clear_pane(pane, old):
    write_state(pane, old, {})


def refresh_clients():
    out, _ = tmux("list-clients", "-F", "#{client_name}")
    names = [n for n in out.split() if n]
    if not names:
        return
    cmds = []
    for n in names:
        if cmds:
            cmds.append(";")
        cmds += ["refresh-client", "-S", "-t", n]
    tmux(*cmds)


def pane_visible(pane):
    out, _ = tmux("display-message", "-p", "-t", pane, "#{window_active} #{session_attached}")
    parts = out.split()
    return len(parts) == 2 and parts[0] == "1" and parts[1] not in ("", "0")


PANE_FIELDS = ["pane_id", "pane_pid", "window_id", "window_index", "window_active", "session_attached"]


def list_panes():
    fmt = "\t".join(["#{%s}" % f for f in PANE_FIELDS] + ["#{%s%s}" % (OPT_PREFIX, k) for k in KEYS])
    out, _ = tmux("list-panes", "-a", "-F", fmt)
    rows = []
    for line in out.splitlines():
        cols = line.split("\t")
        if len(cols) < len(PANE_FIELDS):
            continue
        cols += [""] * (len(PANE_FIELDS) + len(KEYS) - len(cols))
        row = dict(zip(PANE_FIELDS, cols[:len(PANE_FIELDS)]))
        row["state"] = dict(zip(KEYS, cols[len(PANE_FIELDS):]))
        row["visible"] = row["window_active"] == "1" and row["session_attached"] not in ("", "0")
        rows.append(row)
    return rows


# ---- state helpers --------------------------------------------------------------------
def parse_asking(s):
    """'id@ts/debounce id@ts/debounce' -> [(id, ts, debounce)]"""
    out = []
    for tok in (s or "").split():
        try:
            ident, rest = tok.rsplit("@", 1)
            ts, deb = rest.split("/", 1)
            out.append((ident, float(ts), float(deb)))
        except ValueError:
            continue
    return out


def format_asking(entries):
    return " ".join(f"{i}@{int(ts)}/{int(d)}" for i, ts, d in entries)


def add_asking(st, ident, now, debounce):
    ident = ident.replace(" ", "_").replace("\t", "_")
    entries = [e for e in parse_asking(st.get("asking", "")) if e[0] != ident]
    entries.append((ident, now, debounce))
    st["asking"] = format_asking(entries)


def remove_asking(st, ids=(), prefixes=()):
    entries = parse_asking(st.get("asking", ""))
    kept = [e for e in entries
            if e[0] not in ids and not any(e[0].startswith(p) for p in prefixes)]
    st["asking"] = format_asking(kept)


def has_asking(st, prefix):
    return any(e[0].startswith(prefix) for e in parse_asking(st.get("asking", "")))


def unique_suffix(now):
    return f"{int(now * 1e6)}-{os.getpid()}"


# ---- approvals matched by call identity ------------------------------------------------
# PermissionRequest payloads carry no call id (verified: only session_id, tool_name,
# tool_input and context fields), but PreToolUse carries tool_use_id plus the same
# tool_input. So calls are remembered at PreToolUse under a signature of
# (session, tool, input); an approval binds to the one remembered call with the same
# signature that does not already own an approval; a completion retires exactly its own
# call's approval. Associations are strictly one-to-one and never overwritten. Nothing ever
# falls back to the tool name: an ambiguous approval (identical concurrent inputs) stays
# pending until an authoritative signal resolves it (Stop, a new prompt, or Claude's
# session file leaving "waiting"), which can only keep violet on too long, never clear it
# too early.

def call_signature(payload):
    if not payload.get("session_id") or not payload.get("tool_name") or "tool_input" not in payload:
        return None
    encoded = json.dumps([payload["session_id"], payload["tool_name"], payload["tool_input"]],
                         sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()[:16]


def parse_calls(s):
    """'call_id=sig call_id=sig' -> {call_id: sig}"""
    out = {}
    for tok in (s or "").split():
        cid, _, sig = tok.partition("=")
        if cid:
            out[cid] = sig
    return out


def format_calls(calls):
    items = list(calls.items())[-MAX_CALLS:]
    return " ".join(f"{c}={s}" for c, s in items)


def owned_calls(entries):
    """Call ids that already own a pending approval entry."""
    return {e[0][len("perm:"):] for e in entries
            if e[0].startswith("perm:") and not e[0].startswith(("perm:sig:", "perm:unmatched:"))}


def remember_call(st, payload, now):
    """PreToolUse: record the call. An approval that arrived earlier without a call id
    (if the agent's hook order puts the request first) is bound to this call only when the
    association is unambiguous: it is the sole waiting approval for this input and this is
    the sole in-flight call with this input. Otherwise it stays pending."""
    tuid = payload.get("tool_use_id")
    sig = call_signature(payload)
    if not tuid:
        return
    calls = parse_calls(st.get("calls", ""))
    others_same_input = [cid for cid, s in calls.items() if sig and s == sig and cid != tuid]
    calls[tuid] = sig or ""
    st["calls"] = format_calls(calls)
    if not sig or others_same_input:
        return
    entries = parse_asking(st.get("asking", ""))
    waiting = [e for e in entries if e[0].startswith(f"perm:sig:{sig}:")]
    if len(waiting) == 1 and tuid not in owned_calls(entries):
        ident, ts, deb = waiting[0]
        entries.remove(waiting[0])
        entries.append((f"perm:{tuid}", ts, deb))
        st["asking"] = format_asking(entries)


def approval_key(st, payload, now):
    """PermissionRequest: the pending entry's id. Exact call id if offered; else the single
    remembered call with the same signature that does not already own an approval (one
    approval per call, never overwritten); else a signature-bearing entry a later PreToolUse
    may bind; else an unmatched entry that only an authoritative signal clears."""
    tuid = payload.get("tool_use_id")
    entries = parse_asking(st.get("asking", ""))
    owned = owned_calls(entries)
    if tuid:
        return f"perm:{tuid}" if tuid not in owned else f"perm:unmatched:{unique_suffix(now)}"
    sig = call_signature(payload)
    if sig:
        matches = [cid for cid, s in parse_calls(st.get("calls", "")).items() if s == sig and cid not in owned]
        if len(matches) == 1:
            return f"perm:{matches[0]}"
        if not matches:
            return f"perm:sig:{sig}:{unique_suffix(now)}"
    return f"perm:unmatched:{unique_suffix(now)}"


def complete_call(st, payload):
    """PostToolUse / PostToolUseFailure: retire exactly this call's approval or question.
    Without a call id there is no evidence of which approval resolved, so nothing is
    retired. The permission notification, not tied to a call, goes once no approval and no
    question is pending any more."""
    tuid = payload.get("tool_use_id")
    if not tuid:
        return
    entries = parse_asking(st.get("asking", ""))
    own = {f"perm:{tuid}", f"q:{tuid}"}
    matched = any(e[0] in own for e in entries)
    entries = [e for e in entries if e[0] not in own]
    calls = parse_calls(st.get("calls", ""))
    calls.pop(tuid, None)
    st["calls"] = format_calls(calls)
    if matched and not any(e[0].startswith(("perm:", "q:")) for e in entries):
        entries = [e for e in entries if not e[0].startswith("notif:permission")]
    st["asking"] = format_asking(entries)


def reset_flags(st):
    st["busy"] = "0"
    st["asking"] = ""
    st["calls"] = ""
    st["bg"] = "0"
    st["jobs"] = ""
    st["replied"] = "0"
    st["roff"] = ""
    st["finished_at"] = ""
    st["seen"] = "0"


def reset_for_new_agent(st):
    """The process in the pane changed without SessionStart/SessionEnd reaching us: nothing
    inherited can be trusted, including the claim that hooks captured every event."""
    reset_flags(st)
    st["busy"] = ""
    st["session"] = ""
    st["transcript"] = ""
    st["hooked"] = ""


def start_work(st):
    """A new turn: thinking again. Background jobs are deliberately kept."""
    st["busy"] = "1"
    st["replied"] = "0"
    st["finished_at"] = ""
    st["seen"] = "0"


def finish(st, now, visible):
    st["busy"] = "0"
    st["finished_at"] = str(int(now))
    st["seen"] = "1" if visible else "0"


def parse_jobs(s):
    """'pid@ts pid@ts' -> {pid: ts}"""
    out = {}
    for tok in (s or "").split():
        pid, _, ts = tok.partition("@")
        try:
            out[pid] = float(ts or 0)
        except ValueError:
            out[pid] = 0.0
    return out


def format_jobs(jobs):
    return " ".join(f"{p}@{int(ts)}" for p, ts in sorted(jobs.items()))


def outstanding(st):
    """Work the agent is still waiting on after replying: Claude background tasks / crons,
    Codex processes that outlived their tool call."""
    return st.get("bg") == "1" or bool(st.get("jobs"))


def jobs_known(st):
    """Is the outstanding-work list complete? Claude: the Stop payload is authoritative.
    Codex: only once the whole rollout has been scanned (roff set). Hook tool responses also
    register jobs, but whether they always carry the exec envelope is unverified, so the
    hooked flag is informational and never sufficient for green."""
    if st.get("kind") != "codex":
        return True
    return bool(st.get("roff"))


def maybe_finish(st, now, visible):
    """The completion gate: replied, not thinking, nothing outstanding and the outstanding
    list known to be complete, nothing asked."""
    if (st.get("replied") == "1" and st.get("busy") != "1" and not outstanding(st)
            and jobs_known(st) and not parse_asking(st.get("asking", "")) and not st.get("finished_at")):
        finish(st, now, visible)


def compute_render(st, now):
    if not st.get("kind"):
        return ""
    if any(now >= ts + deb for _, ts, deb in parse_asking(st.get("asking", ""))):
        return "Q"
    if st.get("busy") == "1" or outstanding(st):
        return "B"
    try:
        fa = float(st.get("finished_at") or 0)
    except ValueError:
        fa = 0
    if fa:
        if now - fa < SETTLE_SECONDS:
            return "B"
        return "I" if st.get("seen") == "1" else "D"
    # Idle needs evidence (SessionStart, Interrupt, Claude's file saying idle, a Codex
    # task_complete) AND a complete picture of outstanding work. Anything less is P,
    # which blocks green for the whole window.
    if not jobs_known(st):
        return "P"
    return "I" if st.get("busy") == "0" else "P"


# ---- hooks ----------------------------------------------------------------------------
def handle_claude(payload, st, pane, now):
    new = dict(st)
    new["kind"] = "claude"
    ev = payload.get("hook_event_name") or ""
    if payload.get("session_id"):
        new["session"] = payload["session_id"]
    if payload.get("transcript_path"):
        new["transcript"] = payload["transcript_path"]
    tool = payload.get("tool_name") or ""
    tuid = payload.get("tool_use_id") or ""

    if ev == "SessionStart":
        if payload.get("source") != "compact":
            reset_flags(new)
            new["pid"] = ""      # let the poller adopt the new process identity
            new["pstart"] = ""
    elif ev == "UserPromptSubmit":
        start_work(new)
        new["asking"] = ""
        new["calls"] = ""
    elif ev == "PreToolUse":
        if new.get("busy") != "1":
            start_work(new)
        new["finished_at"] = ""
        remember_call(new, payload, now)
        if tool == "AskUserQuestion":
            add_asking(new, f"q:{tuid}" if tuid else f"q:unmatched:{unique_suffix(now)}", now, 0)
    elif ev == "PermissionRequest":
        new["busy"] = "1"
        new["finished_at"] = ""
        add_asking(new, approval_key(new, payload, now), now, PERMISSION_DEBOUNCE)
    elif ev in ("PostToolUse", "PostToolUseFailure"):
        new["busy"] = "1"
        new["finished_at"] = ""
        complete_call(new, payload)
    elif ev == "Notification":
        nt = payload.get("notification_type") or payload.get("matcher") or ""
        if nt == "permission_prompt":
            add_asking(new, "notif:permission", now, 0)
        elif nt in ("elicitation_dialog", "elicitation_url_dialog", "agent_needs_input"):
            add_asking(new, f"notif:{nt}", now, 0)
        elif nt in ("elicitation_complete", "elicitation_response"):
            remove_asking(new, prefixes=("notif:elicitation",))
        elif nt == "idle_prompt":
            # Only a hint: never overrides background work or a pending question.
            if new.get("busy") == "1" and new.get("bg") != "1" and not parse_asking(new.get("asking", "")):
                finish(new, now, pane_visible(pane))
    elif ev == "Elicitation":
        add_asking(new, "elicit:" + str(payload.get("elicitation_id") or payload.get("request_id") or "x"), now, 0)
    elif ev == "ElicitationResult":
        remove_asking(new, prefixes=("elicit:",))
    elif ev in ("Stop", "StopFailure"):
        if ev == "Stop":
            if "background_tasks" in payload or "session_crons" in payload:
                bt = payload.get("background_tasks") or []
                sc = payload.get("session_crons") or []
                new["bg"] = "1" if (bt or sc) else "0"
                log(f"{pane} Stop: background_tasks={len(bt) if isinstance(bt, list) else '?'} "
                    f"session_crons={len(sc) if isinstance(sc, list) else '?'} -> bg={new['bg']}")
            else:
                # Absent fields mean the task registry was unreachable, not that it is empty.
                log(f"{pane} Stop: no background fields in payload; bg stays {new.get('bg') or '0'}")
        new["asking"] = ""
        new["calls"] = ""
        new["busy"] = "0"
        new["replied"] = "1"
        new["finished_at"] = ""
        maybe_finish(new, now, pane_visible(pane))   # stays yellow while background work remains
    elif ev == "SessionEnd":
        clear_pane(pane, st)
        return True
    else:
        return False

    new["render"] = compute_render(new, now)
    return write_state(pane, st, new, why=ev)


# Codex exec tools answer with a JSON chunk such as
#   {"chunk_id":"6c8b7f","wall_time_seconds":1.0,"session_id":54461,"original_token_count":0,"output":""}
# A chunk whose exit_code is absent or null means the process outlived its yield time and is
# still running; an integer exit_code means it ended. The session_id is Codex's own handle
# (NOT an OS pid) and equals process_id in the later item_completed CommandExecution record.
# In the rollout the chunk sits as a JSON string inside output parts; in hook payloads it
# arrives inside tool_response. Both are decoded structurally, never with regexes, so
# braces or quotes inside the command's own output cannot hide a running process.
_NONTERMINAL = {"running", "in_progress", "inprogress", "started", "pending", "queued", "active"}
_MAX_EMBEDDED = 4_000_000   # do not try to decode strings larger than this


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def is_envelope(obj):
    """An exec result envelope, with or without a session_id. Observed shapes in the installed
    Codex: a command that finished within its yield time gives
      {"chunk_id","wall_time_seconds","exit_code","original_token_count","output"}
    and one still running gives
      {"chunk_id","wall_time_seconds","session_id","original_token_count","output"}.
    Either way the envelope's output is the command's own text and must stay opaque."""
    return isinstance(obj, dict) and "chunk_id" in obj and "wall_time_seconds" in obj and "output" in obj


def iter_json_objects(text, depth=0):
    """Yield every JSON object decodable from text: the whole text if it is JSON, otherwise
    each object a real decoder can read starting at a '{' (so strings containing braces or
    quotes are handled by the decoder, not guessed at)."""
    text = text.strip()
    if not text or len(text) > _MAX_EMBEDDED:
        return
    try:
        yield from walk_json(json.loads(text), depth)
        return
    except ValueError:
        pass
    dec = json.JSONDecoder()
    i, tries = 0, 0
    while tries < 500:
        j = text.find("{", i)
        if j == -1:
            break
        try:
            obj, end = dec.raw_decode(text, j)
        except ValueError:
            i, tries = j + 1, tries + 1
            continue
        yield from walk_json(obj, depth)
        i = max(end, j + 1)


def walk_json(obj, depth=0):
    """Yield dicts inside obj, recursing into containers (code-mode wraps envelopes in
    {"status":"fulfilled","value":...} and similar) and into string fields that hold JSON
    themselves. A recognised envelope is yielded but not descended into: its 'output' is the
    command's own text and may legitimately quote example chunks."""
    if depth > 6:
        return
    if isinstance(obj, dict):
        yield obj
        if is_envelope(obj):
            return
        for v in obj.values():
            yield from walk_json(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_json(v, depth + 1)
    elif isinstance(obj, str) and "session_id" in obj:
        yield from iter_json_objects(obj, depth + 1)


def register_jobs(st, data, now):
    """Update the pane's job list from an exec tool result (any shape: string, dict, list of
    output parts). Returns (added, removed) session handles."""
    if data is None:
        return [], []
    jobs = parse_jobs(st.get("jobs", ""))
    added, removed = [], []
    source = iter_json_objects(data) if isinstance(data, str) else walk_json(data)
    for obj in source:
        if not is_envelope(obj) or not is_int(obj.get("session_id")):
            continue   # not an exec result, or one that finished within its yield time
        handle = str(obj["session_id"])
        if is_int(obj.get("exit_code")):
            if jobs.pop(handle, None) is not None:
                removed.append(handle)
        elif handle not in jobs:
            jobs[handle] = now
            added.append(handle)
    st["jobs"] = format_jobs(jobs)
    return added, removed


def is_terminal_record(item):
    """A CommandExecution item_completed record only retires a job with a real terminal result."""
    status = str(item.get("status") or "").lower()
    if status in ("completed", "failed"):
        return True
    return status not in _NONTERMINAL and is_int(item.get("exit_code"))


def handle_codex(payload, st, pane, now):
    new = dict(st)
    new["kind"] = "codex"
    ev = payload.get("hook_event_name") or ""
    if payload.get("session_id"):
        new["session"] = payload["session_id"]
    if payload.get("transcript_path"):
        new["transcript"] = payload["transcript_path"]
    tool = payload.get("tool_name") or ""

    if ev == "SessionStart":
        if payload.get("source") != "compact":
            reset_flags(new)
            new["pid"] = ""
            new["pstart"] = ""
            # Hooks were active from this session's first moment. Informational only: the
            # completion gate relies on the rollout scan, see jobs_known().
            new["hooked"] = "1"
    elif ev == "UserPromptSubmit":
        start_work(new)
        new["asking"] = ""
        new["calls"] = ""
    elif ev == "PreToolUse":
        if new.get("busy") != "1":
            start_work(new)
        new["finished_at"] = ""
        # Also binds an approval that arrived before this call's PreToolUse (Codex's event
        # order is not documented), so it resolves on this call's completion.
        remember_call(new, payload, now)
        if tool == "request_user_input":
            tuid = payload.get("tool_use_id")
            add_asking(new, f"q:{tuid}" if tuid else f"q:unmatched:{unique_suffix(now)}", now, 0)
    elif ev == "PermissionRequest":
        new["busy"] = "1"
        new["finished_at"] = ""
        add_asking(new, approval_key(new, payload, now), now, 0)
    elif ev in ("PostToolUse", "PostToolUseFailure"):
        new["busy"] = "1"
        new["finished_at"] = ""
        complete_call(new, payload)
        added, _ = register_jobs(new, payload.get("tool_response"), now)
        if added:
            log(f"{pane} codex: background process(es) {' '.join(added)} started")
    elif ev == "Stop":
        new["asking"] = ""
        new["calls"] = ""
        new["busy"] = "0"
        new["replied"] = "1"
        new["finished_at"] = ""
        maybe_finish(new, now, pane_visible(pane))   # stays yellow while tracked processes run
    elif ev == "Interrupt":
        # The user aborted the turn: idle, but not a reply, so never dark green.
        new["asking"] = ""
        new["calls"] = ""
        new["busy"] = "0"
        new["replied"] = "0"
        new["finished_at"] = ""
    elif ev == "SessionEnd":
        clear_pane(pane, st)
        return True
    else:
        return False

    new["render"] = compute_render(new, now)
    return write_state(pane, st, new, why=ev)


def hook_main(kind):
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return 0
    resolve_socket()
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    try:
        handler = handle_claude if kind == "claude" else handle_codex
        with pane_lock(pane):
            st = read_state(pane)
            changed = handler(payload, st, pane, time.time())
        if changed:
            refresh_clients()
    except TmuxGone:
        pass
    except Exception:
        log(f"{kind}-hook {pane} {payload.get('hook_event_name')}: {traceback.format_exc()}")
    return 0


# ---- process detection ----------------------------------------------------------------
def read_procs():
    try:
        r = subprocess.run(["ps", "-axo", "pid=,ppid=,lstart=,args="],
                           capture_output=True, text=True, timeout=10)
    except Exception as e:
        log(f"ps failed: {e}")
        return {}, defaultdict(list)
    procs = {}
    children = defaultdict(list)
    for line in r.stdout.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        pstart = f"{parts[3]}{parts[4]}-{parts[5]}-{parts[6]}"
        procs[pid] = (ppid, pstart, parts[7])
        children[ppid].append(pid)
    return procs, children


def is_helper(args):
    """Our own hook processes and kibitz's scripts: never count them as agents."""
    heads = [os.path.basename(t) for t in args.split()[:3]]
    return any(h == "agent_state.py" or h.startswith("kibitz") for h in heads) or "agent_state.py" in args


def classify(args):
    if is_helper(args):
        return None
    toks = args.split()
    heads = [os.path.basename(t) for t in toks[:2]]
    if "claude" in heads or "/claude/versions/" in args or "@anthropic-ai/claude-code" in args:
        return "claude"
    if "codex" in heads or "@openai/codex" in args or "codex-darwin" in args:
        return "codex"
    return None


def detect_agent(pane_pid, procs, children, hinted_kind=""):
    """Breadth-first from the pane's root process; the shallowest agent process wins."""
    try:
        root = int(pane_pid)
    except (TypeError, ValueError):
        return None
    order = []
    seen = {root}
    q = deque([(root, 0)])
    while q:
        pid, depth = q.popleft()
        if pid not in procs:
            continue
        order.append(pid)
        if depth < 8:
            for c in children.get(pid, []):
                if c not in seen:
                    seen.add(c)
                    q.append((c, depth + 1))
    for pid in order:
        kind = classify(procs[pid][2])
        if kind:
            return kind, pid, procs[pid][1]
    if hinted_kind:
        for pid in order:
            args = procs[pid][2]
            if hinted_kind in args.lower() and not is_helper(args):
                return hinted_kind, pid, procs[pid][1]
    return None


# ---- reconciliation against on-disk truth ---------------------------------------------
_seen_status = set()


def reconcile_claude(st, pid, now, visible):
    f = CLAUDE_SESSIONS / f"{pid}.json"
    try:
        data = json.loads(f.read_text())
    except Exception:
        return
    status = data.get("status") or ""
    try:
        updated = float(data.get("statusUpdatedAt") or 0) / 1000.0
    except (TypeError, ValueError):
        updated = 0
    stable = updated and (now - updated) >= RECONCILE_STABLE_SECONDS
    if data.get("sessionId") and not st.get("session"):
        st["session"] = data["sessionId"]
    if status not in ("busy", "idle", "waiting") and (pid, status) not in _seen_status:
        _seen_status.add((pid, status))
        log(f"claude pid {pid}: session file status {status!r} (asking={st.get('asking') or '-'})")
    # Observed 2026-10-06: the file is stamped only on status changes; "waiting" appears the
    # moment a permission dialog (or question) is shown and flips back to "busy" when answered.
    if status == "waiting":
        if st.get("busy") != "1":
            start_work(st)
        if FILE_WAITING_IS_ASKING and stable and not parse_asking(st.get("asking", "")):
            add_asking(st, "file:waiting", now, 0)
    elif status == "busy":
        # Going busy is applied at once (a wake-up before the first tool call has no hook),
        # and "busy" after a permission entry means the dialog was answered.
        if st.get("busy") != "1":
            start_work(st)
        remove_asking(st, prefixes=("perm:", "notif:", "file:", "elicit:"))
    elif status == "idle" and stable:
        # Going idle waits for the stamp to settle so it never races the Stop hook.
        remove_asking(st, prefixes=("perm:", "notif:", "file:", "elicit:"))
        if st.get("busy") == "1":
            st["busy"] = "0"
            if st.get("bg") != "1":
                finish(st, now, visible)
        elif st.get("busy") != "0":
            st["busy"] = "0"     # idle evidence for a pane adopted mid-session: I, not P
        if (now - updated) >= STALE_ASKING_SECONDS and st.get("asking"):
            st["asking"] = ""


def codex_rollout_path(st):
    tp = st.get("transcript") or ""
    if tp and Path(tp).exists():
        return Path(tp)
    sid = st.get("session") or ""
    if sid:
        matches = sorted(CODEX_HOME.glob(f"sessions/*/*/*/rollout-*-{sid}.jsonl"))
        if matches:
            return matches[-1]
    return None


def codex_last_lifecycle(path):
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            f.seek(max(0, size - 262144))
            chunk = f.read().decode("utf-8", "replace")
    except Exception:
        return None
    last = None
    for marker in ('"type":"task_started"', '"type":"task_complete"', '"type":"turn_aborted"',
                   '"type": "task_started"', '"type": "task_complete"', '"type": "turn_aborted"'):
        idx = chunk.rfind(marker)
        if idx != -1 and (last is None or idx > last[0]):
            last = (idx, marker.split('"')[3])
    return last[1] if last else None


def codex_command_children(agent_pid, procs, children):
    """Processes under the Codex agent that are neither Codex binaries nor our helpers:
    the shells of running commands (and their children)."""
    out = []
    q = deque(children.get(agent_pid, []))
    seen = set()
    while q:
        pid = q.popleft()
        if pid in seen or pid not in procs:
            continue
        seen.add(pid)
        args = procs[pid][2]
        head = os.path.basename(args.split()[0]) if args.split() else ""
        if head.startswith("codex") or "codex-darwin" in args or "@openai/codex" in args or is_helper(args):
            q.extend(children.get(pid, []))
            continue
        out.append(pid)
    return out


def reconcile_codex(st, pane, now, visible, agent_pid=None, procs=None, children=None):
    path = codex_rollout_path(st)
    if path is None:
        # kibitz keeps a pane -> rollout mapping for its reviewer panes; reuse it.
        kf = KIBITZ_CACHE / f"pane-{pane.lstrip('%')}.rollout"
        try:
            cand = Path(kf.read_text().strip())
            if cand.exists():
                path = cand
                st["transcript"] = str(cand)
        except Exception:
            return
    if path is None:
        return
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return
    last = codex_last_lifecycle(path)
    if last is None:
        return
    if not st.get("session"):
        name = path.name
        if name.startswith("rollout-") and name.endswith(".jsonl") and len(name) > 50:
            st["session"] = name[-42:-6]
    if last == "task_started":
        if st.get("busy") != "1":
            start_work(st)   # the log is written continuously during a turn, so no stability wait here
    elif last in ("task_complete", "turn_aborted"):
        if st.get("busy") == "1":
            if (now - mtime) >= RECONCILE_STABLE_SECONDS:
                st["asking"] = ""
                st["busy"] = "0"
                if last == "task_complete":
                    st["replied"] = "1"
                    st["finished_at"] = ""
                    maybe_finish(st, now, visible)
                else:
                    st["replied"] = "0"       # aborted: idle, not a reply
                    st["finished_at"] = ""
        elif st.get("busy") != "0":
            st["busy"] = "0"     # idle evidence for a pane adopted mid-session: I, not P
    scan_codex_jobs(st, pane, path, now, visible, agent_pid, procs, children)


def scan_codex_jobs(st, pane, path, now, visible, agent_pid=None, procs=None, children=None):
    """Track Codex processes that outlived their tool call, from the rollout log.

    Registration: an exec tool output chunk carrying a session_id and no integer exit_code.
    Retirement: an item_completed CommandExecution record with that process_id and a terminal
    status, or a later chunk for the same session_id carrying an integer exit_code.
    Verified 2026-10-06: Codex writes the completion record even when the process exits
    after the turn has ended, so this is the normal retirement path.
    Fallback: Codex session ids are not OS pids, so no per-job liveness check is possible;
    instead, once tracked jobs are older than the grace period and the Codex process has no
    command children left at all, the list must be stale and is cleared. While any command
    child runs, every job is kept (conservative: never retires a running job).
    The first scan covers the whole rollout so no older process is missed; afterwards
    @as_roff remembers how far into this rollout we have read."""
    try:
        size = path.stat().st_size
    except OSError:
        return
    spath = str(path)
    try:
        off = int(st.get("roff") or -1)
    except ValueError:
        off = -1
    if st.get("transcript") != spath or off < 0 or off > size:
        off = 0
        st["transcript"] = spath
    jobs_before = parse_jobs(st.get("jobs", ""))
    completed = set()
    if size > off:
        try:
            with path.open("rb") as f:
                f.seek(off)
                chunk = f.read(size - off)
        except OSError:
            return
        # Only consume whole lines; a partially written last line is re-read next time.
        cut = chunk.rfind(b"\n")
        if cut == -1:
            return
        new_off = off + cut + 1
        for raw in chunk[:cut].split(b"\n"):
            if not raw.strip():
                continue
            if b"session_id" in raw:
                try:
                    d = json.loads(raw)
                    p = d.get("payload") or {}
                    if isinstance(p, dict) and p.get("type") in ("custom_tool_call_output", "function_call_output"):
                        register_jobs(st, p.get("output"), now)
                except Exception:
                    pass
            if b"CommandExecution" in raw and b"item_completed" in raw:
                try:
                    d = json.loads(raw)
                    item = (d.get("payload") or {}).get("item") or {}
                    if (item.get("type") == "CommandExecution" and item.get("process_id") is not None
                            and is_terminal_record(item)):
                        completed.add(str(item["process_id"]))
                except Exception:
                    pass
        st["roff"] = str(new_off)
    jobs = parse_jobs(st.get("jobs", ""))
    for handle in list(jobs):
        if handle in completed:
            del jobs[handle]
    # Process-tree fallback: with jobs past the grace period and no command child under the
    # Codex process, nothing can still be running, so the remaining entries are stale.
    if jobs and procs is not None and agent_pid is not None \
            and all(now - ts >= JOB_GRACE_SECONDS for ts in jobs.values()) \
            and not codex_command_children(agent_pid, procs, children or {}):
        log(f"{pane} codex: no command processes left under pid {agent_pid}; retiring stale job(s) {' '.join(sorted(jobs))}")
        jobs = {}
    st["jobs"] = format_jobs(jobs)
    added = sorted(set(jobs) - set(jobs_before))
    gone = sorted(set(jobs_before) - set(jobs))
    if added:
        log(f"{pane} codex: tracking background process(es) {' '.join(added)}")
        if st.get("finished_at"):
            st["finished_at"] = ""     # finished too early: a process was still running
            st["seen"] = "0"
    if gone:
        log(f"{pane} codex: background process(es) {' '.join(gone)} finished")
    maybe_finish(st, now, visible)


# ---- poller ---------------------------------------------------------------------------
def poll(now):
    rows = list_panes()
    procs, children = read_procs()
    changed = False
    for row in rows:
        pane = row["pane_id"]
        agent = detect_agent(row["pane_pid"], procs, children, hinted_kind=row["state"].get("kind", ""))
        if agent is None and not any(row["state"].get(k) for k in KEYS):
            continue   # plain pane with no state: nothing to do, no lock needed
        with pane_lock(pane):
            st = read_state(pane)   # re-read under the lock; a hook may have run since list_panes
            if agent is None:
                if any(st.get(k) for k in KEYS):
                    clear_pane(pane, st)
                    changed = True
                continue
            kind, pid, pstart = agent
            new = dict(st)
            if st.get("pid") and (st.get("pid") != str(pid) or st.get("pstart") != pstart or st.get("kind") != kind):
                # The agent was replaced without SessionStart/SessionEnd reaching us: drop
                # inherited state; the new agent is unknown (P) until evidence arrives.
                log(f"{pane}: identity changed {st.get('kind')}/{st.get('pid')} -> {kind}/{pid}; resetting")
                reset_for_new_agent(new)
            new["kind"] = kind
            new["pid"] = str(pid)
            new["pstart"] = pstart
            if kind == "claude":
                reconcile_claude(new, pid, now, row["visible"])
            else:
                reconcile_codex(new, pane, now, row["visible"], pid, procs, children)
            if row["visible"] and new.get("finished_at") and new.get("seen") != "1":
                new["seen"] = "1"
            new["render"] = compute_render(new, now)
            if write_state(pane, st, new, why="poll"):
                changed = True
    if changed:
        refresh_clients()
    return changed


def ack(now):
    changed = False
    for row in list_panes():
        if not row["visible"] or not row["state"].get("kind"):
            continue
        with pane_lock(row["pane_id"]):
            st = read_state(row["pane_id"])
            new = dict(st)
            if new.get("finished_at") and new.get("seen") != "1":
                new["seen"] = "1"
            new["render"] = compute_render(new, now)
            if write_state(row["pane_id"], st, new, why="ack"):
                changed = True
    if changed:
        refresh_clients()


def daemon(interval):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = (SOCKET or "default").replace("/", "_")
    lock_path = CACHE_DIR / f"daemon-{key}.lock"
    lock = lock_path.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0
    lock.write(str(os.getpid()))
    lock.flush()
    log(f"daemon started pid={os.getpid()} socket={SOCKET or 'default'}")
    failures = 0
    while True:
        try:
            poll(time.time())
            failures = 0
        except TmuxGone as e:
            failures += 1
            if failures >= 5:
                log(f"daemon exiting: tmux unreachable ({e})")
                return 0
        except Exception:
            log("poll error: " + traceback.format_exc())
        time.sleep(interval)


def status():
    rows = list_panes()
    hdr = (f"{'pane':6} {'win':4} {'vis':3} {'kind':6} {'pid':7} {'R':1} {'busy':4} {'bg':2} {'rep':3} "
           f"{'seen':4} {'finished':10} jobs | asking | session")
    print(hdr)
    for row in rows:
        st = row["state"]
        if not st.get("kind"):
            continue
        print(f"{row['pane_id']:6} {row['window_index']:4} {'y' if row['visible'] else '-':3} "
              f"{st.get('kind',''):6} {st.get('pid',''):7} {st.get('render','') or '-':1} "
              f"{st.get('busy','') or '-':4} {st.get('bg','') or '-':2} {st.get('replied','') or '-':3} "
              f"{st.get('seen','') or '-':4} {st.get('finished_at','') or '-':10} "
              f"{st.get('jobs','') or '-'} | {st.get('asking','') or '-'} | {st.get('session','') or '-'}")


def clear_all():
    for row in list_panes():
        if any(row["state"].get(k) for k in KEYS):
            with pane_lock(row["pane_id"]):
                clear_pane(row["pane_id"], read_state(row["pane_id"]))
    refresh_clients()


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    sock = None
    interval = POLL_INTERVAL
    i = 0
    while i < len(rest):
        if rest[i] == "--socket" and i + 1 < len(rest):
            sock = rest[i + 1]
            i += 2
        elif rest[i] == "--interval" and i + 1 < len(rest):
            interval = float(rest[i + 1])
            i += 2
        else:
            i += 1
    if cmd == "claude-hook":
        return hook_main("claude")
    if cmd == "codex-hook":
        return hook_main("codex")
    resolve_socket(sock)
    try:
        if cmd == "poll":
            poll(time.time())
        elif cmd == "daemon":
            return daemon(interval)
        elif cmd == "ack":
            ack(time.time())
        elif cmd == "status":
            status()
        elif cmd == "clear":
            clear_all()
        else:
            print(__doc__)
            return 2
    except TmuxGone as e:
        print(f"tmux not reachable: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
