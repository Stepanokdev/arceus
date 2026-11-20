"""A stand-in for the Bulava engine's command line, following engine/SERVER.md.

Installed as bin/dispatch.sh, bin/worker-send.sh and bin/night-shift.sh. Keeps its
state in $SUPERVISOR_STATE_DIR/fake-engine.json and logs every call with the
environment it saw, so tests can check what the runner handed to the engine.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

STATE = Path(os.environ["SUPERVISOR_STATE_DIR"]) / "fake-engine.json"


def load() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"tasks": {}, "calls": [], "next_rc": {}}


def save(s: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=1))


def now() -> str:
    return datetime.now(UTC).isoformat()


def session_of(folder: str) -> str:
    """The engine's tmux session for a folder (supervisor-lib.sh: session_name of slug_for)."""
    real = os.path.realpath(folder) if os.path.isdir(folder) else folder
    base = re.sub(r"-+", "-", re.sub(r"[^A-Za-z0-9]", "-", os.path.basename(real))).strip("-") or "proj"
    return f"night-{base}-{hashlib.sha1(real.encode()).hexdigest()[:12]}"


def open_session(s: dict, folder: str) -> None:
    sessions = s.setdefault("sessions", [])
    if session_of(folder) not in sessions:
        sessions.append(session_of(folder))


def tmux(s: dict, argv: list[str]) -> int:
    """Installed as tmux too: the engine's tmux server, as far as the runner asks it. Sessions are opened by a
    dispatch or a follow-up and stay, as the engine leaves them, until killed. `tmux_fails` makes it answer as a
    tmux that cannot be asked."""
    if argv[:1] == ["-L"]:
        argv = argv[2:]
    sessions = s.setdefault("sessions", [])
    if s.get("tmux_fails"):
        print("lost server", file=sys.stderr)
        return 1
    name = argv[argv.index("-t") + 1].lstrip("=") if "-t" in argv else ""
    if argv[:1] == ["list-sessions"]:
        if not sessions:
            print("no server running on /tmp/tmux-fake/bulava", file=sys.stderr)
            return 1
        made = s.setdefault("created", {})
        fmt = argv[argv.index("-F") + 1] if "-F" in argv else "#{session_name}"
        for name in sessions:
            made.setdefault(name, int(datetime.now(UTC).timestamp()))  # when it was first seen, as tmux keeps it
            print(fmt.replace("#{session_name}", name).replace("#{session_created}", str(made[name])))
        return 0
    if argv[:1] == ["has-session"]:
        return 0 if name in sessions else 1
    if argv[:1] == ["kill-session"]:
        if name not in sessions:
            return 1
        if name not in s.get("unkillable", []):  # a session a test keeps alive whatever is asked
            sessions.remove(name)
            s.setdefault("created", {}).pop(name, None)
        return 0
    return 2


def flags(argv: list[str]) -> tuple[dict, list[str]]:
    out, rest, i = {}, [], 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            out[a[2:]] = argv[i + 1]
            i += 2
        else:
            rest.append(a)
            i += 1
    return out, rest


def emit(tier: str, data: dict, rc: int = 0) -> int:
    print(f"TIER={tier}")
    print(json.dumps(data))
    return rc


def main() -> int:
    cmd, argv = sys.argv[1], sys.argv[2:]
    s = load()
    s["calls"].append({"cmd": cmd, "argv": argv, "env": {k: v for k, v in os.environ.items()
                                                       if k.startswith(("SUPERVISOR_", "CLAUDE_", "NIGHT_SHIFT", "GIT", "YOUTRACK",
                                                                        "SLACK"))}})
    if cmd == "tmux":
        rc = tmux(s, argv)
        save(s)
        return rc
    sub = f"{cmd} {argv[0]}" if argv else cmd
    forced = s["next_rc"].pop(sub, None)
    if forced is None:
        forced = s["next_rc"].pop(cmd, None)
    if forced is not None:
        save(s)
        print("❌ forced by the test", file=sys.stderr)
        return forced

    if cmd == "dispatch":
        f, (repo, text) = flags(argv)
        tid = f["task-id"]
        t = s["tasks"].get(tid)
        if t:
            if f.get("message-id") in t["messages"]:
                save(s)
                return emit("duplicate", {"task_id": tid, "attempt": 1})
            save(s)
            print(f"❌ задача {tid} уже є", file=sys.stderr)
            return 2
        # engine/SERVER.md: `busy` (6) while another task in this folder is running. Off unless a test asks for it:
        # the runner's own tests often leave a task the runner is done with as the engine last showed it.
        if s.get("busy_by_folder") and any(x["project"] == repo and x["state"] == "running" and x["attempt_live"]
                                           for x in s["tasks"].values()):
            save(s)
            print("TIER=busy")
            return 6
        subprocess.run(["git", "-C", repo, "checkout", "-q", "-B", f["branch"], f["base"]], check=True)
        base = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        # Like the engine: every repository of the task with its own base, the project first.
        repos = [{"path": repo, "base_sha": base}]
        extra_file = os.environ.get("SUPERVISOR_EXTRA_DIRS_FILE")
        if extra_file and os.path.exists(extra_file):
            for line in open(extra_file).read().splitlines():
                if line.strip():
                    repos.append({"path": line, "base_sha": subprocess.run(
                        ["git", "-C", line, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()})
        s["tasks"][tid] = {
            "schema": 1, "task_id": tid, "state": "running", "state_reason": "", "attempt_live": True,
            "project": repo, "branch": f["branch"], "base_ref": f["base"], "base_sha": base, "objective": text,
            "author": f.get("author"), "repos": repos, "question": None, "receipt": None, "outcome": None,
            "attempts": [{"n": 1, "kind": "initial", "started_at": now(), "ended_at": None}],
            "messages": {f.get("message-id"): text},
        }
        open_session(s, repo)
        s["tasks"][tid]["session"] = session_of(repo)
        # Like dispatch.sh: the folder's instance names the task it started there.
        instance = STATE.parent / "instances" / session_of(repo).removeprefix("night-")
        instance.mkdir(parents=True, exist_ok=True)
        (instance / "task-id").write_text(tid + "\n")
        save(s)
        return emit("dispatched", {"task_id": tid, "dispatch_id": "d-1", "attempt": 1, "message_id": f.get("message-id")})

    if cmd == "worker-send":
        f, rest = flags(argv)
        tid = f["task-id"]
        text = rest[-1]
        t = s["tasks"].get(tid)
        if not t:
            save(s)
            return 2
        mid = f.get("message-id")
        if mid in t["messages"]:
            save(s)
            return emit("duplicate", {"task_id": tid}) if t["messages"][mid] == text else 4
        if t["state"] == "running" and t["attempt_live"]:
            save(s)
            print("TIER=busy")
            return 6
        if "answers" in f and (not t["question"] or t["question"].get("id") != f["answers"]):
            save(s)
            print("TIER=conflict")
            return 4
        t["messages"][mid] = text
        t["attempts"].append({"n": len(t["attempts"]) + 1, "kind": "continue", "started_at": now(), "ended_at": None})
        t.update(state="running", attempt_live=True, question=None)
        open_session(s, t["project"])
        save(s)
        return emit("dispatched", {"task_id": tid, "attempt": len(t["attempts"])})

    if cmd in ("claude-usage", "codex-usage"):
        save(s)
        return 0

    if cmd == "night-shift":
        if argv[:2] == ["task", "show"]:
            t = s["tasks"].get(argv[3])
            save(s)
            if not t:
                print(f"❌ задачі {argv[3]} немає", file=sys.stderr)
                return 1
            shown = {k: v for k, v in t.items() if k != "messages"}
            # Like the engine (tasks.sh task_busy): an attempt is live only while its folder's session is there.
            named = t.get("session") or (session_of(t["project"]) if "project" in t else None)  # kept with the attempt
            if shown.get("attempt_live") is True and named and named not in s.get("sessions", []):
                shown["attempt_live"] = False
            print(json.dumps(shown))
            return 0
        if argv[:1] == ["doctor"]:
            checks = s.get("doctor") or [{"name": "auth.claude", "status": "ok", "detail": "signed in"}]
            save(s)
            print(json.dumps({"ok": all(c["status"] != "fail" for c in checks), "schema": 1, "checks": checks}))
            return 1 if any(c["status"] == "fail" for c in checks) else 0
        if argv[:1] == ["stop"] and len(argv) > 1:
            # Like the engine: the folder's run is no longer live in its record (its tmux session is another matter,
            # engine.stop_worker).
            for t in s["tasks"].values():
                if t.get("project") == argv[1]:
                    t["attempt_live"] = False
        save(s)
        return 0  # trust, mcp-decide, stop
    return 2


if __name__ == "__main__":
    sys.exit(main())
