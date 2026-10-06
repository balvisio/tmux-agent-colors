#!/usr/bin/env python3
"""Codex background-job tracker: envelope parsing, registration, retirement, completeness."""
import json
import sys
import tempfile
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


def env(output, sid=None, exit_code=None):
    e = {"chunk_id": "ab", "wall_time_seconds": 1.0, "original_token_count": 0, "output": output}
    if sid is not None:
        e["session_id"] = sid
    if exit_code is not None:
        e["exit_code"] = exit_code
    return e


# --- envelope recognition -----------------------------------------------------------------
running_example = json.dumps(env("", sid=424242))
st = {"kind": "codex"}
check("completed envelope quoting a running chunk registers nothing",
      a.register_jobs(st, env("example: " + running_example, exit_code=0), now)[0], [])
st = {"kind": "codex"}
check("wrapped completed envelope registers nothing",
      a.register_jobs(st, [{"type": "input_text", "text": json.dumps({"i": 0, "status": "fulfilled",
                                                                      "value": env("printed " + running_example, exit_code=0)})}], now)[0], [])
st = {"kind": "codex"}
check("wrapped running envelope registers",
      a.register_jobs(st, [{"type": "input_text", "text": json.dumps({"id": "x", "status": "fulfilled", "value": env("", sid=32368)})}], now)[0], ["32368"])
a.register_jobs(st, {"result": {"status": "fulfilled", "value": env("done", sid=32368, exit_code=0)}}, now)
check("later envelope with exit_code retires", st["jobs"], "")
st = {"kind": "codex"}
check("braces and quotes inside output", a.register_jobs(st, json.dumps(env('says {ready} "ok"', sid=7)), now)[0], ["7"])
st = {"kind": "codex"}
check("prose around the JSON", a.register_jobs(st, "Output follows\n" + json.dumps(env("x {y}", sid=5)) + "\nend", now)[0], ["5"])
st = {"kind": "codex"}
a.register_jobs(st, env("", sid=9, exit_code=None), now)
check("null exit_code registers", st["jobs"].startswith("9@"), True)
a.register_jobs(st, env("", sid=9, exit_code=True), now)
check("boolean exit_code does not retire", st["jobs"].startswith("9@"), True)
a.register_jobs(st, env("", sid=9, exit_code=0), now)
check("integer exit_code retires", st["jobs"], "")
st = {"kind": "codex"}
check("unrelated JSON mentioning session_id ignored", a.register_jobs(st, '{"session_id": 123, "hook_event_name": "Stop"}', now)[0], [])
check("terminal: completed", a.is_terminal_record({"status": "completed", "exit_code": 0}), True)
check("terminal: failed without code", a.is_terminal_record({"status": "failed", "exit_code": None}), True)
check("not terminal: running", a.is_terminal_record({"status": "running", "exit_code": None}), False)
check("not terminal: running with code", a.is_terminal_record({"status": "running", "exit_code": 0}), False)

# --- rollout scan, completion gate, process-tree fallback -----------------------------------
P = {1: (0, "t", "node /opt/homebrew/bin/codex"), 2: (1, "t", "/x/codex-darwin-arm64/bin/codex --config"),
     3: (2, "t", "/x/codex-darwin-arm64/bin/codex-code-mode-host"), 4: (2, "t", "/opt/homebrew/bin/bash -lc sleep 100"),
     5: (4, "t", "sleep 100"), 6: (2, "t", "bash -c python3 /x/agent_state.py codex-hook")}
C_RUNNING = {0: [1], 1: [2], 2: [3, 4, 6], 4: [5]}
C_IDLE = {0: [1], 1: [2], 2: [3, 6]}
check("tree: command children found", a.codex_command_children(1, P, C_RUNNING), [4])
check("tree: no command children", a.codex_command_children(1, P, C_IDLE), [])


def rec(payload):
    return json.dumps({"timestamp": "t", "type": "x", "payload": payload}) + "\n"


with tempfile.TemporaryDirectory() as td:
    roll = Path(td) / "rollout.jsonl"
    chunk = json.dumps(env("", sid=17527))
    roll.write_text(rec({"type": "task_started"})
                    + rec({"type": "custom_tool_call_output", "call_id": "c", "output": [{"type": "input_text", "text": chunk}]})
                    + rec({"type": "item_completed", "item": {"type": "CommandExecution", "id": "e", "process_id": "17527", "status": "running", "exit_code": None}}))
    st = {"kind": "codex", "busy": "0", "replied": "1", "transcript": str(roll)}
    a.scan_codex_jobs(st, "%T", roll, now, False, 1, P, C_RUNNING)
    check("launch registered, non-terminal record ignored", st["jobs"].startswith("17527@"), True)
    check("Stop with job outstanding does not finish", st.get("finished_at", ""), "")
    check("render while job outstanding", a.compute_render(st, now), "B")
    st["jobs"] = "17527@%d" % int(now - 60)
    a.scan_codex_jobs(st, "%T", roll, now, False, 1, P, C_RUNNING)
    check("old job kept while a command child runs", bool(st["jobs"]), True)
    a.scan_codex_jobs(st, "%T", roll, now, False, 1, P, C_IDLE)
    check("old job retired with no command children", st["jobs"], "")
    check("finished once the last job retired", bool(st.get("finished_at")), True)
    st = {"kind": "codex", "busy": "0", "replied": "1", "transcript": str(roll), "roff": st["roff"], "jobs": "999@%d" % int(now)}
    a.scan_codex_jobs(st, "%T", roll, now, False, 1, P, C_IDLE)
    check("young job kept during grace even with no children", bool(st["jobs"]), True)
    with roll.open("a") as f:
        f.write(rec({"type": "item_completed", "item": {"type": "CommandExecution", "id": "e", "process_id": "999", "status": "completed", "exit_code": 0}}))
    a.scan_codex_jobs(st, "%T", roll, now + 1, False, 1, P, C_RUNNING)
    check("terminal record retires", st["jobs"], "")
    with roll.open("a") as f:
        f.write(rec({"type": "custom_tool_call_output", "call_id": "d", "output": [{"type": "input_text", "text": json.dumps(env("", sid=4242))}]}))
    a.scan_codex_jobs(st, "%T", roll, now + 2, False, 1, P, C_RUNNING)
    check("new job after finish reverts the finish", st.get("finished_at", ""), "")

s = {"kind": "codex", "busy": "0", "replied": "1"}
a.maybe_finish(s, now, False)
check("gate: unknown completeness never finishes", s.get("finished_at", ""), "")
check("gate: unknown completeness renders P", a.compute_render(s, now), "P")
s = {"kind": "codex", "busy": "0", "replied": "1", "hooked": "1"}
a.maybe_finish(s, now, False)
check("gate: hooks alone are not enough", s.get("finished_at", ""), "")
s = {"kind": "codex", "busy": "0", "replied": "1", "roff": "10"}
a.maybe_finish(s, now, False)
check("gate: scanned rollout finishes", bool(s.get("finished_at")), True)
s = {"kind": "codex", "hooked": "1", "roff": "5", "jobs": "1@2", "busy": "0"}
a.reset_for_new_agent(s)
check("identity reset clears hooked/roff/jobs", (s["hooked"], s["roff"], s["jobs"]), ("", "", ""))

# --- real data, if this machine has kibitz pane mappings ------------------------------------
kib = Path.home() / ".cache" / "kibitz"
for mapping in sorted(kib.glob("pane-*.rollout")) if kib.exists() else []:
    roll = Path(mapping.read_text().strip())
    if not roll.exists():
        continue
    st = {"kind": "codex", "busy": "0", "transcript": ""}
    a.scan_codex_jobs(st, mapping.stem, roll, now, False, None, {}, {})
    check(f"real rollout {roll.name[-18:]} full scan leaves no phantom job", st["jobs"], "")

print("\nFAILED" if failures else "\nALL TRACKER TESTS PASSED", f"({failures} failures)" if failures else "")
sys.exit(1 if failures else 0)
