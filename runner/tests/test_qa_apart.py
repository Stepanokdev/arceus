"""A test run in a folder of its own (qa.workspace): it works beside a ticket, in both orders, and nothing of the
ticket's clone moves while it does."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest

from arceus_runner.cli import qa_check
from arceus_runner.config import Config
from arceus_runner.engine import Engine, EngineError
from arceus_runner.progress import task_key
from arceus_runner.workspace_run import FOLDER_MARK
from arceus_runner.console import runs as console_runs
from arceus_runner.console import views_tasks
from arceus_runner.progress import tool_target

from .test_delivery import example_raw, git, take
from .test_qa import (PNG, RID, WHOLE, accepted_with_notes, dispatches, finish, no_real_tools,  # noqa: F401 (autouse)
                      scenario, world)


def apart_world(tmp_path):
    w = world(tmp_path, workspace=str(tmp_path / "qa-workspace"))
    w.set_engine(lambda s: s.update(busy_by_folder=True))  # the engine's rule: one running task per folder
    return w


@pytest.fixture
def w(tmp_path):
    w = apart_world(tmp_path)
    yield w
    w.events.close()
    w.store.close()


def qa_root(w) -> Path:
    return w.cfg.qa.workspace.resolve()


def home(w) -> Path:
    """The engine's project for test runs: their evidence and report links."""
    return qa_root(w) / "home"


def checkout(w, repo: str = "acme-platform") -> Path:
    """A repository as a test run reads it: beside the engine's project, outside it."""
    return qa_root(w) / "repos" / repo


def out_dir(w, rid: str = RID) -> Path:
    return home(w) / ".arceus" / "qa" / rid


def queue(w, scenario: int = 1) -> None:
    w.store.add_run("qa", "command", "ops", scenario=scenario)
    w.tick()


def push_main(w, name: str = "acme-platform", file: str = "app/new.py") -> str:
    """A commit on the code host's main that no clone has fetched yet."""
    seed = w.tmp / f"push-{name}"
    if not seed.exists():
        subprocess.run(["git", "clone", "-q", str(w.origins[name]), str(seed)], check=True)
    git(seed, "pull", "-q", "origin", "main")
    (seed / file).parent.mkdir(parents=True, exist_ok=True)
    (seed / file).write_text("x = 1\n")
    git(seed, "add", file)
    git(seed, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", f"add {file}")
    git(seed, "push", "-q", "origin", "HEAD:main")
    return git(seed, "rev-parse", "HEAD")


def ticket_state(repo: Path) -> dict:
    """Everything of a clone a ticket's agent may be relying on."""
    fetch_head = repo / ".git" / "FETCH_HEAD"
    return {"branch": git(repo, "branch", "--show-current"), "head": git(repo, "rev-parse", "HEAD"),
            "status": git(repo, "status", "--porcelain", "--untracked-files=all"),
            "index": git(repo, "diff", "--cached"), "work": git(repo, "diff"),
            "origin_main": git(repo, "rev-parse", "refs/remotes/origin/main"),
            "branches": git(repo, "branch", "--list", "--format=%(refname)"),
            "stash": git(repo, "stash", "list"),
            "fetch_head": fetch_head.read_text() if fetch_head.exists() else None}


def dirty_ticket(w) -> None:
    """The ticket's agent mid-work: something staged, something changed, something new."""
    (w.repo / "staged.txt").write_text("staged\n")
    git(w.repo, "add", "staged.txt")
    (w.repo / "README.md").write_text("changed, not staged\n")
    (w.repo / "untracked.txt").write_text("new\n")


def unlock(path: Path) -> None:
    """What only a deliberate `chmod -R u+w` by the agent would do to its read-only checkout."""
    for d, dirs, files in os.walk(path):
        os.chmod(d, 0o755)
        for f in files:
            if not os.path.islink(os.path.join(d, f)):
                os.chmod(os.path.join(d, f), 0o644)


def denied(path: Path, text: str = "x\n") -> bool:
    try:
        path.write_text(text)
    except PermissionError:
        return True
    return False


def started(w) -> list[str]:
    return [c["argv"][c["argv"].index("--task-id") + 1] for c in w.calls("dispatch")]


# -- side by side ----------------------------------------------------------------------------------------------------

def test_a_test_run_starts_beside_a_ticket_at_work_and_leaves_its_clone_as_it_was(w):
    take(w)
    assert w.store.task("ACME-1").state == "dispatched"
    dirty_ticket(w)
    newer = push_main(w)  # main moved on the code host since the ticket fetched
    before = ticket_state(w.repo)
    queue(w)
    assert started(w) == ["ACME-1", RID]
    [call] = dispatches(w)
    argv = call["argv"]
    root = qa_root(w)
    assert argv[-2] == str(home(w)) and argv[argv.index("--branch") + 1] == "arceus-qa/1"
    co = checkout(w)
    # The run reads the code host's main as it is now, in a co of its own, detached, outside the engine's
    # project (where the engine lets the agent edit nothing) and not listed as writable.
    assert git(co, "rev-parse", "HEAD") == newer and git(co, "branch", "--show-current") == ""
    assert (co / "app" / "new.py").exists() and not (co / "untracked.txt").exists()
    assert git(co, "status", "--porcelain") == "" and not co.is_relative_to(home(w))
    extra = Path(call["env"]["SUPERVISOR_EXTRA_DIRS_FILE"])
    assert str(co) in extra.read_text().splitlines() and str(w.repo) not in extra.read_text()
    assert not (extra.parent / "writable-directories.txt").exists()
    text = argv[-1]
    assert f"checked out in {root / 'repos'}" in text and "to be read only" in text
    assert f"`acme-platform`: checked out at `{newer}` (main) in {co}." in text
    assert str(out_dir(w) / "findings.json") in text
    # Bulava trusts the project and each co.
    trusted = [c["argv"][1] for c in w.calls("night-shift") if c["argv"][:1] == ["trust"]]
    assert str(home(w)) in trusted and str(co) in trusted
    # The ticket's clone: same branch, commit, index, changes, refs, stash and FETCH_HEAD. Only the run's own ref.
    assert ticket_state(w.repo) == before
    assert git(w.repo, "rev-parse", "refs/arceus/qa-1") == newer
    # Nothing waits for anything: the ticket holds the workspace, the run works apart.
    assert w.runner.delivery.workspace.taken().task_id == "ACME-1"
    assert w.store.run(RID).folder == str(root) and w.store.run_holding() is None
    assert w.store.run_apart().id == RID


def test_a_ticket_starts_and_is_taken_up_again_while_a_test_run_works_apart(w):
    queue(w)
    assert w.store.run(RID).state == "running"
    assert w.runner.delivery.workspace.taken() is None
    take(w)
    assert started(w) == [RID, "ACME-1"] and w.store.task("ACME-1").state == "dispatched"
    # The ticket's session died: it is sent on while the test run still works.
    w.set_engine(lambda s: s["tasks"]["ACME-1"].update(attempt_live=False))
    w.tick()
    assert [c["argv"][c["argv"].index("--task-id") + 1] for c in w.calls("worker-send")] == ["ACME-1"]
    assert w.store.run(RID).state == "running"


def test_a_queued_test_run_does_not_hold_back_a_ticket(w):
    """Queued behind a test run under way, a second one waits for it; tickets do not wait for either."""
    n = scenario(w, "Payments", "Pay with the test card.\n", days=(), at=None)
    queue(w)
    w.store.add_run("qa", "command", "ops", scenario=n)
    assert not w.runner.delivery.workspace.others_waiting()
    take(w)
    assert started(w) == [RID, "ACME-1"]
    [waits] = [e for e in w.events_named("qa_waits") if e["task_id"] == "qa-2"]
    assert (waits["reason"], waits["holder"]) == ("run_under_way", RID)
    finish(w)  # the first one is over: the second starts beside the ticket
    w.tick()
    assert started(w) == [RID, "ACME-1", "qa-2"]


def test_nothing_new_starts_while_the_engine_is_to_be_restarted(w):
    w.store.set_state("drain", {"token": "t", "by": "ops"})
    queue(w)
    take(w)
    assert started(w) == []
    assert w.store.run(RID).state == "pending"


# -- stopping, crashing, putting back ----------------------------------------------------------------------------------

def stops(w) -> list[str]:
    return [c["argv"][1] for c in w.calls("night-shift") if c["argv"][:1] == ["stop"]]


def test_stopping_a_test_run_stops_its_folder_only(w):
    take(w)
    queue(w)
    w.store.update_run(RID, stop_requested=1)
    w.tick()
    assert stops(w) == [str(home(w))]
    assert w.store.run(RID).state == "done" and w.store.task("ACME-1").state == "dispatched"


def test_a_test_run_past_its_time_is_stopped_in_its_folder_only(w):
    take(w)
    queue(w)
    w.now += w.cfg.qa.timeout + timedelta(minutes=1)
    w.tick()
    assert stops(w) == [str(home(w))]
    assert w.store.task("ACME-1").state == "dispatched"


def test_a_finished_run_takes_its_checkouts_and_refs_away_and_keeps_its_report(w):
    take(w)
    dirty_ticket(w)
    before = ticket_state(w.repo)
    queue(w)
    co = checkout(w)
    assert denied(co / "scratch.txt")  # the checkout is read-only
    unlock(co)  # an agent that made it writable on purpose
    (co / "scratch.txt").write_text("the agent left this\n")
    out = out_dir(w)
    out.mkdir(parents=True, exist_ok=True)
    (out / "findings.json").write_text(json.dumps({
        "summary": "Checked sign-in.", "blocked": "", "findings": [],
        "tested": [{"what": "Sign-in", "result": "ok", "cases": [{"what": "customer", "result": "ok"}]}]}))
    finish(w)
    run = w.store.run(RID)
    assert run.state == "done" and (w.cfg.qa.reports / "1" / "report.html").exists()
    assert not co.exists() and not out.exists()
    assert git(w.repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert git(w.repo, "for-each-ref", "refs/arceus/") == ""
    assert ticket_state(w.repo) == before
    [dropped] = w.events_named("qa_leftovers_dropped")
    assert (dropped["repo"], dropped["files"]) == ("acme-platform", 1)
    # The folder stays, on main, with no branch of the run left: the next run starts from it as it is.
    assert git(home(w), "branch", "--show-current") == "main"
    assert git(home(w), "branch", "--list", "arceus-qa/*") == ""
    assert w.store.run_apart() is None and w.runner.delivery.workspace.taken().task_id == "ACME-1"


def test_a_dispatch_the_engine_did_not_take_is_sent_again_into_the_same_checkout(w):
    w.store.add_run("qa", "command", "ops", scenario=1)
    w.set_engine(lambda s: s["next_rc"].update({"dispatch": 6}))
    w.tick()
    run = w.store.run(RID)
    assert run.state == "preparing" and run.folder == str(qa_root(w))
    co = checkout(w)
    unlock(co / ".git")
    (co / ".git" / "kept-mark").write_text("the first checkout\n")  # a checkout made again would not have it
    os.chmod(co / ".git", 0o555)
    w.tick()  # taken up where it stood: the same folder, the same checkout, one engine task
    assert w.store.run(RID).state == "running"
    assert (co / ".git" / "kept-mark").exists()
    assert dispatches(w)[-1]["argv"][-2] == str(home(w))
    assert len(w.engine_state()["tasks"]) == 1


def test_a_checkout_left_by_a_run_that_was_never_put_back_is_replaced(w):
    root = qa_root(w)
    queue(w)
    co = checkout(w)
    old = git(co, "rev-parse", "HEAD")
    # The run's record is lost, as if the runner's database were restored from before it: its checkout stays, and
    # its worker is gone.
    w.store.update_run(RID, state="done")
    w.set_engine(lambda s: s["sessions"].remove(session(w)))
    unlock(co)
    (co / "junk.txt").write_text("left\n")
    newer = push_main(w)
    queue(w, scenario=1)
    assert git(co, "rev-parse", "HEAD") == newer != old
    assert not (co / "junk.txt").exists() and root in co.parents


def test_a_run_under_way_from_before_the_folder_keeps_holding_the_shared_workspace(w):
    """Upgrading: a run that was prepared in the shared workspace keeps it until it ends, and tickets wait for it."""
    w.store.add_run("qa", "command", "ops", scenario=1)
    w.store.update_run(RID, state="running", snapshot=json.dumps({"branch": "arceus-qa/1", "repos": []}))
    w.set_engine(lambda s: (s["tasks"].update({RID: {"task_id": RID, "state": "running", "attempt_live": True,
                                                     "project": str(w.cfg.paths.workspace), "attempts": []}}),
                            s.setdefault("sessions", []).append(session(w, w.cfg.paths.workspace))))
    assert w.store.run(RID).folder is None
    assert w.runner.delivery.workspace.taken().task_id == RID
    take(w)
    assert w.store.task("ACME-1") is None and w.store.run(RID).state == "running"


# -- what says what ----------------------------------------------------------------------------------------------------

def test_the_folder_must_be_beside_the_workspace(tmp_path):
    raw = example_raw()
    raw["paths"] = {**raw.get("paths", {}), "workspace": str(tmp_path / "ws")}
    for bad in (tmp_path / "ws", tmp_path / "ws" / "qa", tmp_path):
        raw["qa"] = {**raw.get("qa", {}), "workspace": str(bad)}
        with pytest.raises(ValueError, match="qa.workspace"):
            Config.model_validate(raw)
    raw["qa"]["workspace"] = str(tmp_path / "qa-workspace")
    assert Config.model_validate(raw).qa.workspace == tmp_path / "qa-workspace"


def test_the_console_says_what_waits_for_what(w):
    take(w)
    w.store.add_run("qa", "command", "ops", scenario=1)
    # A queued test run waits for nothing while a ticket works; a ticket queued meanwhile is not behind it.
    assert console_runs.waits_for(w.cfg, w.store, "qa") == ""
    assert views_tasks._ahead(w.store, w.cfg) == ""
    w.tick()
    n = scenario(w, "Payments", "Pay with the test card.\n", days=(), at=None)
    w.store.add_run("qa", "command", "ops", scenario=n)
    assert console_runs.waits_for(w.cfg, w.store, "qa") == "Platform test, run 1"


def test_progress_names_a_file_below_the_test_runs_folder_too(tmp_path):
    ws, qa = tmp_path / "ws", tmp_path / "qa"
    assert tool_target("Read", {"file_path": str(qa / "api" / "urls.py")}, (ws, qa)) == "api/urls.py"
    assert tool_target("Read", {"file_path": str(ws / "web" / "a.ts")}, (ws, qa)) == "web/a.ts"
    assert tool_target("Read", {"file_path": "/etc/hosts"}, (ws, qa)) == "hosts"




# -- preparing: what is chosen stays chosen ---------------------------------------------------------------------------

def refs(w, repo: Path) -> str:
    if not (repo / ".git").exists():
        return ""
    return git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/arceus/")


def theme_unreachable(w) -> tuple[Path, Path]:
    """The theme cloned once, then its code host stops answering: the fetch, not the clone, fails."""
    theme = w.cfg.paths.workspace / "acme-theme"
    if not theme.exists():
        subprocess.run(["git", "clone", "-q", str(w.origins["acme-theme"]), str(theme)], check=True)
    origin = w.origins["acme-theme"]
    hidden = origin.with_name("hidden.git")
    origin.rename(hidden)
    return origin, hidden


def test_a_preparation_cut_short_goes_on_with_the_commits_it_had_chosen(w):
    theme_origin, hidden = theme_unreachable(w)
    queue(w)
    run = w.store.run(RID)
    assert run.state == "pending" and dispatches(w) == []
    [failed] = [e for e in w.events_named("qa_step_failed") if e["step"] == "prepare"]
    assert failed["task_id"] == RID
    chosen = git(w.repo, "rev-parse", "refs/arceus/qa-1")
    # The folder and what was chosen are kept before anything else happens: the theme's ref was named, not made.
    snap = json.loads(run.snapshot)
    assert snap["workspace"] == str(qa_root(w))
    assert snap["pinned"] == {"acme-platform": chosen, "acme-theme": None}
    assert refs(w, w.repo) == f"refs/arceus/qa-1 {chosen}"
    # Main moves on meanwhile, and the runner restarts: the run reads what it had chosen, not today's main.
    newer = push_main(w)
    hidden.rename(theme_origin)
    w.restart()
    w.tick()
    assert w.store.run(RID).state == "running"
    assert git(checkout(w), "rev-parse", "HEAD") == chosen != newer
    assert f"`acme-platform`: checked out at `{chosen}`" in dispatches(w)[0]["argv"][-1]


def test_a_run_stopped_before_it_starts_takes_away_the_refs_it_made(w):
    theme_origin, hidden = theme_unreachable(w)
    queue(w)
    assert refs(w, w.repo) != ""
    hidden.rename(theme_origin)
    w.store.update_run(RID, stop_requested=1)
    w.tick()
    run = w.store.run(RID)
    assert (run.state, run.outcome) == ("done", "stopped")
    assert refs(w, w.repo) == "" and refs(w, w.cfg.paths.workspace / "acme-theme") == ""
    assert dispatches(w) == []


# -- the folder is the runner's, or it is left alone -------------------------------------------------------------------

def test_a_folder_that_holds_someone_elses_files_is_never_used(tmp_path):
    root = tmp_path / "qa-workspace"
    (root / "acme-platform").mkdir(parents=True)
    (root / "acme-platform" / "keep.txt").write_text("someone's\n")
    (root / "notes.md").write_text("mine\n")
    w = apart_world(tmp_path)
    try:
        [problem] = [c for c in qa_check(w.cfg) if c.name == "config.qa.workspace"]
        assert problem.status == "fail" and FOLDER_MARK in problem.detail
        queue(w)
        run = w.store.run(RID)
        assert (run.state, run.outcome) == ("done", "unfinished") and FOLDER_MARK in run.reason
        assert (root / "acme-platform" / "keep.txt").read_text() == "someone's\n"
        assert (root / "notes.md").read_text() == "mine\n" and not (root / FOLDER_MARK).exists()
        assert dispatches(w) == [] and refs(w, w.cfg.paths.workspace / "acme-platform") == ""
    finally:
        w.events.close()
        w.store.close()


def test_an_empty_folder_becomes_the_runners_and_stays_so(w):
    qa_root(w).mkdir(parents=True)
    queue(w)
    assert (qa_root(w) / FOLDER_MARK).is_file()
    assert w.store.run(RID).state == "running"
    assert [c.status for c in qa_check(w.cfg) if c.name == "config.qa.workspace"] == []


# -- stopping and finishing: the worker first, then its files ---------------------------------------------------------

def session(w, folder: Path | None = None) -> str:
    return Engine.folder_session(folder or home(w))


def sessions(w) -> list[str]:
    return w.engine_state().get("sessions", [])


def keep_alive(w, *names: str) -> None:
    """The fake tmux keeps these sessions whatever the runner asks: a worker that does not go."""
    w.set_engine(lambda s: s.update(unkillable=list(names)))


def gone_before_restored(w) -> bool:
    """The run's worker was confirmed gone before anything of the run was removed, in events.jsonl's order."""
    names = [e["event"] for e in w.events_named("qa_worker_gone", "qa_workspace_restored")]
    return names[:2] == ["qa_worker_gone", "qa_workspace_restored"]


def test_the_session_of_a_folder_is_named_as_the_engine_names_it():
    # engine/bin/supervisor-lib.sh slug_for; on the lab: night-qa-workspace-9e5b3bd122ce, night-workspace-0b284ae0ba8b
    assert Engine.folder_session(Path("/home/arceus/arceus/qa-workspace")) == "night-qa-workspace-9e5b3bd122ce"
    assert Engine.folder_session(Path("/home/arceus/arceus/workspace")) == "night-workspace-0b284ae0ba8b"


def test_a_stop_after_a_dispatch_the_runner_never_recorded_stops_the_worker_before_anything_goes(w):
    take(w)
    queue(w)
    co = checkout(w)
    # The engine took the dispatch, the runner crashed before it wrote `running`; then someone asks to stop.
    w.store.update_run(RID, state="preparing", stop_requested=1)
    w.tick()
    assert w.store.run(RID).state == "running" and co.exists()  # followed as the live run it is
    assert stops(w) == []
    w.tick()
    assert stops(w) == [str(home(w))]
    assert w.store.run(RID).state == "done" and not co.exists() and gone_before_restored(w)
    assert session(w) not in sessions(w) and session(w, w.cfg.paths.workspace) in sessions(w)
    assert w.store.task("ACME-1").state == "dispatched"
    assert w.engine_state()["tasks"]["ACME-1"]["attempt_live"] is True


def test_nothing_is_taken_away_while_the_worker_is_still_running(w):
    take(w)
    queue(w)
    co = checkout(w)
    keep_alive(w, session(w))  # its tmux session outlives the stop
    w.store.update_run(RID, stop_requested=1)
    w.tick()
    w.tick()
    assert w.store.run(RID).state == "running" and co.exists() and refs(w, w.repo) != ""
    unconfirmed = w.events_named("qa_stop_unconfirmed")
    assert len(unconfirmed) == 2 and unconfirmed[0]["reason"] == "its worker is still running"
    keep_alive(w)
    w.tick()
    assert w.store.run(RID).state == "done" and not co.exists() and gone_before_restored(w)
    assert session(w, w.cfg.paths.workspace) in sessions(w)  # the ticket's worker is never touched


def test_a_stop_is_seen_through_even_when_the_engine_ends_the_attempt_first_and_the_runner_restarts(w):
    """The engine's record says the attempt is over while the worker's tmux session goes on: its files stay until
    that session is gone, across a restart too, and the ticket's worker is never touched."""
    take(w)
    queue(w)
    co = checkout(w)
    keep_alive(w, session(w))
    w.store.update_run(RID, stop_requested=1)
    w.set_engine(lambda s: s["tasks"][RID].update(state="failed", state_reason="stopped"))  # the record moves on
    w.tick()
    assert w.store.run(RID).state == "running" and co.exists() and refs(w, w.repo) != ""
    assert json.loads(w.store.run(RID).snapshot)["stopping"]["why"] == "stopped by `arceus-runner qa stop`"
    w.restart()
    w.tick()
    assert w.store.run(RID).state == "running" and co.exists()
    keep_alive(w)  # the session ends
    w.tick()
    run = w.store.run(RID)
    assert (run.state, run.reason) == ("done", "stopped by `arceus-runner qa stop`") and not co.exists()
    assert session(w, w.cfg.paths.workspace) in sessions(w) and w.store.task("ACME-1").state == "dispatched"


def test_a_finished_run_keeps_its_checkouts_until_its_worker_has_gone(w):
    """The engine writes `done` and lets the worker's session go a few seconds later: the run is put back only once
    it has."""
    take(w)
    queue(w)
    co = checkout(w)
    out = out_dir(w)
    out.mkdir(parents=True, exist_ok=True)
    (out / "findings.json").write_text(json.dumps({"summary": "Checked sign-in.", "blocked": "", "findings": [],
                                                   "tested": [{"what": "Sign-in", "result": "ok"}]}))
    keep_alive(w, session(w))
    finish(w)  # done in the engine's record, the session still there
    assert w.store.run(RID).state == "running" and co.exists() and refs(w, w.repo) != ""
    assert w.events_named("qa_worker_still_running")
    keep_alive(w)
    w.tick()
    run = w.store.run(RID)
    assert run.state == "done" and run.outcome != "unfinished" and not co.exists() and gone_before_restored(w)
    assert session(w) not in sessions(w) and session(w, w.cfg.paths.workspace) in sessions(w)


def lose_record(w, record: str) -> None:
    """The engine answers `task show` with «задачі qa-1 немає»: for a record it has not, and for one it cannot read."""
    task_json = w.cfg.paths.bulava_state / "tasks" / task_key(RID) / "task.json"
    task_json.parent.mkdir(parents=True, exist_ok=True)
    task_json.write_text("" if record == "corrupt" else "{}")
    w.set_engine(lambda s: s["tasks"].pop(RID))
    assert w.runner.qa.d.engine.show(RID) is None


@pytest.mark.parametrize("record", ["missing", "corrupt"])
def test_a_run_whose_engine_record_is_gone_leaves_its_worker_working(w, record):
    """No stop asked, time left: the worker, a session tmux could end at any moment, is left to work, and its
    checkout and refs stay. Asked to stop, only its session ends and the run is published as stopped."""
    take(w)
    queue(w)
    co = checkout(w)
    lose_record(w, record)
    w.tick()
    w.tick()
    assert w.store.run(RID).state == "running" and co.exists() and refs(w, w.repo) != ""
    assert session(w) in sessions(w) and stops(w) == []
    assert w.events_named("qa_waits")[-1]["reason"] == "record_unreadable_worker_alive"
    w.store.update_run(RID, stop_requested=1)
    w.tick()
    run = w.store.run(RID)
    assert (run.state, run.outcome, run.reason) == ("done", "stopped", "stopped by `arceus-runner qa stop`")
    assert session(w) not in sessions(w) and session(w, w.cfg.paths.workspace) in sessions(w)
    assert not co.exists() and refs(w, w.repo) == "" and gone_before_restored(w)


def test_a_run_whose_engine_record_is_gone_is_stopped_at_its_time(w):
    queue(w)
    lose_record(w, "missing")
    w.tick()
    assert w.store.run(RID).state == "running" and session(w) in sessions(w)
    w.now += w.cfg.qa.timeout + timedelta(minutes=1)
    w.tick()
    run = w.store.run(RID)
    assert run.state == "done" and "did not finish in 8 h" in run.reason
    assert session(w) not in sessions(w) and not checkout(w).exists()


def test_a_run_whose_record_is_gone_and_whose_worker_is_gone_is_closed(w):
    queue(w)
    lose_record(w, "missing")
    w.set_engine(lambda s: s["sessions"].remove(session(w)))  # the worker went too
    w.tick()
    run = w.store.run(RID)
    assert run.state == "done" and "lost track" in run.reason and not checkout(w).exists()


@pytest.mark.parametrize("bad", [{"state": "paused"}, {"state": "running", "attempt_live": None},
                                 {"state": "queued", "attempt_live": "no"}])
def test_a_record_in_a_state_the_engine_does_not_name_decides_nothing(w, bad):
    queue(w)
    co = checkout(w)
    w.set_engine(lambda s: s["tasks"][RID].update(bad) if "attempt_live" in bad else
                 (s["tasks"][RID].update(bad), s["tasks"][RID].pop("attempt_live")))
    with pytest.raises(EngineError, match="task show qa-1"):
        w.runner.qa.d.engine.show(RID)
    w.tick()
    w.tick()
    assert w.store.run(RID).state == "running" and co.exists() and refs(w, w.repo) != ""
    assert session(w) in sessions(w) and stops(w) == []
    assert [c for c in w.calls("worker-send") if RID in c["argv"]] == []

def test_a_run_past_its_time_whose_attempt_already_ended_is_stopped_not_sent_on(w):
    queue(w)
    w.set_engine(lambda s: s["tasks"][RID].update(attempt_live=False))  # the session died, the record says running
    w.now += w.cfg.qa.timeout + timedelta(minutes=1)
    w.tick()
    assert [c for c in w.calls("worker-send") if RID in c["argv"]] == []
    run = w.store.run(RID)
    assert run.state == "done" and "did not finish in 8 h" in run.reason


def test_an_engine_that_refuses_the_stop_is_asked_again(w):
    queue(w)
    w.store.update_run(RID, stop_requested=1)
    w.set_engine(lambda s: s["next_rc"].update({"night-shift stop": 1}))
    w.tick()
    assert w.store.run(RID).state == "running" and checkout(w).exists()
    assert [e["step"] for e in w.events_named("qa_step_failed")] == ["running"]
    w.tick()
    assert w.store.run(RID).state == "done"


def test_without_tmux_a_stop_waits_and_nothing_is_removed(w, monkeypatch):
    queue(w)
    co = checkout(w)
    engine = w.runner.qa.d.engine
    monkeypatch.setitem(engine.extra_env, "PATH", "/nonexistent")  # tmux cannot be found
    w.store.update_run(RID, stop_requested=1)
    w.tick()
    run = w.store.run(RID)
    assert run.state == "running" and co.exists() and git(w.repo, "rev-parse", "refs/arceus/qa-1")
    [failed] = [e for e in w.events_named("qa_step_failed") if e["task_id"] == RID]
    assert "tmux is not on PATH" in failed["reason"]


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_stopping_a_worker_ends_its_own_tmux_session_and_no_other(tmp_path):
    w = apart_world(tmp_path)
    # A tmux server of this test's own and nobody else's: inside a tmux session $TMUX would point every command at
    # that session's server (a `kill-server` there ends the session the tests run in), so no TMUX* variable passes,
    # and the server has a label of its own in a folder of its own.
    socket_dir = Path(tempfile.mkdtemp(prefix="arceus-tmux-", dir="/tmp"))  # short: a socket's path has a limit
    label = "arceus-test"
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("TMUX")}, "TMUX_TMPDIR": str(socket_dir)}

    def tmux(*args: str) -> int:
        return subprocess.run(["tmux", "-L", label, *args], env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30).returncode

    try:
        engine = Engine(w.cfg, extra_env={"TMUX_TMPDIR": str(socket_dir), "SUPERVISOR_TMUX_SOCKET": label,
                                          "PATH": os.environ["PATH"]})
        qa_home, ws = home(w), w.cfg.paths.workspace
        qa_home.mkdir(parents=True)
        ws.mkdir(parents=True, exist_ok=True)
        qa, ticket = Engine.folder_session(qa_home), Engine.folder_session(ws)
        for name in (qa, ticket, f"{qa}-2"):  # the last only begins the same: another folder's
            assert tmux("new-session", "-d", "-s", name, "sleep 300") == 0
        assert engine.session_alive(qa) and engine.session_alive(ticket)
        assert engine.stop_worker(qa_home) is True
        assert not engine.session_alive(qa)
        assert engine.session_alive(ticket) and tmux("has-session", "-t", f"={qa}-2") == 0
        w.set_engine(lambda s: s["next_rc"].update({"night-shift stop": 1}))
        with pytest.raises(EngineError, match="night-shift stop"):
            engine.stop_worker(ws)
        assert engine.session_alive(ticket)
        # Without tmux, a session cannot be told gone: an error, never "stopped".
        blind = Engine(w.cfg, extra_env={"TMUX_TMPDIR": str(socket_dir), "SUPERVISOR_TMUX_SOCKET": label,
                                         "PATH": "/nonexistent"})
        with pytest.raises(EngineError, match="tmux is not on PATH"):
            blind.session_alive(ticket)
    finally:
        if (socket_dir / f"tmux-{os.getuid()}" / label).exists():  # only the server this test made
            tmux("kill-server")
        shutil.rmtree(socket_dir, ignore_errors=True)
        w.events.close()
        w.store.close()


def test_a_retry_whose_preparation_crosses_the_deadline_is_never_dispatched(w, monkeypatch):
    w.store.add_run("qa", "command", "ops", scenario=1)
    w.set_engine(lambda s: s["next_rc"].update({"dispatch": 6}))  # busy: the run waits in `preparing`
    w.tick()
    assert w.store.run(RID).state == "preparing" and len(dispatches(w)) == 1
    real = w.runner.qa.git.checkout_at

    def slow(*a, **k):  # getting the checkouts ready takes the rest of the run's time
        made = real(*a, **k)
        w.now += w.cfg.qa.timeout
        return made
    monkeypatch.setattr(w.runner.qa.git, "checkout_at", slow)
    w.tick()
    run = w.store.run(RID)
    assert len(dispatches(w)) == 1  # nothing more reached the engine
    assert run.state == "done" and "could not start" in run.reason
    assert not checkout(w).exists() and refs(w, w.repo) == ""


def test_nothing_the_test_run_does_in_a_checkout_reaches_the_ticket_clone(w):
    """A checkout is read-only, its git metadata too: a shell write or a git stash, branch or commit there fails.
    And it is a repository of its own that only borrows the clone's objects, with no remote: even after a deliberate
    `chmod`, whatever git is run there stays there."""
    take(w)
    dirty_ticket(w)
    before = ticket_state(w.repo)
    queue(w)
    co = checkout(w)
    assert denied(co / "README.md") and denied(co / "new.txt") and denied(co / ".git" / "refs" / "heads" / "x")
    for args in (["stash"], ["switch", "-q", "-c", "qa-scratch"], ["tag", "qa-tag"], ["config", "user.name", "x"]):
        assert subprocess.run(["git", "-C", str(co), *args], capture_output=True).returncode != 0, args
    unlock(co)  # made writable on purpose: the second wall
    (co / "README.md").write_text("changed by the test run\n")
    git(co, "stash")
    git(co, "switch", "-q", "-c", "qa-scratch")
    (co / "probe.txt").write_text("x\n")
    git(co, "add", "probe.txt")
    git(co, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "probe")
    git(co, "config", "user.name", "someone")
    git(co, "tag", "qa-tag")
    assert git(co, "remote") == ""
    assert ticket_state(w.repo) == before
    assert "qa-scratch" not in git(w.repo, "branch", "--list") and git(w.repo, "tag", "--list") == ""
    assert git(w.repo, "config", "--get", "user.name") != "someone"
    # The report folder is the agent's to write in.
    (out_dir(w) / "plan.md").write_text("## Sign-in\n- [ ] Sign in\n")
    finish(w)
    assert w.store.run(RID).state == "done" and not co.exists()
    assert ticket_state(w.repo) == before and refs(w, w.repo) == ""


# -- links inside the runner's folder ----------------------------------------------------------------------------------

@pytest.mark.parametrize("part", ["repos", "home"])
def test_a_link_in_place_of_repos_or_home_is_refused_before_anything_is_written(w, part):
    take(w)
    dirty_ticket(w)
    before = ticket_state(w.repo)
    files = sorted(p.name for p in w.repo.iterdir())
    qa_root(w).mkdir(parents=True)
    (qa_root(w) / FOLDER_MARK).write_text("ours\n")
    (qa_root(w) / part).symlink_to(w.cfg.paths.workspace)  # into the ticket's workspace
    queue(w)
    run = w.store.run(RID)
    assert (run.state, run.outcome) == ("done", "unfinished") and "is a link" in run.reason
    assert ticket_state(w.repo) == before and sorted(p.name for p in w.repo.iterdir()) == files
    assert dispatches(w) == [RID] * 0 and stops(w) == []
    assert w.engine_state()["tasks"]["ACME-1"]["attempt_live"] is True


@pytest.mark.parametrize("part", ["repos", "home"])
def test_a_link_put_in_place_of_repos_or_home_while_the_run_works_leads_nowhere(w, part):
    """`repos` or `home` replaced by a link into the ticket's workspace while the run works: the stop does not reach
    the ticket's folder by that path, the run's own session is ended by its kept name, and nothing is removed
    through the link."""
    take(w)
    dirty_ticket(w)
    queue(w)
    before = ticket_state(w.repo)
    files = sorted(p.name for p in w.repo.iterdir())
    real = qa_root(w) / part
    real.rename(qa_root(w) / f"{part}-moved")
    real.symlink_to(w.cfg.paths.workspace)
    w.store.update_run(RID, stop_requested=1)
    w.tick()
    run = w.store.run(RID)
    assert run.state == "done"
    assert str(w.cfg.paths.workspace) not in stops(w) and str(home(w)) not in stops(w)
    assert session(w, w.cfg.paths.workspace) in sessions(w) and w.store.task("ACME-1").state == "dispatched"
    assert ticket_state(w.repo) == before and sorted(p.name for p in w.repo.iterdir()) == files
    assert [e["folder"] for e in w.events_named("qa_folder_not_ours")] == [run.folder]
    assert refs(w, w.repo) == ""


# -- a crash between the engine's answer and the runner's record, and a record it cannot read -----------------------

@pytest.mark.parametrize("why", ["stop", "deadline"])
def test_a_preparing_run_with_an_unreadable_record_and_a_live_worker_is_never_put_back_under_it(w, why):
    """The engine took the dispatch, the runner crashed before it wrote `running`, and the engine's record cannot be
    read: the session kept before the dispatch says a worker runs, so the run is followed as running, and its
    checkouts and refs stay until that worker is gone."""
    queue(w)
    co = checkout(w)
    w.store.update_run(RID, state="preparing")
    w.set_engine(lambda s: s["tasks"].pop(RID))  # task show: «задачі qa-1 немає», as for an unreadable record
    keep_alive(w, session(w))
    if why == "stop":
        w.store.update_run(RID, stop_requested=1)
    else:
        w.now += w.cfg.qa.timeout + timedelta(minutes=1)
    w.tick()
    w.tick()
    assert w.store.run(RID).state == "running" and co.exists() and refs(w, w.repo) != ""
    assert len(dispatches(w)) == 1  # never dispatched again under it
    keep_alive(w)
    w.tick()
    run = w.store.run(RID)
    assert run.state == "done" and not co.exists() and gone_before_restored(w) and len(dispatches(w)) == 1


def test_a_preparing_run_whose_worker_cannot_be_asked_waits(w):
    queue(w)
    co = checkout(w)
    w.store.update_run(RID, state="preparing", stop_requested=1)
    w.set_engine(lambda s: (s["tasks"].pop(RID), s.update(tmux_fails=True)))
    w.tick()
    assert w.store.run(RID).state == "preparing" and co.exists() and refs(w, w.repo) != ""
    assert [e for e in w.events_named("qa_step_failed") if e["step"] == "preparing"]


@pytest.mark.parametrize("bad", [{"attempts": 3}, {"attempt_live": "yes"}, {"receipt": [1]}, {"state": None},
                                 {"attempts": [7]}])
def test_a_malformed_engine_record_decides_nothing(w, bad):
    queue(w)
    co = checkout(w)
    w.set_engine(lambda s: s["tasks"][RID].update({"state": "running", "attempt_live": False, **bad}))
    with pytest.raises(EngineError, match="task show qa-1"):
        w.runner.qa.d.engine.show(RID)
    w.store.update_run(RID, stop_requested=1)
    w.tick()
    w.tick()
    assert w.store.run(RID).state == "running" and co.exists() and refs(w, w.repo) != ""
    assert [c for c in w.calls("worker-send") if RID in c["argv"]] == []
    assert all(e["step"] == "running" for e in w.events_named("qa_step_failed"))

def report_in_parts(w) -> None:
    out = out_dir(w)
    out.mkdir(parents=True, exist_ok=True)
    (out / "reset.png").write_bytes(PNG)
    first = {"summary": "Checked sign-in.", "blocked": "", "parts": 2, "checks": 2, "findings_count": 2,
             "findings": [{**WHOLE[0], "code": [{"repo": "acme-platform", "file": "README.md", "line": 1}]}],
             "tested": [{"what": "Sign-in as the customer", "result": "ok"}]}
    second = {"tested": [{"what": "Password reset", "result": "problem"}],
              "findings": [{**WHOLE[0], "title": "Reset mail never arrives"}]}
    (out / "findings.json").write_text(json.dumps(first))
    (out / "findings-2.json").write_text(json.dumps(second))


def test_a_report_in_parts_is_reviewed_in_the_folder_across_a_restart_and_its_report_stays(w):
    take(w)
    queue(w)
    report_in_parts(w)
    accepted_with_notes(w, why="STATE: COMPLETE\nVERDICT: FAIL\n1. Finding 1 names no URL.")
    [send] = [c for c in w.calls("worker-send") if RID in c["argv"]]
    argv = send["argv"]
    assert argv[-5] == str(home(w)) and f"`artifacts/{RID}/findings-2.txt`" in argv[-1]
    link = home(w) / "artifacts" / RID / "findings-2.txt"
    assert link.resolve() == (out_dir(w) / "findings-2.json").resolve()
    w.restart()  # the runner restarts while the second part is under review
    w.tick()
    assert w.store.run(RID).state == "running" and len([c for c in w.calls("worker-send") if RID in c["argv"]]) == 1
    finish(w, receipt={"result": "succeeded_research", "review": "passed", "commits": [], "review_why": ""})
    run = w.store.run(RID)
    assert (run.state, run.outcome) == ("done", "found")
    folder = w.cfg.qa.reports / "1"
    page = (folder / "report.html").read_text()
    assert "Reset link gives 404" in page and "Reset mail never arrives" in page
    [shot] = list((folder / "media").iterdir())
    assert shot.read_bytes() == PNG and f"media/{shot.name}" in page
    published = json.loads((folder / "result.json").read_text())
    [code] = published["findings"][0]["code"]
    assert code["sha"] == git(w.origins["acme-platform"], "rev-parse", "main")
    assert not out_dir(w).exists() and not link.exists() and not (checkout(w)).exists()
    # Putting back and publishing again, as after a crash in between, changes nothing and loses nothing.
    w.runner.qa._restore(w.store.run(RID))
    w.runner.qa._publish(w.store.run(RID))
    assert (folder / "report.html").read_text() == page and shot.read_bytes() == PNG
    assert git(w.repo, "for-each-ref", "refs/arceus/") == "" and w.store.task("ACME-1").state == "dispatched"


def test_a_repository_that_left_the_group_between_two_tries_leaves_no_ref_behind(w, monkeypatch):
    theme_origin, hidden = theme_unreachable(w)
    # The theme fails first, so the platform's ref is made; the run waits for another try.
    w.runner.qa.d.workspace.group  # noqa: B018 (the real one, before it is replaced below)
    real = w.runner.qa.d.workspace.group
    monkeypatch.setattr(w.runner.qa.d.workspace, "group",
                        lambda: sorted(real(), key=lambda r: r.relative_path != "acme-platform"))
    queue(w)
    assert refs(w, w.repo) != "" and w.store.run(RID).state == "pending"
    hidden.rename(theme_origin)
    # Before the next try the platform leaves the group (excluded, archived): the run goes on without it.
    monkeypatch.setattr(w.runner.qa.d.workspace, "group",
                        lambda: [r for r in real() if r.relative_path != "acme-platform"])
    w.tick()
    run = w.store.run(RID)
    assert run.state == "running" and [r["repo"] for r in json.loads(run.snapshot)["repos"]] == ["acme-theme"]
    finish(w)
    assert w.store.run(RID).state == "done"
    assert refs(w, w.repo) == "" and refs(w, w.cfg.paths.workspace / "acme-theme") == ""


def test_a_link_given_as_the_folder_is_refused_and_what_it_leads_to_is_left_alone(tmp_path):
    target = tmp_path / "elsewhere"
    (target / "acme-platform").mkdir(parents=True)
    (target / "acme-platform" / "keep.txt").write_text("not the runner's\n")
    (tmp_path / "qa-workspace").symlink_to(target)
    w = apart_world(tmp_path)
    try:
        queue(w)
        run = w.store.run(RID)
        assert (run.state, run.outcome) == ("done", "unfinished") and "is a link" in run.reason
        assert (target / "acme-platform" / "keep.txt").read_text() == "not the runner's\n"
        assert not (target / FOLDER_MARK).exists() and dispatches(w) == []
    finally:
        w.events.close()
        w.store.close()


def test_a_folder_replaced_before_putting_back_is_left_alone_and_the_refs_still_go(w):
    queue(w)
    root = qa_root(w)
    out = out_dir(w)
    # Someone moves the runner's folder away and puts a folder of their own in its place.
    moved = root.with_name("qa-workspace-moved")
    root.rename(moved)
    (root / "repos" / "acme-platform").mkdir(parents=True)
    (root / "repos" / "acme-platform" / "keep.txt").write_text("theirs\n")
    (root / "home" / ".arceus" / "qa" / RID).mkdir(parents=True)
    (root / "home" / ".arceus" / "qa" / RID / "theirs.txt").write_text("theirs\n")
    finish(w)
    run = w.store.run(RID)
    assert run.state == "done"
    assert (root / "repos" / "acme-platform" / "keep.txt").read_text() == "theirs\n"
    assert (out / "theirs.txt").exists() and out == root / "home" / ".arceus" / "qa" / RID
    assert refs(w, w.repo) == ""
    assert [e["folder"] for e in w.events_named("qa_folder_not_ours")] == [str(root)]


def test_the_folders_own_instructions_say_to_read_and_never_change(w):
    queue(w)
    text = (home(w) / "AGENTS.md").read_text()
    assert "Never change it" in text and "commit nothing" in text
    assert "Commit inside each repository" not in text and "switched to the task's branch" not in text
    # The ticket's workspace keeps its own instructions.
    take(w)
    assert "Commit inside each repository you change" in (w.cfg.paths.workspace / "AGENTS.md").read_text()


def test_a_scheduled_test_run_starts_beside_a_ticket(w):
    take(w)
    # NOW is Tuesday 10:00 in Kyiv: a scenario due at 09:30 is queued and starts while the ticket works.
    scenario(w, "Smoke", "Sign in.\n", days=("tue",), at="09:30")
    w.tick()
    [run] = [r for r in w.store.runs("qa") if r.source == "schedule"]
    assert run.state == "running" and run.folder == str(qa_root(w))
    assert w.store.task("ACME-1").state == "dispatched"


def test_the_console_starts_and_stops_a_run_beside_a_ticket(w):
    from arceus_runner.console.runs import start, stop
    from arceus_runner.console.store import User
    take(w)
    person = User(id=1, email="q@example.com", name="Q", status="active", roles=frozenset({"qa"}), note="",
                  approved_by=None, approved_at=None, last_login_at=None, created_at="")
    n = start(w.cfg, "qa", person, scenario=1)
    w.tick()
    assert w.store.run(f"qa-{n}").state == "running"
    stop(w.cfg, "qa", n)
    w.tick()
    run = w.store.run(f"qa-{n}")
    assert (run.state, run.outcome) == ("done", "stopped") and stops(w) == [str(home(w))]
    assert w.store.task("ACME-1").state == "dispatched"
    assert (w.cfg.qa.reports / str(n) / "report.html").exists()


def test_a_run_the_engine_kept_busy_past_its_time_is_never_dispatched_late(w):
    w.store.add_run("qa", "command", "ops", scenario=1)
    w.set_engine(lambda s: s["next_rc"].update({"dispatch": 6}))
    w.tick()
    assert w.store.run(RID).state == "preparing"
    w.now += w.cfg.qa.timeout + timedelta(minutes=1)
    w.tick()  # the engine is free now, but the run's time is over
    run = w.store.run(RID)
    assert run.state == "done" and "could not start" in run.reason
    assert len(dispatches(w)) == 1  # only the one the engine answered busy
    assert not (checkout(w)).exists() and refs(w, w.repo) == ""


def test_a_ticket_clone_made_again_and_git_maintenance_never_break_the_run(w):
    queue(w)
    co = checkout(w)
    sha = git(co, "rev-parse", "HEAD")
    git(w.repo, "gc", "--quiet", "--prune=now")  # maintenance while the run reads: its commit is kept
    assert git(co, "rev-parse", "HEAD") == sha and git(co, "status", "--porcelain") == ""
    assert git(co, "cat-file", "-t", sha) == "commit"
    # The ticket's clone is removed and cloned again: the run's co no longer belongs to it.
    shutil.rmtree(w.repo)
    subprocess.run(["git", "clone", "-q", str(w.origins["acme-platform"]), str(w.repo)], check=True)
    finish(w)
    assert w.store.run(RID).state == "done"
    assert not co.exists() and git(w.repo, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_the_runner_asks_the_tmux_server_the_engines_workers_live_in(tmp_path):
    w = apart_world(tmp_path)
    try:
        # A server engine keeps its workers in a tmux server of its own (engine/supervisor/config.sh: `bulava`).
        assert Engine(w.cfg)._tmux_args()[1] == ["tmux", "-L", "bulava"]
        assert Engine(w.cfg, extra_env={"SUPERVISOR_TMUX_SOCKET": "other"})._tmux_args()[1] == ["tmux", "-L", "other"]
    finally:
        w.events.close()
        w.store.close()


# -- a folder replaced while the run works: nothing more is handed to the engine by its path --------------------------

def replace_home_with_link(w) -> None:
    real = home(w)
    real.rename(qa_root(w) / "home-moved")
    real.symlink_to(w.cfg.paths.workspace)


def test_a_run_whose_home_was_replaced_is_never_resumed_there(w):
    take(w)
    queue(w)
    before = ticket_state(w.repo)
    qa_session = session(w)  # named before the swap: after it the path leads to the ticket's folder
    replace_home_with_link(w)
    w.set_engine(lambda s: (s["tasks"][RID].update(attempt_live=False), s["sessions"].remove(qa_session)))
    w.tick()  # the session died: a resume would go to the engine by the home's path, now the ticket's folder
    assert [c for c in w.calls("worker-send") if RID in c["argv"]] == []
    run = w.store.run(RID)
    assert run.state == "done" and "no longer the runner's folder" in run.reason
    assert str(w.cfg.paths.workspace) not in stops(w) and session(w, w.cfg.paths.workspace) in sessions(w)
    assert not (w.cfg.paths.workspace / "artifacts").exists() and not (w.cfg.paths.workspace / ".arceus" / "qa").exists()
    assert ticket_state(w.repo) == before and w.store.task("ACME-1").state == "dispatched"


def test_a_run_whose_home_was_replaced_sends_no_further_part_of_its_report(w):
    take(w)
    queue(w)
    report_in_parts(w)
    replace_home_with_link(w)
    accepted_with_notes(w, why="STATE: COMPLETE\nVERDICT: FAIL\n1. Finding 1 names no URL.")
    assert [c for c in w.calls("worker-send") if RID in c["argv"]] == []
    assert not (w.cfg.paths.workspace / "artifacts").exists()
    run = w.store.run(RID)
    assert run.state == "done" and "no longer the runner's folder" in run.reason
    assert any("its report was not read" in p for p in json.loads(run.result)["problems"])


@pytest.mark.parametrize("part", [".arceus", ".arceus/qa", "artifacts"])
def test_a_link_on_the_way_to_the_report_folder_removes_nothing_on_its_other_side(w, part):
    queue(w)
    elsewhere = w.tmp / "elsewhere"
    victim = elsewhere / ("qa" if part == ".arceus" else "") / RID if part != "artifacts" else elsewhere / RID
    victim.mkdir(parents=True)
    (victim / "keep.txt").write_text("not the runner's\n")
    link = home(w) / part
    shutil.rmtree(link)
    link.symlink_to(elsewhere)
    finish(w)
    run = w.store.run(RID)
    assert run.state == "done"
    assert (victim / "keep.txt").read_text() == "not the runner's\n"


# -- the home's own git, a ref made before its commit was kept, a refused dispatch, a stop before anything else ------

def ticket_git(w) -> dict:
    """The ticket's clone's git: everything but the run's own ref (`refs/arceus/`), which putting back removes."""
    refs_ = [r for r in git(w.repo, "for-each-ref", "--format=%(refname) %(objectname)").splitlines()
             if not r.startswith("refs/arceus/")]
    return {"head": git(w.repo, "rev-parse", "HEAD"), "branch": git(w.repo, "branch", "--show-current"),
            "index": git(w.repo, "diff", "--cached"), "refs": refs_, "stash": git(w.repo, "stash", "list")}


@pytest.mark.parametrize("how", ["link", "gitdir"])
def test_a_home_whose_git_leads_to_the_tickets_is_never_used(w, how):
    """`home/.git` turned into a link to the ticket's clone's git, or a `.git` file naming it (`gitdir:`): every git
    command in home would act on the ticket's metadata. Refused when a run is prepared, and found when it ends."""
    take(w)
    dirty_ticket(w)

    def redirect():
        g = home(w) / ".git"
        if g.is_dir() and not g.is_symlink():
            unlock(g)
            shutil.rmtree(g)
        if how == "link":
            g.symlink_to(w.repo / ".git")
        else:
            g.write_text(f"gitdir: {w.repo / '.git'}\n")

    # Prepared into a folder whose home's git is someone else's: refused before anything runs there.
    home(w).mkdir(parents=True)
    (qa_root(w) / FOLDER_MARK).write_text("ours\n")
    redirect()
    before = ticket_git(w)
    queue(w)
    run = w.store.run(RID)
    assert (run.state, run.outcome) == ("done", "unfinished") and ".git" in run.reason
    assert ticket_git(w) == before and dispatches(w) == []


@pytest.mark.parametrize("how", ["link", "gitdir"])
def test_a_home_whose_git_is_redirected_while_the_run_works_is_left_alone(w, how):
    take(w)
    dirty_ticket(w)
    queue(w)
    g = home(w) / ".git"
    shutil.rmtree(g)
    if how == "link":
        g.symlink_to(w.repo / ".git")
    else:
        g.write_text(f"gitdir: {w.repo / '.git'}\n")
    before = ticket_git(w)
    finish(w)
    run = w.store.run(RID)
    assert run.state == "done" and "no longer the runner's folder" in run.reason
    assert ticket_git(w) == before and refs(w, w.repo) == ""


def test_a_ref_made_before_a_crash_kept_its_commit_is_the_one_read(w, monkeypatch):
    """The runner died after the fetch made the run's ref and before it wrote down the commit: the next pass takes
    the commit from that ref, not from the code host's branch, which has moved on meanwhile."""
    real = w.runner.qa.git.fetch_pinned

    def fetch_then_die(path, branch, ref):
        real(path, branch, ref)
        raise OSError("the runner dies here")
    with monkeypatch.context() as m:
        m.setattr(w.runner.qa.git, "fetch_pinned", fetch_then_die)
        queue(w)
    chosen = git(w.repo, "rev-parse", "refs/arceus/qa-1")
    assert json.loads(w.store.run(RID).snapshot)["pinned"] == {"acme-platform": None}
    newer = push_main(w)
    w.restart()
    w.tick()
    assert w.store.run(RID).state == "running"
    assert git(checkout(w), "rev-parse", "HEAD") == chosen != newer
    assert json.loads(w.store.run(RID).snapshot)["pinned"]["acme-platform"] == chosen


def test_a_dispatch_the_engine_refused_after_starting_a_session_waits_for_it(w, monkeypatch):
    """The engine may start the folder's session and then refuse (exit 2): the run's checkouts stay until that
    session is gone, and while tmux cannot say, they stay too."""
    engine = w.runner.qa.d.engine
    real = engine.dispatch

    def starts_then_refuses(**kw):
        w.set_engine(lambda s: (s["next_rc"].update({"dispatch": 2}), s.setdefault("sessions", []).append(session(w)),
                                s.update(unkillable=[session(w)])))
        return real(**kw)
    with monkeypatch.context() as m:
        m.setattr(engine, "dispatch", starts_then_refuses)
        w.store.add_run("qa", "command", "ops", scenario=1)
        w.tick()
    co = checkout(w)
    run = w.store.run(RID)
    assert run.state == "running" and co.exists() and refs(w, w.repo) != ""
    assert json.loads(run.snapshot)["stopping"]["why"].startswith("the execution engine did not accept the run")
    w.set_engine(lambda s: s.update(unkillable=[], tmux_fails=True))
    w.tick()
    assert w.store.run(RID).state == "running" and co.exists()
    w.set_engine(lambda s: s.update(tmux_fails=False))
    w.tick()
    run = w.store.run(RID)
    assert run.state == "done" and "did not accept" in run.reason and not co.exists() and refs(w, w.repo) == ""
    assert session(w) not in sessions(w)


def test_a_stop_under_way_reads_and_sends_nothing_through_a_replaced_home(w, monkeypatch):
    """A stop whose first try could not end the worker, a part of the report waiting to be sent, and home replaced
    by a link to the ticket's workspace: the next pass goes on with the stop before anything else, reads nothing
    through the link and sends nothing."""
    take(w)
    queue(w)
    report_in_parts(w)
    w.set_engine(lambda s: s["next_rc"].update({"worker-send": 6}))  # the next part waits to be sent
    accepted_with_notes(w, why="STATE: COMPLETE\nVERDICT: FAIL\n1. Finding 1 names no URL.")
    assert json.loads(w.store.run(RID).snapshot)["review"]["sent"] is False
    sends = len([c for c in w.calls("worker-send") if RID in c["argv"]])
    keep_alive(w, session(w))  # the first try to stop cannot end it
    w.store.update_run(RID, stop_requested=1)
    w.tick()
    assert json.loads(w.store.run(RID).snapshot).get("stopping")
    replace_home_with_link(w)
    read = []
    monkeypatch.setattr(w.runner.qa, "_note_progress", lambda run: read.append(run.id) or run)
    w.tick()
    keep_alive(w)
    w.tick()
    run = w.store.run(RID)
    assert run.state == "done" and read == []
    assert len([c for c in w.calls("worker-send") if RID in c["argv"]]) == sends
    assert not (w.cfg.paths.workspace / "artifacts").exists()


def test_a_refused_dispatch_whose_first_stop_tmux_could_not_answer_is_never_dispatched_again(w, monkeypatch):
    """The engine refused, tmux did not answer the first time the session was asked about, the runner restarted,
    and the engine kept no record of the task: the stop goes on (checkouts and refs kept meanwhile), and once tmux
    says the session is gone the run closes with the engine's refusal, never dispatched a second time."""
    engine = w.runner.qa.d.engine
    real = engine.dispatch

    def refused_and_tmux_gone(**kw):
        w.set_engine(lambda s: (s["next_rc"].update({"dispatch": 2}), s.update(tmux_fails=True)))
        return real(**kw)
    with monkeypatch.context() as m:
        m.setattr(engine, "dispatch", refused_and_tmux_gone)
        w.store.add_run("qa", "command", "ops", scenario=1)
        w.tick()
    co = checkout(w)
    run = w.store.run(RID)
    assert run.state == "running" and json.loads(run.snapshot)["stopping"]["why"].startswith(
        "the execution engine did not accept the run")
    assert co.exists() and refs(w, w.repo) != "" and len(dispatches(w)) == 1
    w.restart()
    w.tick()
    assert w.store.run(RID).state == "running" and co.exists() and len(dispatches(w)) == 1
    w.set_engine(lambda s: s.update(tmux_fails=False))
    w.tick()
    run = w.store.run(RID)
    assert run.state == "done" and run.reason.startswith("the execution engine did not accept the run")
    assert len(dispatches(w)) == 1 and not co.exists() and refs(w, w.repo) == ""


def test_a_stop_left_in_preparing_by_an_older_runner_is_seen_through_never_dispatched(w):
    w.store.add_run("qa", "command", "ops", scenario=1)
    w.set_engine(lambda s: s["next_rc"].update({"dispatch": 6}))
    w.tick()
    assert w.store.run(RID).state == "preparing"
    snap = json.loads(w.store.run(RID).snapshot)
    w.store.update_run(RID, snapshot=json.dumps({**snap, "stopping": {"why": "the engine refused it"}}))
    w.tick()
    w.tick()
    run = w.store.run(RID)
    assert run.state == "done" and run.reason == "the engine refused it" and len(dispatches(w)) == 1


def another_task_in_the_folder(w) -> str:
    """A task of the engine's already at work in the test runs' folder (a run before, its worker still live), with
    a home of its own: a branch, something staged, and its checkout in repos."""
    name = session(w)
    w.set_engine(lambda s: (s["tasks"].update({"qa-0": {"task_id": "qa-0", "state": "running", "attempt_live": True,
                                                         "project": str(home(w)), "session": name, "attempts": []}}),
                            s.setdefault("sessions", []).append(name), s.update(busy_by_folder=True)))
    return name


def its_files(w) -> dict:
    """Everything of the folder another worker may be using: the home's branch, HEAD, index, files; the checkouts."""
    h = home(w)
    files = sorted(str(p.relative_to(qa_root(w))) for p in qa_root(w).rglob("*")
                   if ".git" not in p.relative_to(qa_root(w)).parts)
    return {"branch": git(h, "branch", "--show-current"), "head": git(h, "rev-parse", "HEAD"),
            "index": git(h, "diff", "--cached", "--name-only"), "files": files}


def occupied_folder(w) -> str:
    """The folder, claimed, with another task's home and checkout in it and that task's worker alive."""
    h = home(w)
    h.mkdir(parents=True)
    (qa_root(w) / FOLDER_MARK).write_text("ours\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(h)], check=True)
    (h / "AGENTS.md").write_text("theirs\n")
    git(h, "add", "AGENTS.md")
    git(h, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "home")
    git(h, "switch", "-q", "-c", "arceus-qa/0")
    (h / "staged.txt").write_text("staged by the other run\n")
    git(h, "add", "staged.txt")
    (qa_root(w) / "repos" / "acme-platform").mkdir(parents=True)
    (qa_root(w) / "repos" / "acme-platform" / "in-use.txt").write_text("the other run reads this\n")
    return another_task_in_the_folder(w)


def test_another_worker_in_the_folder_keeps_everything_of_it_through_waiting_and_a_stop(w):
    w.store.add_run("qa", "command", "ops", scenario=1)
    other = occupied_folder(w)
    before = its_files(w)
    w.tick()
    w.tick()
    run = w.store.run(RID)
    assert run.state == "pending" and dispatches(w) == []
    assert w.events_named("qa_waits")[-1]["reason"] == "folder_busy"
    assert its_files(w) == before and other in sessions(w) and refs(w, w.repo) == ""
    w.store.update_run(RID, stop_requested=1)
    w.tick()
    assert w.store.run(RID).state == "done" and its_files(w) == before and other in sessions(w) and stops(w) == []


@pytest.mark.parametrize("then", ["retry", "stop", "deadline"])
def test_a_worker_that_took_the_folder_during_the_dispatch_keeps_everything_of_it(w, monkeypatch, then):
    """The folder was free when the run was prepared, and another task's worker took it in the moment of the
    dispatch (busy). From then on nothing of the folder is touched, whether the run asks again, is stopped or runs
    out of time, until that worker has gone."""
    engine = w.runner.qa.d.engine
    real = engine.dispatch

    def taken_meanwhile(**kw):
        another_task_in_the_folder(w)
        return real(**kw)
    with monkeypatch.context() as m:
        m.setattr(engine, "dispatch", taken_meanwhile)
        w.store.add_run("qa", "command", "ops", scenario=1)
        w.tick()
    run = w.store.run(RID)
    assert run.state == "preparing" and json.loads(run.snapshot)["asked"] is False
    other = session(w)
    before = its_files(w)
    n = len(dispatches(w))
    if then == "stop":
        w.store.update_run(RID, stop_requested=1)
    elif then == "deadline":
        w.now += w.cfg.qa.timeout + timedelta(minutes=1)
    w.tick()
    w.tick()
    assert w.store.run(RID).state == "preparing" and len(dispatches(w)) == n
    assert its_files(w) == before and other in sessions(w) and stops(w) == []
    # The other worker goes: now the run goes on (stopped, out of time, or dispatched).
    w.set_engine(lambda s: (s["sessions"].remove(other), s["tasks"].pop("qa-0")))
    w.tick()
    run = w.store.run(RID)
    if then == "retry":
        assert run.state == "running" and len(dispatches(w)) == n + 1
    else:
        assert run.state == "done" and len(dispatches(w)) == n


def test_a_home_git_sharing_the_tickets_metadata_through_commondir_is_never_used(w):
    take(w)
    dirty_ticket(w)
    queue(w)
    (home(w) / ".git" / "commondir").write_text(str(w.repo / ".git") + "\n")
    before = ticket_git(w)
    config = git(w.repo, "config", "--list", "--local")
    finish(w)
    run = w.store.run(RID)
    assert run.state == "done" and "no longer the runner's folder" in run.reason
    assert ticket_git(w) == before and git(w.repo, "config", "--list", "--local") == config


def test_a_home_git_with_commondir_is_refused_when_a_run_is_prepared(w):
    take(w)
    home(w).mkdir(parents=True)
    (qa_root(w) / FOLDER_MARK).write_text("ours\n")
    subprocess.run(["git", "init", "-q", str(home(w))], check=True)
    (home(w) / ".git" / "commondir").write_text(str(w.repo / ".git") + "\n")
    before = ticket_git(w)
    queue(w)
    run = w.store.run(RID)
    assert (run.state, run.outcome) == ("done", "unfinished") and ".git" in run.reason
    assert ticket_git(w) == before and dispatches(w) == []


def test_a_deadline_that_passes_inside_a_busy_dispatch_waits_for_the_other_worker_and_never_asks_again(w, monkeypatch):
    """The run's time runs out while the engine is being asked, and the engine answers busy because another task's
    worker took the folder meanwhile: that worker's home, index, files and checkouts stay until it has gone; then
    the run closes as unable to start, never dispatched again."""
    engine = w.runner.qa.d.engine
    real = engine.dispatch

    def late_and_taken(**kw):
        another_task_in_the_folder(w)
        w.now += w.cfg.qa.timeout + timedelta(minutes=1)
        return real(**kw)
    with monkeypatch.context() as m:
        m.setattr(engine, "dispatch", late_and_taken)
        w.store.add_run("qa", "command", "ops", scenario=1)
        w.tick()
    run = w.store.run(RID)
    assert run.state == "preparing" and json.loads(run.snapshot)["asked"] is False
    other = session(w)
    before = its_files(w)
    n = len(dispatches(w))
    w.tick()
    w.tick()
    assert w.store.run(RID).state == "preparing" and its_files(w) == before and other in sessions(w)
    assert len(dispatches(w)) == n and stops(w) == []
    w.set_engine(lambda s: (s["sessions"].remove(other), s["tasks"].pop("qa-0")))
    w.tick()
    run = w.store.run(RID)
    assert run.state == "done" and "could not start" in run.reason and len(dispatches(w)) == n
    assert refs(w, w.repo) == ""
