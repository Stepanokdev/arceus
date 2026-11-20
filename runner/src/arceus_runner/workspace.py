"""The shared workspace: every repository of the group and the home are one checkout, so one runner at a time works
in it. Who holds it (a ticket's task, a code review, a test run or an architecture review), whether a follow-up may
go ahead, and getting the repositories and the home ready for whoever does.

The rule "one at a time" lives here alone: a ticket's task, a `/continue`, a code review and a background run all
ask `Workspace.taken` before they switch a branch. A test run in a folder of its own (qa.workspace) switches nothing
here and is no holder: it works beside whoever holds the workspace. Nothing here changes a task's state; the callers
do."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from arceus_runner.config import Config
from arceus_runner.db import ACTIVE_STATES, Task, TaskStore
from arceus_runner.engine import Engine
from arceus_runner.events import EventLog
from arceus_runner.gitops import Git
from arceus_runner.integrations import CodeHost, Repo, usable_repos
from arceus_runner.texts import START_REFUSED, ProjectNotesError, home_files, project_notes

PREPARED_MARK = "arceus-prepared"
PREPARED_PATHS = "arceus-prepared-paths"  # in a home's .git: the checkouts below it trusted so (prepare_checkout)


class Holder(Protocol):
    """Whatever else can hold the workspace: code reviews (code_review.ReviewAgent), background runs
    (workspace_run.WorkspaceRun)."""

    def holding(self) -> Task | None: ...

    def waiting(self) -> bool: ...


class Workspace:
    def __init__(self, cfg: Config, store: TaskStore, events: EventLog, git: Git, engine: Engine, host: CodeHost,
                 now: Callable[[], datetime]):
        self.cfg = cfg
        self.store = store
        self.events = events
        self.git = git
        self.engine = engine
        self.host = host
        self.now = now
        self._waiting: dict[str, tuple] = {}  # task id → why its follow-up waits, as last written as followup_waits
        # Code reviews and background runs, in the order they are asked who holds the workspace (loop.Runner).
        self.holders: list[Holder] = []

    def others_waiting(self) -> bool:
        """A code review or a background run is queued for the workspace: those go before new tickets."""
        return any(h.waiting() for h in self.holders)

    def group(self) -> list[Repo]:
        return usable_repos(self.cfg, self.host.repos())

    def names(self, task: Task) -> list[str]:
        """The task's repositories, by path below the group (the starting one first for older tasks)."""
        return [n for n in [task.repo, *json.loads(task.extra_repos or "[]")] if n]

    def task_repos(self, task: Task, repos: list[Repo]) -> list[Repo]:
        """The task's repositories as they were when it was taken."""
        by_name = {r.relative_path: r for r in repos}
        return [by_name[n] for n in self.names(task) if n in by_name]

    def prepare(self, task: Task, repos: list[Repo]) -> str | None:
        """Clone or fetch every repository of the task, the task branch in each; the home ready.

        The folder the session starts in gets its branch from dispatch.sh itself (--branch, --base,
        --dirty fail). A reason is returned instead when the work cannot start.
        """
        mine = self.task_repos(task, repos)
        try:
            notes = self.task_notes(task)
        except ProjectNotesError as e:
            return str(e)
        started = self.now()
        self.events.emit("workspace_preparing", task_id=task.task_id, repos=len(mine))
        for repo in mine:
            self.ensure_repo(repo, self.cfg.paths.workspace / repo.relative_path)
        extras = [r for r in mine if r.relative_path != task.repo]
        dirty = [r.relative_path for r in extras
                 if not self.git.is_clean(self.cfg.paths.workspace / r.relative_path)]
        if dirty:
            return f"{START_REFUSED[77]}: {', '.join(dirty)}"
        for repo in extras:
            self.git.start_branch(self.cfg.paths.workspace / repo.relative_path, task.branch,
                                  f"origin/{repo.default_branch or 'main'}")
        if not task.repo:
            self.prepare_home(repos, notes)
        self.write_extra_dirs(task)
        self.events.emit("workspace_ready", task_id=task.task_id, repos=len(mine),
                         seconds=round((self.now() - started).total_seconds(), 1))
        return None

    def notes_file(self, task: Task) -> Path:
        return self.cfg.paths.db.parent / "tasks" / task.task_id / "project-notes.md"

    def task_notes(self, task: Task) -> str:
        """The project's notes this task works with. Read from paths.project_notes when the task first starts
        and kept with its other files, so a restart, a `/continue` or a code review of it gets the same ones
        whatever the file says by then, or if it is gone."""
        f = self.notes_file(task)
        if f.exists():
            return f.read_text(encoding="utf-8")
        notes = project_notes(self.cfg.paths.project_notes)
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(notes, encoding="utf-8")
        tmp.replace(f)
        self.events.emit("project_notes_pinned", task_id=task.task_id, bytes=len(notes.encode()),
                         sha256=hashlib.sha256(notes.encode()).hexdigest())
        return notes

    def prepare_home(self, repos: list[Repo], notes: str, home: Path | None = None, reading: bool = False) -> None:
        """The workspace folder as the git project Bulava runs in, on main with these notes: the task's own
        (task_notes). The branch of whatever runs next starts from this commit. `home`: a folder of a run's own
        (a test run's beside a ticket, workspace_run.py) made so in the same way, its instructions to read the code
        there and change nothing (`reading`)."""
        home = home or self.cfg.paths.workspace
        h = self.host
        files = home_files(f"{h.label} {h.scope_kind} `{h.scope}`", repos, home.resolve(), notes, reading=reading)
        if self.git.write_home(home, files, f"Arceus: workspace of {h.label} {h.scope_kind} {h.scope}"):
            self.events.emit("workspace_home_updated", path=str(home), repos=len(repos),
                             project_notes=len(notes.encode()), commit=self.git.head(home))
        if not (home / ".git" / PREPARED_MARK).exists():
            self.engine.prepare_repo(home)
            (home / ".git" / PREPARED_MARK).write_text("trust, mcp-decide skip\n")

    def prepare_checkout(self, home: Path, path: Path) -> None:
        """A checkout below a home of its own (Git.checkout_at) trusted for Claude and its MCP servers decided, once
        per path. Its `.git` is a file, so the home keeps the list of paths done; the path stays the same from run to
        run, so a later run needs nothing."""
        mark = home / ".git" / PREPARED_PATHS
        done = set(mark.read_text().splitlines()) if mark.exists() else set()
        if str(path) in done:
            return
        self.engine.prepare_repo(path)
        with mark.open("a") as f:
            f.write(f"{path}\n")

    def extra_dirs_file(self, task: Task) -> Path | None:
        extras = json.loads(task.extra_repos or "[]")
        if not extras:
            return None
        return self.cfg.paths.db.parent / "tasks" / task.task_id / "extra-dirs.txt"

    def write_extra_dirs(self, task: Task) -> None:
        f = self.extra_dirs_file(task)
        if not f:
            return
        f.parent.mkdir(parents=True, exist_ok=True)
        lines = "".join(f"{self.cfg.paths.workspace / r}\n" for r in json.loads(task.extra_repos))
        f.write_text(lines)
        # All of them are the agent's to change (engine/SERVER.md: listed beside the file).
        (f.parent / "writable-directories.txt").write_text(lines)

    def taken(self, task: Task | None = None, *, states: tuple[str, ...] = ACTIVE_STATES) -> Task | None:
        """The task working in the shared workspace now, other than `task`; None when it is free.

        Every repository of the group and the home are one checkout, so whatever switches a branch
        there waits for it. A task's own resume or nudge does not wait for itself; it does wait for a
        code review of its own ticket (code_review.py), which has the workspace switched to the MRs.
        `states`: which states of a ticket's task count as holding it.
        """
        for holder in self.holders:
            held = holder.holding()
            if held is not None:
                return held
        return next((t for t in self.store.tasks(*states) if task is None or t.task_id != task.task_id), None)

    def waits(self, task: Task) -> bool:
        """True (and noted once) while another task holds the workspace: `task`'s follow-up keeps for a later tick."""
        holder = self.taken(task)
        if holder is None:
            return False
        self.note_waiting(task, "workspace_taken", holder=holder.task_id)
        return True

    def note_waiting(self, task: Task, reason: str, **fields) -> None:
        """A `followup_waits` line when the reason changes; nothing on a tick that waits for the same thing."""
        seen = (reason, *sorted((k, str(v)) for k, v in fields.items()))
        if self._waiting.get(task.task_id) == seen:
            return
        self._waiting[task.task_id] = seen
        self.events.emit("followup_waits", task_id=task.task_id, reason=reason, **fields)

    def followup_ready(self, task: Task) -> bool:
        """Before a follow-up: every repository of the task on the task branch, caught up with the code host.

        The workspace is shared, so another task may have switched a repository to its own branch
        in between; and people may have pushed to the MR branch. Both are put right here; nothing
        local is rewritten (catch_up only fast-forwards). A repository in the middle of a rebase or a
        cherry-pick is the agent's own unfinished work (a session cut off by a crash): it is left as it is, for
        the agent to finish.

        False, with nothing touched, while the follow-up has to wait: another task works in the
        workspace, or a repository that has to change branch holds uncommitted work (a checkout
        would carry it into this task's branch). The caller sends nothing and tries on a later tick.
        """
        if self.waits(task):
            return False
        home = self.cfg.paths.workspace
        home_moves = (not task.repo and (home / ".git").exists() and self.git.branch_exists(home, task.branch)
                      and self.git.current_branch(home) != task.branch)
        repos = []  # (name, path, has to change branch)
        for name in self.names(task):
            path = self.cfg.paths.workspace / name
            if not (path / ".git").exists() or not self.git.branch_exists(path, task.branch):
                continue
            if self.git.unfinished(path):
                continue
            repos.append((name, path, self.git.current_branch(path) != task.branch))
        # All checked before anything is switched: a follow-up never starts in a half-switched workspace.
        dirty = (["."] if home_moves and not self.git.is_clean(home) else []) + \
            [name for name, path, moves in repos if moves and not self.git.is_clean(path)]
        if dirty:
            self.note_waiting(task, "uncommitted_changes", repos=dirty)
            return False
        self._waiting.pop(task.task_id, None)
        if home_moves:
            self.git.home_on(home, task.branch)  # another task started from main in between
        for name, path, moves in repos:
            if moves:
                self.git.start_branch(path, task.branch, "HEAD")
            if self.store.task_mrs(task.task_id) and self.git.is_clean(path):
                moved = self.git.catch_up(path, task.branch)
                if moved in ("forwarded", "diverged"):  # "same" and "ahead": nothing came from the code host
                    self.events.emit("branch_caught_up", task_id=task.task_id, repo=name, result=moved)
        self.write_extra_dirs(task)
        return True

    def ensure_repo(self, repo: Repo, path: Path) -> None:
        if not (path / ".git").exists():
            self.git.clone(repo.http_url, path)
            self.events.emit("repo_cloned", repo=repo.relative_path)
        fixed = self.git.fix_modes(path)
        if fixed:
            self.events.emit("workspace_modes_fixed", repo=repo.relative_path, files=fixed)
        if not (path / ".git" / PREPARED_MARK).exists():
            self.git.configure(path)
            self.engine.prepare_repo(path)
            (path / ".git" / PREPARED_MARK).write_text("trust, mcp-decide skip\n")
        self.git.fetch(path)


@dataclass(frozen=True)
class AgentContext:
    """What a background agent (workspace_run.WorkspaceRun) works with: the runner's services and the workspace,
    nothing of a ticket's delivery."""
    cfg: Config
    store: Any  # db.ReviewStore: the runs and their findings
    events: EventLog
    now: Callable[[], datetime]
    engine: Engine
    git: Git
    host: CodeHost
    workspace: Workspace
    gate: Any = None  # screening.Gate: people's notes are screened before an agent reads them; None, none runs
