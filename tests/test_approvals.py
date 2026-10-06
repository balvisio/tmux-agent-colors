#!/usr/bin/env python3
"""Approvals matched to calls by identity: one-to-one, never overwritten, never guessed."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_state as a  # noqa: E402

a.log = lambda m: None
now = time.time()
failures = 0


def check(label, got, expect):
    global failures
    ok = got == expect
    failures += 0 if ok else 1
    print(("ok  " if ok else "FAIL"), label, "->", repr(got), "" if ok else f"(expected {expect!r})")


def ids(st):
    return sorted(e[0] for e in a.parse_asking(st["asking"]))


def kinds(st):
    return sorted(i.split(":")[1] if i.startswith(("perm:sig:", "perm:unmatched:")) else i for i in ids(st))


def pre(tuid, cmd):
    return {"hook_event_name": "PreToolUse", "session_id": "s", "tool_name": "Bash", "tool_use_id": tuid, "tool_input": {"command": cmd}}


def req(cmd):
    return {"hook_event_name": "PermissionRequest", "session_id": "s", "tool_name": "Bash", "tool_input": {"command": cmd}}


def post(tuid):
    return {"hook_event_name": "PostToolUse", "session_id": "s", "tool_name": "Bash", "tool_use_id": tuid}


def approve(st, cmd, t):
    a.add_asking(st, a.approval_key(st, req(cmd), t), t, 0)


def fresh(kind="claude"):
    return {"kind": kind, "asking": "", "calls": ""}


# different inputs, PreToolUse first (Claude's order)
st = fresh()
a.remember_call(st, pre("A", "ls"), now)
a.remember_call(st, pre("B", "pwd"), now)
approve(st, "ls", now)
approve(st, "pwd", now)
check("two approvals bind to their calls", ids(st), ["perm:A", "perm:B"])
a.complete_call(st, post("A"))
check("A done leaves B", ids(st), ["perm:B"])
a.complete_call(st, post("A"))
check("duplicate completion removes nothing", ids(st), ["perm:B"])
a.complete_call(st, post("C"))
check("unrelated completion removes nothing", ids(st), ["perm:B"])
a.complete_call(st, post("B"))
check("B done clears", ids(st), [])

# the reviewer's four-event sequence: approval first, identical input while A runs
st = fresh("codex")
approve(st, "x", now)
a.remember_call(st, pre("A", "x"), now + 1)
check("early approval binds to the sole call", ids(st), ["perm:A"])
approve(st, "x", now + 2)
check("second approval not merged into A", kinds(st), ["perm:A", "sig"])
a.complete_call(st, post("A"))
check("A done keeps the waiting approval", kinds(st), ["sig"])
a.remember_call(st, pre("B", "x"), now + 3)
check("B's PreToolUse binds the waiting approval", ids(st), ["perm:B"])
a.complete_call(st, post("B"))
check("B done clears", ids(st), [])

# identical inputs, PreToolUse first: each approval binds to the call without one
st = fresh()
a.remember_call(st, pre("A", "x"), now)
approve(st, "x", now + 1)
a.remember_call(st, pre("B", "x"), now + 2)
approve(st, "x", now + 3)
check("identical inputs resolve one-to-one", ids(st), ["perm:A", "perm:B"])
a.complete_call(st, post("A"))
check("A done leaves B", ids(st), ["perm:B"])

# both calls in flight before any approval: ambiguous, stays pending through a completion
st = fresh()
a.remember_call(st, pre("A", "x"), now)
a.remember_call(st, pre("B", "x"), now)
approve(st, "x", now + 1)
approve(st, "x", now + 2)
check("ambiguous approvals stay unmatched", kinds(st), ["unmatched", "unmatched"])
a.complete_call(st, post("A"))
check("completion retires nothing ambiguous", len(ids(st)), 2)

# two waiting approvals before any PreToolUse: binding would be a guess
st = fresh("codex")
approve(st, "x", now)
approve(st, "x", now + 1)
a.remember_call(st, pre("A", "x"), now + 2)
check("two waiting approvals never bind", kinds(st), ["sig", "sig"])
a.remember_call(st, pre("B", "x"), now + 3)
a.complete_call(st, post("A"))
check("still pending after a completion", len(ids(st)), 2)

# miscellany
st = {"kind": "claude", "asking": "perm:A@1/0", "calls": "A=sig"}
check("duplicate call id becomes unmatched",
      a.approval_key(st, {"tool_use_id": "A", "session_id": "s", "tool_name": "Bash", "tool_input": {}}, now).startswith("perm:unmatched:"), True)
st = fresh("codex")
approve(st, "x", now)
approve(st, "y", now + 1)
a.remember_call(st, pre("A", "x"), now + 2)
a.remember_call(st, pre("B", "y"), now + 3)
check("different inputs bind under approval-first order", ids(st), ["perm:A", "perm:B"])
st = fresh()
a.remember_call(st, pre("A", "ls"), now)
approve(st, "ls", now)
a.add_asking(st, "notif:permission", now + 6, 0)
a.complete_call(st, post("Z"))
check("notification survives unrelated completion", ids(st), ["notif:permission", "perm:A"])
a.complete_call(st, post("A"))
check("notification goes with the last approval", ids(st), [])
st = fresh()
a.add_asking(st, "q:q1", now, 0)
a.complete_call(st, {"tool_use_id": "q1"})
check("question resolves by id", ids(st), [])
st = {"kind": "claude", "asking": "perm:A@1/5", "calls": "A=abc"}
a.complete_call(st, {"tool_name": "Bash"})
check("completion without id retires nothing", ids(st), ["perm:A"])
st = {"calls": ""}
for i in range(70):
    a.remember_call(st, pre(f"c{i}", f"cmd{i}"), now)
check("remembered calls capped", len(a.parse_calls(st["calls"])), a.MAX_CALLS)

# render: debounce and priorities
st = {"kind": "claude", "session": "s", "busy": "1", "asking": "perm:A@%d/5" % int(now)}
check("permission within debounce renders busy", a.compute_render(st, now), "B")
check("permission past debounce renders asking", a.compute_render(st, now + 6), "Q")
st = {"kind": "claude", "busy": "0", "finished_at": str(int(now) - 10), "seen": "0"}
check("finished, settled, unseen", a.compute_render(st, now), "D")
st["seen"] = "1"
check("finished and seen", a.compute_render(st, now), "I")
st = {"kind": "claude", "busy": "0", "bg": "1"}
check("background work outstanding", a.compute_render(st, now), "B")

print("\nFAILED" if failures else "\nALL APPROVAL TESTS PASSED", f"({failures} failures)" if failures else "")
sys.exit(1 if failures else 0)
