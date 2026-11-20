"""The Bulava engine's command line, as engine/SERVER.md defines it.

Bulava's processes get their own environment: the engine settings plus
bulava.env (the Claude token). The runner's tracker, code host and Slack tokens
are never in it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from arceus_runner.config import Config, read_env_file
from arceus_runner.gitops import _workspace_umask
from arceus_runner.screening import Clearance, NotScreened

TIMEOUT = 900
# What bulava.env may give the engine's commands: the models' credentials and the way out through a proxy. Nothing
# that changes how a program starts or which program runs (BASH_ENV, LD_PRELOAD, PATH, NODE_OPTIONS, ...).
TOKEN_VARS = re.compile(r"^(CLAUDE_CODE_OAUTH_TOKEN(_ISSUED_AT)?|ANTHROPIC_API_KEY|OPENAI_API_KEY|CODEX_API_KEY|"
                        r"(HTTPS?|NO|ALL)_PROXY|(https?|no|all)_proxy)$")


class EngineError(Exception):
    pass


@dataclass(frozen=True)
class Reply:
    rc: int
    tier: str | None  # dispatched, duplicate, busy, conflict
    data: dict = field(default_factory=dict)
    stderr: str = ""

    @property
    def accepted(self) -> bool:
        return self.rc == 0 and self.tier in ("dispatched", "duplicate")

    def why(self) -> str:
        last = [line for line in self.stderr.strip().splitlines() if line.strip()]
        return f"exit {self.rc}" + (f": {last[-1][:300]}" if last else "")


@dataclass(frozen=True)
class ReportReview:
    """How the engine's Codex reviews one task's closing report (engine/SERVER.md, "Review budget of a task"): at
    most `rounds` rounds, judged by the `criteria` file instead of the server's rule about data changes, and at the
    limit either accepted with the reviewer's notes (`accept`) or handed to a person (`ask`). The engine keeps it with
    the task, so a later attempt (`worker-send --relation continue`) is reviewed the same way; a task sent without
    it gets the engine's defaults."""
    rounds: int
    criteria: Path
    at_limit: str = "accept"

    def args(self) -> list[str]:
        return ["--report-max-rounds", str(self.rounds), "--report-criteria", str(self.criteria),
                "--report-at-limit", self.at_limit]


def _require(clearance: Clearance, text: str, task_id: str) -> None:
    """Nothing reaches the agent that the security screen did not clear, or that is not the runner's own."""
    if not isinstance(clearance, Clearance) or not clearance.covers(text):
        raise NotScreened(f"{task_id}: the text for the agent has no security clearance")


def google_key(mcp_config: Path | None) -> Path | None:
    """The service account key of the agent's `google` MCP server (setup.sh puts it into mcp.json), None when the
    agent has no Google."""
    try:
        env = json.loads(mcp_config.read_text())["mcpServers"]["google"]["env"]
        return Path(env["GOOGLE_SERVICE_ACCOUNT_KEY_FILE"])
    except (AttributeError, OSError, ValueError, KeyError, TypeError):
        return None


def parse_reply(rc: int, stdout: str, stderr: str) -> Reply:
    tier = None
    data: dict = {}
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("TIER="):
            tier = line.removeprefix("TIER=")
        elif line.startswith("{"):
            try:
                data = json.loads(line)
            except ValueError:
                pass
    return Reply(rc, tier, data, stderr)


# engine/SERVER.md, "States": every state `task show` gives a task.
TASK_STATES = ("queued", "running", "waiting_review", "done", "needs_input", "blocked", "failed")


def _malformed(record) -> str:
    """Why a `task show` record cannot be read as engine/SERVER.md gives it, or ""."""
    if not isinstance(record, dict):
        return "the record is not an object"
    kinds = {"state": (str,), "state_reason": (str, type(None)), "attempt_live": (bool, type(None)),
             "attempts": (list, type(None)), "receipt": (dict, type(None)), "outcome": (dict, type(None)),
             "question": (dict, type(None)), "repos": (list, type(None))}
    if record.get("state") not in TASK_STATES:
        return f"its state {str(record.get('state'))[:40]!r} is none the engine names"
    if record["state"] in ("queued", "running") and not isinstance(record.get("attempt_live"), bool):
        return f"a {record['state']} record does not say whether its attempt is live"
    for key, allowed in kinds.items():
        if key in record and not isinstance(record[key], allowed):
            return f"its {key} is a {type(record[key]).__name__}"
    if any(not isinstance(a, dict) for a in record.get("attempts") or []):
        return "an attempt in it is not an object"
    return ""


class Engine:
    def __init__(self, cfg: Config, extra_env: dict[str, str] | None = None, events=None):
        self.cfg = cfg
        self.bin = cfg.paths.bulava / "bin"
        self.extra_env = extra_env or {}  # tests: a stub worker instead of Claude
        self.events = events  # an EventLog: dispatch and send can take minutes, and say so in events.jsonl
        # While it answers a reason, nothing new goes to the engine: the console is restarting it once its running
        # tasks end (console/hostops.py). Every caller already takes the busy answer as "try again later".
        self.held: Callable[[], str | None] = lambda: None
        # The runner's own words added to what a task is sent, after the screen: what cleanup removed while the task
        # waited (cleanup.release_note), so the agent makes it again instead of finding it gone.
        self.notes: Callable[[str], str | None] = lambda task_id: None

    def env(self) -> dict[str, str]:
        b = self.cfg.bulava
        home = os.environ.get("HOME", str(Path.home()))
        env = {
            "HOME": home,
            "USER": os.environ.get("USER", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PATH": f"/usr/local/bin:{home}/.local/bin:/usr/bin:/bin",
            "DISABLE_AUTOUPDATER": "1",
            "SUPERVISOR_PROFILE": "server",
            "SUPERVISOR_REPORT_LANGUAGE": b.report_language,
            "SUPERVISOR_CODEX_UNAVAILABLE": b.codex_unavailable,
            "SUPERVISOR_CODEX_UNAVAILABLE_AFTER_HOURS": str(b.codex_unavailable_after_hours),
            "SUPERVISOR_HANDSHAKE_WAIT": str(int(b.start_handshake_wait.total_seconds())),
            "SUPERVISOR_SERVER_EXTRA_SECRET_PATHS": " ".join(str(p) for p in self.secret_paths()),
        }
        if b.mcp_config:
            env["SUPERVISOR_AUTOMATION_MCP_CONFIG"] = str(b.mcp_config)
        if google_key(b.mcp_config):
            # A run with nobody there is refused an MCP server not named here, at its first call.
            env["SUPERVISOR_ALLOW_TOOLS"] = "google"
        env["SUPERVISOR_STATE_DIR"] = str(self.cfg.paths.bulava_state)
        # config.yaml is the only place the models are chosen: never ~/.config/night-shift/config.json,
        # which the engine would otherwise let outrank a choice equal to its own default.
        env["NIGHT_SHIFT_SETTINGS"] = "off"
        env.update(self.token_env())
        env.update(b.model_env())
        env.update(self.extra_env)
        return env

    def token_env(self) -> dict[str, str]:
        """bulava.env, if there is anything real in it. Without it Claude uses its own sign-in
        (~/.claude/.credentials.json). An empty or example value must never reach the engine:
        it would override a working sign-in with a broken token. Only the names it is for pass (TOKEN_VARS): a
        variable such as BASH_ENV or LD_PRELOAD in it would run code in every engine command."""
        path = self.cfg.paths.bulava_env
        if not path.exists():
            return {}
        return {k: v for k, v in read_env_file(path).items()
                if TOKEN_VARS.match(k) and v.strip() and "replace-me" not in v}

    def refresh_usage(self) -> None:
        """Take fresh subscription readings with the engine's own scripts (Bulava takes them only during
        runs, so an idle machine's readings go stale). Failures are left to doctor to report."""
        for script in ("claude-usage.sh", "codex-usage.sh"):
            try:
                self._run([str(self.bin / script)], timeout=60)
            except EngineError:
                pass

    def doctor(self) -> dict:
        """`night-shift doctor --server --json`: sign-ins, hooks, usage, browser (engine/SERVER.md)."""
        r = self._run([str(self.bin / "night-shift.sh"), "doctor", "--server", "--json"], timeout=120)
        try:
            return json.loads(r.stdout)
        except ValueError:
            raise EngineError(f"doctor --server: exit {r.returncode}, no JSON: {(r.stderr or r.stdout).strip()[-300:]}") from None

    def secret_paths(self) -> list[Path]:
        p = self.cfg.paths
        key = google_key(self.cfg.bulava.mcp_config)  # the Google MCP server reads it, the agent's tools never
        return [p.secrets, p.bulava_env, p.db, p.db.with_suffix(".lock"), *([key.parent] if key else [])]

    def _run(self, args: list[str], cwd: Path | None = None, timeout: int = TIMEOUT,
             extra_dirs: Path | None = None) -> subprocess.CompletedProcess:
        env = self.env()
        if extra_dirs:
            # The other repositories of the task, one path per line, writable-directories.txt beside it.
            env["SUPERVISOR_EXTRA_DIRS_FILE"] = str(extra_dirs)
        try:
            # The engine starts the agent's sessions and its stack from here: the project's ordinary
            # umask, not the runner's private one (see gitops.WORKSPACE_UMASK).
            return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL, preexec_fn=_workspace_umask)
        except subprocess.TimeoutExpired:
            raise EngineError(f"{Path(args[0]).name}: timed out after {timeout}s") from None
        except OSError as e:
            raise EngineError(f"{Path(args[0]).name}: {e}") from None

    def dispatch(self, *, task_id: str, message_id: str, author: str, branch: str, base: str, repo: Path,
                 text: str, clearance: Clearance, source: str = "tracker", extra_dirs: Path | None = None,
                 report_review: ReportReview | None = None) -> Reply:
        _require(clearance, text, task_id)
        text = self._noted(task_id, text)
        return self._call("dispatch", task_id, message_id, [
            str(self.bin / "dispatch.sh"), "--task-id", task_id, "--message-id", message_id, "--source", source,
            "--author", author, "--branch", branch, "--base", base, "--dirty", "fail",
            *(report_review.args() if report_review else ()), str(repo), text], extra_dirs)

    def _noted(self, task_id: str, text: str) -> str:
        note = self.notes(task_id)
        return f"{text}\n\n{note}" if note else text

    def send(self, *, task_id: str, message_id: str, source: str, author: str, repo: Path, text: str,
             clearance: Clearance, answers: str | None = None, extra_dirs: Path | None = None) -> Reply:
        _require(clearance, text, task_id)
        text = self._noted(task_id, text)
        args = [str(self.bin / "worker-send.sh"), "--task-id", task_id, "--relation", "continue",
                "--message-id", message_id, "--source", source, "--author", author]
        if answers:
            args += ["--answers", answers]
        return self._call("send", task_id, message_id, [*args, str(repo), "-", "-", "-", text], extra_dirs)

    def _call(self, call: str, task_id: str, message_id: str, args: list[str], extra_dirs: Path | None) -> Reply:
        """dispatch.sh or worker-send.sh, bracketed by events: the runner's loop waits for it (up to TIMEOUT),
        and without them that wait looks like nothing happening."""
        emit = self.events.emit if self.events else lambda *a, **k: None
        held = self.held()
        if held:
            emit("engine_call_held", call=call, task_id=task_id, reason=held)
            return Reply(6, "busy", {}, f"held: {held}")
        emit("engine_call_started", call=call, task_id=task_id, message_id=message_id)
        started = time.monotonic()
        try:
            r = self._run(args, extra_dirs=extra_dirs)
        except EngineError as e:
            emit("engine_call_finished", call=call, task_id=task_id, result="error", reason=str(e),
                 seconds=round(time.monotonic() - started, 1))
            raise
        reply = parse_reply(r.returncode, r.stdout, r.stderr)
        emit("engine_call_finished", call=call, task_id=task_id, result=reply.tier or f"exit {reply.rc}",
             seconds=round(time.monotonic() - started, 1))
        return reply

    def show(self, task_id: str) -> dict | None:
        """The task record, or None if the engine does not know the task."""
        r = self._run([str(self.bin / "night-shift.sh"), "task", "show", "--task-id", task_id, "--json"], timeout=60)
        if r.returncode != 0:
            # The engine's own words for an unknown id (night-shift.sh: «задачі X немає», exit 1).
            # Anything else is a failure to retry, never a reason to treat the task as gone.
            if r.returncode == 1 and f"задачі {task_id} немає" in r.stderr:
                return None
            raise EngineError(f"task show {task_id}: exit {r.returncode}: {r.stderr.strip()[-300:]}")
        try:
            record = json.loads(r.stdout)
        except ValueError:
            raise EngineError(f"task show {task_id}: not JSON: {r.stdout[:200]}") from None
        why = _malformed(record)
        if why:
            # Nothing is decided from a record of this shape (resumed, settled, put back): try again later.
            raise EngineError(f"task show {task_id}: {why}")
        return record

    def prepare_repo(self, repo: Path) -> None:
        """Once per repository: trust it for Claude and decide its MCP servers (skip)."""
        for args in (["trust", str(repo)], ["mcp-decide", str(repo), "skip"]):
            r = self._run([str(self.bin / "night-shift.sh"), *args], timeout=120)
            if r.returncode != 0:
                raise EngineError(f"night-shift {args[0]} {repo}: exit {r.returncode}: {r.stderr.strip()[-300:]}")

    def stop(self, repo: Path) -> None:
        self._run([str(self.bin / "night-shift.sh"), "stop", str(repo)], timeout=120)

    # `night-shift stop <folder>` ends the folder's run in the engine's record (task show: attempt_live false) but
    # leaves the worker's tmux session, `night-<slug>`, and whatever runs in it, alive (night-shift.sh stop_instance:
    # «сесія залишена»). Before the runner takes a folder's files away from under a worker, it ends that session
    # itself and checks it is gone.

    @staticmethod
    def folder_session(repo: Path) -> str:
        """The tmux session of whatever worker the engine runs in `repo`: one folder, one session, named from the
        folder alone (engine/bin/supervisor-lib.sh: session_name of slug_for, the folder's name and the first 12 hex
        digits of the SHA-1 of its real path). Known without the engine's record of any task, so a record that is
        gone or cannot be read never leaves the runner guessing which worker to wait for."""
        real = os.path.realpath(repo) if os.path.isdir(repo) else str(repo)  # canon_path: `cd && pwd -P`, else as is
        base = re.sub(r"-+", "-", re.sub(r"[^A-Za-z0-9]", "-", os.path.basename(real))).strip("-") or "proj"
        return f"night-{base}-{hashlib.sha1(real.encode()).hexdigest()[:12]}"

    def folder_task(self, repo: Path) -> str | None:
        """The task the engine last started in `repo` (dispatch.sh writes its id into the folder's instance, as
        `task-id`, after it has decided the folder is not busy), or None when it names none. A session in the folder
        is a task's only if this names it."""
        slug = self.folder_session(repo).removeprefix("night-")
        try:
            return (self.cfg.paths.bulava_state / "instances" / slug / "task-id").read_text().strip() or None
        except FileNotFoundError:
            return None
        except OSError as e:
            raise EngineError(f"the engine's record of the task in {repo} cannot be read ({e})") from None

    def _tmux_args(self) -> tuple[dict[str, str], list[str]]:
        """tmux as the engine calls it: on a server its workers live in a tmux server of the engine's own, `-L
        bulava` unless SUPERVISOR_TMUX_SOCKET names another (engine/supervisor/config.sh). Asked anywhere else, tmux
        would say there is no such session while the worker runs."""
        env = self.env()
        sock = env.get("SUPERVISOR_TMUX_SOCKET") or ("bulava" if env.get("SUPERVISOR_PROFILE") == "server" else "")
        return env, ["tmux", *(["-L", sock] if sock else [])]

    def _tmux(self, *args: str) -> int:
        env, tmux = self._tmux_args()
        try:
            # Never a pipe: a tmux that starts its server would hold it open, and the wait would never end.
            return subprocess.run([*tmux, *args], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=60).returncode
        except FileNotFoundError:
            raise EngineError("tmux is not on PATH: whether the worker's session is gone cannot be told") from None
        except subprocess.TimeoutExpired:
            raise EngineError("tmux: timed out") from None

    def session_alive(self, session: str) -> bool:
        """Whether the tmux server has that session. "No" only when tmux says so: it lists its sessions and this is
        not among them, or no server runs at all. Any other failure is an EngineError, never taken for "gone"."""
        env, tmux = self._tmux_args()
        try:
            r = subprocess.run([*tmux, "list-sessions", "-F", "#{session_name}"], env=env, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            # Without tmux nothing tells whether the worker is gone; never taken for "gone".
            raise EngineError("tmux is not on PATH: whether the worker's session is gone cannot be told") from None
        except subprocess.TimeoutExpired:
            raise EngineError("tmux: timed out") from None
        if r.returncode == 0:
            return session in r.stdout.splitlines()
        why = r.stderr.strip()
        if "no server running" in why or ("error connecting to" in why and "No such file or directory" in why):
            return False
        raise EngineError(f"tmux list-sessions: exit {r.returncode}: {why[-300:]}")

    def stop_worker(self, repo: Path, session: str | None = None) -> bool:
        """The run in `repo` stopped in the engine and its worker's session (`folder_session`) ended. True once that
        session is gone; an engine that refuses the stop, or a tmux that cannot say, is an EngineError, to try
        again."""
        r = self._run([str(self.bin / "night-shift.sh"), "stop", str(repo)], timeout=120)
        if r.returncode != 0:
            raise EngineError(f"night-shift stop {repo}: exit {r.returncode}: {r.stderr.strip()[-300:]}")
        return self.end_session(repo, session)

    def end_session(self, repo: Path, session: str | None = None) -> bool:
        """The worker's session of `repo` ended, once its attempt is over in the engine's record (the engine lets it
        go itself a few seconds after; this does not wait for that). True once tmux no longer has it. `session`: its
        name as recorded when the run was dispatched, which holds even if the folder has gone since."""
        session = session or self.folder_session(repo)
        if self.session_alive(session):
            self._tmux("kill-session", "-t", f"={session}")
        return not self.session_alive(session)
