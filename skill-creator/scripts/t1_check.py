#!/usr/bin/env python3
"""Check that a T1 trigger run is valid before its score is used (references/updating.md, T1).

The official run_eval.py scores any call that did not invoke its stub as "not triggered",
including calls that timed out, crashed or errored. The logging shim records every call
(calls.log), its stream-json stdout (stdout.log) and its stderr (stderr.log). A call counts as
decided if its stream shows a tool_use (Claude chose some tool or skill) or a result without an
error. Decisions mirror run_eval.py: a tool_use other than Skill/Read decides at once; a Skill or
Read block only once it completes (content_block_stop or the full assistant message) or once its
streamed arguments contain the runner's complete stub name ("<skill>-skill-" + 8 hex chars, a
trigger; a truncated name is not enough), because the runner waits
for those arguments. Any error result makes the call undecided. A run is valid only if
the number of calls equals the expected queries x runs from the runner's results, every call was
decided, stderr holds nothing but known-benign warnings, and the trigger outcomes reconstructed
from the logs match the runner's per-query trigger counts, and every call ran with only the
Skill and Read tools and no MCP server (so no call could act on anything). The runner can drop the last chunk of
a stream when the process exits, so a mismatch is possible and makes the run inconclusive.
calls.log holds one line per call with the call's arguments (the query is among them).

Usage: python3 t1_check.py <disposable dir T> <run_eval results.json>   (exit 0 valid, 1 inconclusive)
       python3 t1_check.py --self-test
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

BENIGN_STDERR = ("no stdin data received",)  # claude -p notice when stdin is empty


def segments(lines: list[str]) -> list[list[dict]]:
    """Split the shared stdout log into one event list per claude call (each starts with init)."""
    segs: list[list[dict]] = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            segs.append([])
        if segs:
            segs[-1].append(event)
    return segs


def decided(seg: list[dict], skill_name: str) -> bool:
    """Mirror run_eval.py's decision points; a call is valid only if the runner could decide it."""
    if any(e.get("type") == "result" and e.get("is_error") for e in seg):
        return False
    stub = re.compile(re.escape(f"{skill_name}-skill-") + r"[0-9a-f]{8}")
    pending, args = False, ""  # a Skill/Read block has started; the runner reads its arguments
    for e in seg:
        kind = e.get("type")
        if kind == "stream_event":
            ev = e.get("event") or {}
            etype = ev.get("type", "")
            if etype == "content_block_start":
                block = ev.get("content_block") or {}
                if block.get("type") == "tool_use":
                    if block.get("name") not in ("Skill", "Read"):
                        return True  # runner: another tool chosen -> not triggered
                    pending, args = True, ""
            elif etype == "content_block_delta" and pending:
                delta = ev.get("delta") or {}
                if delta.get("type") == "input_json_delta":
                    args += delta.get("partial_json", "")
                    if stub.search(args):
                        return True  # runner: full stub name seen -> triggered
            elif etype in ("content_block_stop", "message_stop"):
                if pending or etype == "message_stop":
                    return True  # runner: block or message finished -> decided
        elif kind == "assistant":
            content = (e.get("message") or {}).get("content") or []
            if any(isinstance(c, dict) and c.get("type") == "tool_use" for c in content):
                return True  # runner: full-message fallback decides on the first tool_use
        elif kind == "result":
            return True
    return False


def triggered(seg: list[dict], skill_name: str) -> bool:
    """Whether the call selected the runner's stub (streamed Skill/Read args or full message)."""
    stub = re.compile(re.escape(f"{skill_name}-skill-") + r"[0-9a-f]{8}")
    args, pending = "", False
    for e in seg:
        if e.get("type") == "stream_event":
            ev = e.get("event") or {}
            block = ev.get("content_block") or {}
            if ev.get("type") == "content_block_start" and block.get("type") == "tool_use":
                pending, args = block.get("name") in ("Skill", "Read"), ""
            elif ev.get("type") == "content_block_delta" and pending:
                args += (ev.get("delta") or {}).get("partial_json", "")
                if stub.search(args):
                    return True
        elif e.get("type") == "assistant":
            for c in (e.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    inp = c.get("input") or {}
                    target = inp.get("skill", "") if c.get("name") == "Skill" else inp.get("file_path", "")
                    if c.get("name") in ("Skill", "Read") and stub.search(str(target)):
                        return True
    return False


def check(t: Path, results: Path) -> bool:
    read = lambda name: (t / name).read_text(errors="replace").splitlines() if (t / name).is_file() else []
    calls, segs = len(read("calls.log")), segments(read("stdout.log"))
    try:
        data = json.loads(results.read_text())
        expected = sum(int(r["runs"]) for r in data["results"])
        skill_name = str(data["skill_name"])
        queries = [(str(r["query"]), int(r["triggers"])) for r in data["results"]]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"INCONCLUSIVE: cannot read skill name / expected run count from {results}: {exc}")
        return False
    errors = [l for l in read("stderr.log") if l.strip() and not any(b in l for b in BENIGN_STDERR)]
    undecided = [i + 1 for i, s in enumerate(segs) if not decided(s, skill_name)]
    unrestricted = [i + 1 for i, s in enumerate(segs)
                    if not s or set(s[0].get("tools") or ["?"]) - {"Skill", "Read"} or s[0].get("mcp_servers")]
    print(f"expected={expected} calls={calls} streams={len(segs)} undecided={undecided or 'none'} "
          f"stderr_errors={len(errors)} unrestricted={unrestricted or 'none'}")
    for e in errors[:5]:
        print(f"  stderr: {e[:200]}")
    # map each logged call to its query (longest query text contained in the call's arguments)
    lines = read("calls.log")
    logged: dict[str, int] = {q: 0 for q, _ in queries}
    unmapped = 0
    for i, line in enumerate(lines):
        hits = [q for q, _ in queries if q and q.replace("\n", " ") in line]
        if not hits:
            unmapped += 1
        elif i < len(segs) and triggered(segs[i], skill_name):
            logged[max(hits, key=len)] += 1
    mismatched = [q[:60] for q, n in queries if logged.get(q, 0) != n]
    if unmapped or mismatched:
        print(f"  unmapped calls={unmapped}; runner vs log trigger counts differ for: {mismatched or 'none'}")
    ok = (expected > 0 and calls == len(segs) == expected and not undecided and not errors
          and not unmapped and not mismatched and not unrestricted)
    print("valid" if ok else "INCONCLUSIVE: do not score this T1 run")
    return ok


def self_test() -> None:
    ev = lambda **kw: json.dumps(kw)
    init = ev(type="system", subtype="init", tools=["Read", "Skill"], mcp_servers=[])
    start = lambda name: ev(type="stream_event", event={"type": "content_block_start",
                                                         "content_block": {"type": "tool_use", "name": name}})
    stop = ev(type="stream_event", event={"type": "content_block_stop"})
    delta = ev(type="stream_event", event={"type": "content_block_delta"})
    arg = lambda text: ev(type="stream_event", event={"type": "content_block_delta",
                                                      "delta": {"type": "input_json_delta", "partial_json": text}})
    ok_result, err_result = ev(type="result", is_error=False), ev(type="result", is_error=True)
    cases = [  # name, calls, expected runs, stdout events, stderr, expected verdict
        ("other tool decides at once", 2, 2, [init, start("Bash"), init, start("Bash")], "", True),
        ("Skill block completed", 1, 1, [init, start("Skill"), delta, stop], "", True),
        ("clean completion without tools", 1, 1, [init, delta, ok_result], "", True),
        ("text-only answer ends at message_stop", 1, 1, [init, delta, ev(type="stream_event", event={"type": "message_stop"})], "", True),
        ("full assistant message with tool_use", 1, 1, [init, ev(type="assistant", message={"content": [{"type": "tool_use", "name": "Bash"}]})], "", True),
        ("text block stop alone is not a decision", 1, 1, [init, delta, ev(type="stream_event", event={"type": "content_block_stop"})], "", False),
        ("benign stdin warning only", 1, 1, [init, ok_result], "Warning: no stdin data received in 3s\n", True),
        ("Skill start then timeout", 1, 1, [init, start("Skill"), delta], "", False),
        ("Skill args name the full stub", 1, 1, [init, start("Skill"), arg('{"skill": "demo-sk'), arg('ill-1a2b3c4d')], "", True),
        ("truncated stub name, then timeout", 1, 1, [init, start("Skill"), arg('{"skill": "demo-skill-1a2b')], "", False),
        ("Skill args name another skill, then timeout", 1, 1, [init, start("Skill"), arg('{"skill": "other"')], "", False),
        ("error result after a tool start", 1, 1, [init, start("Bash"), err_result], "", False),
        ("stream with no decision", 2, 2, [init, start("Bash"), init, delta], "", False),
        ("real stderr error", 1, 1, [init, start("Bash")], "Error: not logged in\n", False),
        ("runner lost a call before launch", 29, 30, [init, start("Bash")] * 29, "", False),
    ]
    for name, calls, runs, out, err, expect in cases:
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "calls.log").write_text("--settings x -p the query --verbose\n" * calls)
            (t / "stdout.log").write_text("\n".join(out) + "\n")
            (t / "stderr.log").write_text(err)
            trig = sum(1 for sg in segments(out) if triggered(sg, "demo"))
            (t / "results.json").write_text(json.dumps({"skill_name": "demo", "results": [
                {"query": "the query", "runs": runs, "triggers": trig}]}))
            assert check(t, t / "results.json") is expect, name

    # a call that had Bash or an MCP server available is inconclusive, whatever it decided
    for loose in (ev(type="system", subtype="init", tools=["Read", "Skill", "Bash"], mcp_servers=[]),
                  ev(type="system", subtype="init", tools=["Read", "Skill"], mcp_servers=[{"name": "x"}])):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "calls.log").write_text("-p q1\n")
            (t / "stdout.log").write_text("\n".join([loose, start("Bash")]) + "\n")
            (t / "stderr.log").write_text("")
            (t / "results.json").write_text(json.dumps({"skill_name": "demo", "results": [
                {"query": "q1", "runs": 1, "triggers": 0}]}))
            assert check(t, t / "results.json") is False

    # the runner recorded "not triggered" although the log shows a full stub selection -> inconclusive
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        (t / "calls.log").write_text("-p q1\n-p q2\n")
        (t / "stdout.log").write_text("\n".join([init, start("Skill"), arg('{"skill": "demo-skill-1a2b3c4d"}'),
                                                 init, start("Bash")]) + "\n")
        (t / "stderr.log").write_text("")
        (t / "results.json").write_text(json.dumps({"skill_name": "demo", "results": [
            {"query": "q1", "runs": 1, "triggers": 0}, {"query": "q2", "runs": 1, "triggers": 0}]}))
        assert check(t, t / "results.json") is False
        (t / "results.json").write_text(json.dumps({"skill_name": "demo", "results": [
            {"query": "q1", "runs": 1, "triggers": 1}, {"query": "q2", "runs": 1, "triggers": 0}]}))
        assert check(t, t / "results.json") is True
        (t / "calls.log").write_text("-p something else\n-p q2\n")  # a call that matches no query
        assert check(t, t / "results.json") is False
    print("self-test ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
        sys.exit(0)
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    sys.exit(0 if check(Path(sys.argv[1]), Path(sys.argv[2])) else 1)
