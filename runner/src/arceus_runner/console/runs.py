"""The background agents' runs from the console: a test run of the platform (qa), an architecture review (arch) or
a triage run of the advisories (secops). Queuing and stopping one is what `arceus-runner qa|arch run|stop` does on the
server, with the person's email as who asked; the runner does the rest on its next pass. Their instructions are edited
with files.py. A triage run is only read here: it is started and stopped on the server."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

from starlette.responses import HTMLResponse, Response

from arceus_runner import secops
from arceus_runner.config import Config
from arceus_runner.console import auth, files, status
from arceus_runner.console import execution as ex
from arceus_runner.console.store import ConsoleError, User, parse_iso
from arceus_runner.console.web import Ctx, Denied, Gone, Markup, as_number
from arceus_runner.db import RUN_HOLDS, RUN_UNDER_WAY, AgentRun, ReviewStore
from arceus_runner.qa import scenario_name
from arceus_runner.workspace_run import CONSOLE_PATHS, MAX_INSTRUCTIONS, instructions_text, read_instructions

KINDS = {"qa": ("qa", "test run", "Test runs"), "arch": ("architect", "architecture review", "Architecture reviews"),
         "secops": ("secops", "triage run", "Advisory triage")}


STATE_TEXT = {"pending": "Queued", "preparing": "Starting", "running": "Running", "reviewing": "Second check",
              "restoring": "Putting the workspace back", "publishing": "Writing the report"}
# A kind whose run does something else in a state: a triage run sends its verdicts to the register before its report
# is written, and may wait an hour for it (secops._published).
OWN_STATE_TEXT = {("secops", "publishing"): "Sending the verdicts"}
# The result's colour, as each kind's report has it (a triage run's: secops.OUTCOME_CLASS).
RESULT_CLASS = {"passed": "passed", "clean": "passed", "found": "found", "partial": "partial", "blocked": "blocked",
                "unfinished": "unfinished", "stopped": "stopped", "unchanged": "stopped", **secops.OUTCOME_CLASS}


def dress(rows: list[dict], head_of, kind: str = "") -> list[dict]:
    """The list's rows with their words: under way, the step; done, the result as the reports word it."""
    for r in rows:
        r["state_text"] = OWN_STATE_TEXT.get((kind, r["state"])) or STATE_TEXT.get(r["state"], r["state"])
        # Only the kinds whose rows say so tell head_of the report was disputed (qa.index_rows).
        extra = {"disputed": True} if r.get("disputed") else {}
        r["result_text"] = head_of(r.get("outcome"), r.get("findings") or 0, **extra) if r["state"] == "done" else ""
        r["result_class"] = RESULT_CLASS.get(r.get("outcome") or "", "")
    return sorted(rows, key=lambda r: r["n"], reverse=True)


def section(cfg: Config, kind: str):
    return getattr(cfg, KINDS[kind][0])


def start(cfg: Config, kind: str, user: User, scenario: int = 0) -> int:
    """Queue a run (of this test scenario); returns its number. Refused while one of the kind (of the same scenario)
    is queued or under way, or the kind is off. Runs of different scenarios queue behind each other."""
    name, noun, _ = KINDS[kind]
    if not section(cfg, kind).enabled:
        raise ConsoleError(f"{noun.capitalize()}s are off in config.yaml ({name}.enabled: false): nothing was queued.")
    try:
        store = ReviewStore.existing(cfg.paths.db)
    except (FileNotFoundError, RuntimeError) as e:
        raise ConsoleError(f"Nothing was queued: {e}") from None
    try:
        try:
            run, busy = store.queue_run(kind, "command", user.email, scenario=scenario)
        except LookupError:
            raise ConsoleError("That scenario no longer exists: nothing was queued.") from None
    finally:
        store.close()
    if busy is not None:
        of = " of this scenario" if kind == "qa" else ""
        raise ConsoleError(f"Run {busy.n}{of} is {'queued' if busy.state == 'pending' else 'under way'}: one at a "
                           "time. Stop it first to start another.")
    assert run is not None
    return run.n


def stop(cfg: Config, kind: str, n: int) -> None:
    try:
        store = ReviewStore.existing(cfg.paths.db)
    except (FileNotFoundError, RuntimeError) as e:
        raise ConsoleError(f"Nothing was stopped: {e}") from None
    try:
        run = store.run(f"{kind}-{n}")
        if run is None or run.state not in RUN_UNDER_WAY:
            raise ConsoleError(f"Run {n} is not queued or under way.")
        if not run.stop_requested:
            store.update_run(run.id, stop_requested=1)
    finally:
        store.close()


Verdict = tuple[str | None, str]  # what the agent gets, or None and why it cannot be given it


def file_page(ctx: Ctx, *, kind: str, action: str, path: Path, title: str, lede: str, back: tuple[str, str],
              required: bool, saved_to: str = "", sensitive: bool = False, limit: int = MAX_INSTRUCTIONS,
              reads: str = "", reader: Callable[[], Verdict] | None = None, check: Callable[[str], Verdict] | None = None) -> Response:
    """The page that shows and edits one of the factory's text files, for the people `action` allows. The file is
    judged as its runner reads it (`reader`), a draft by the same rule (`check`): a draft it refuses is never written,
    so the console cannot save what would stop the next run. A save leads with its notice to `saved_to`, or back to
    the editor when there is none."""
    if not ctx.can(action):
        raise Denied(action)
    user = ctx.user
    assert user is not None
    reader = reader or (lambda: read_instructions(path, required=required))
    check = check or (lambda t: instructions_text(t, path, required))
    current = files.read(path)
    text, sha, error, notice, conflict = current.text, current.sha, current.problem, "", ""
    draft = ctx.request.method == "POST"
    if draft:
        text = ctx.field("text")
        # A text sent back keeps the version the person opened: a change made meanwhile is still a conflict on the
        # next save, never replaced unseen.
        sha = ctx.field("sha")
        agent_gets, why = check(files.normal(text))
        try:
            if agent_gets is None:
                error = f"Not saved: {why}. Your text is still in the editor."
            elif files.save(ctx.store, kind, path, text, sha, user, ctx.field("note")):
                said = "Saved. " + (reads or "A run that starts from now on reads this version.")
                return ctx.redirect(saved_to or ctx.path, said)
            else:
                notice = "Nothing changed: the file is already this text."
        except files.Conflict as c:
            sha = c.current.sha
            if c.current.exists and c.current.text == files.normal(text):
                notice = "Nothing changed: the file is already this text."
            else:
                conflict = c.diff
                error = ("Someone changed the file since you opened it. The difference between what is on the server "
                         "now and your text is below; your text is still in the editor. Save again to replace "
                         "theirs.")
        except OSError as e:
            error = f"Not saved: {e.strerror or e}"
            if getattr(e, "errno", None) == 30:  # read-only file system: a folder mounted read-only
                error = (f"Not saved: {path} is outside the data folder, which the console sees read-only. Edit it "
                         "on the server, or keep it in the data folder.")
    restoring = None
    if ctx.request.method == "GET" and (n := as_number(ctx.query("version"))) is not None:
        restoring = ctx.store.version_of(kind, n)
        # ?before=1: the text that version replaced (the file before the first save here, or an edit on the server).
        if restoring and ctx.query("before") and restoring["before_text"] is not None:
            restoring = {**restoring, "replaced": True}
            text, draft = restoring["before_text"], True
        elif restoring:
            text, draft = restoring["after_text"], True
    # What the agent would get from the text in the editor: the file's, or a draft's (sent back, or a version opened).
    agent_gets, why = check(files.normal(text)) if draft else reader()
    agent_bytes = len((agent_gets or "").rstrip("\n").encode())  # as the runner counts it against `limit`
    versions = ctx.store.versions(kind)
    return ctx.render("file_edit.html", status=409 if conflict else 400 if error else 200,
                      title=title, lede=lede, path=path, text=text, sha=sha, exists=current.exists, error=error,
                      notice=notice or (ctx.query("notice") if ctx.request.method == "GET" else ""),
                      conflict=conflict, back=back, restoring=restoring,
                      versions=versions, outside=changed_outside(versions, current), note=ctx.field("note"),
                      agent_ok=agent_gets is not None and bool(agent_gets.strip()),
                      agent_why=why, agent_bytes=agent_bytes, limit=limit, left=limit - agent_bytes, draft=draft,
                      reads=reads, required=required, sensitive=sensitive,
                      readonly=bool(current.problem) or not files.writable(path))


def changed_outside(versions: list[dict], current: files.Current) -> dict | None:
    """The last version saved here, with the difference to the file, when the file is no longer that text: someone
    changed it on the server (over SSH, a copy put in place). The file is what the agent gets; the history has that
    text only once the next save here keeps it as what it replaced."""
    if not versions or current.problem or current.sha == versions[0]["after_sha"]:
        return None
    return {**versions[0], "diff": files.diff(versions[0]["after_text"], current.text if current.exists else "",
                                              "last saved here", "the file now")}


def _epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        at = parse_iso(value)
    except ValueError:
        return None
    return at.timestamp() if at.tzinfo else None


def clock(run: AgentRun, now: float) -> dict:
    """A run's time as the runner keeps it: how long it waited and has been going (or took), and the times it was
    given when it was prepared (`test_until`, `deadline`), with what is left of each. Nothing is worked out from
    config.yaml: a run prepared before runs kept their times shows none."""
    created, started, finished = _epoch(run.created_at), _epoch(run.started_at), _epoch(run.finished_at)
    until, deadline = _epoch(run.test_until), _epoch(run.deadline)
    end = finished if run.state == "done" and finished else now
    out: dict = {"queued_for": started - created if started and created else None,
                 "elapsed": end - started if started else None, "test_until": run.test_until if until else None,
                 "deadline": run.deadline if deadline else None, "testing_left": None, "left": None,
                 "used": None, "level": ""}
    if started and deadline and deadline > started:
        out["used"] = max(0, min(100, int((end - started) * 100 / (deadline - started))))
        out["given"] = deadline - started
    if run.state != "done":
        if until:
            out["testing_left"] = until - now
        if deadline:
            out["left"] = deadline - now
    if deadline and end >= deadline:
        out["level"] = "bad"
    elif until and end >= until:
        out["level"] = "warn"
    return out


def plan(run: AgentRun) -> dict | None:
    """A test run's plan as the runner last counted it (qa.plan_progress), or None when it has not looked yet."""
    try:
        p = json.loads(run.progress or "null")
    except ValueError:
        return None
    if not isinstance(p, dict) or p.get("state") not in ("none", "unreadable", "shape", "ok"):
        return None
    nums = {k: p.get(k) if isinstance(p.get(k), int) else 0
            for k in ("total", "open", "ok", "problem", "blocked", "skipped", "other")}
    done = max(0, nums["total"] - nums["open"])
    planned = p.get("planned") if isinstance(p.get("planned"), int) else None
    return {"state": p["state"], **nums, "done": done,
            "pct": done * 100 // nums["total"] if nums["total"] else 0,
            "planned": planned if planned is not None and planned != nums["total"] else None,
            "planned_at": p.get("planned_at") or "", "seen_at": p.get("seen_at") or "", "seen": _epoch(p.get("seen_at")),
            "cut": bool(p.get("cut"))}


# What a running run is doing, by whose turn the engine says it is (execution.live): only what is confirmed.
TURN_WORDS = {"agent": "Agent working", "checks": "Checks: the engine's gate or Codex has the turn",
              "paused": "Waiting for the engine"}


def stage(run: AgentRun, live_view: dict | None) -> str:
    """What a run is doing: its state, and while it runs, whose turn the engine reports, with the report part the
    runner has under way when the report comes in parts. Nothing is read from the plan or the clock: a plan whose
    checks are all marked does not show what the agent is doing now, and neither does the time it was told to stop."""
    words = ex.RUN_STATE_WORDS.get(run.state, run.state)
    if run.state != "running":
        return words
    turn = (live_view or {}).get("turn") or ""
    words = TURN_WORDS.get(turn, "Running: the engine's step is not known yet")
    try:
        rv = json.loads(run.snapshot or "{}").get("review") or {}
    except (ValueError, AttributeError):
        rv = {}
    parts = rv.get("parts") if isinstance(rv, dict) else None
    if isinstance(parts, list) and len(parts) > 1:
        again = rv.get("again")
        k = parts.index(again) + 1 if again in parts else rv.get("part", 1)
        if isinstance(k, int) and 1 <= k <= len(parts):
            words += f" (report in {len(parts)} parts, on part {k}" + (", read again)" if again in parts else ")")
    return words


def waits_for(cfg: Config, store: ReviewStore, kind: str) -> str:
    """What a queued run of `kind` waits for, in words, or '': a run of its kind under way, wherever it works, and,
    unless test runs have a folder of their own (qa.workspace), whatever holds the shared workspace."""
    mine = store.run_holding(kind) or store.run_apart(kind)
    words = f"{status.RUN_WORDS[kind][0]}, run {mine.n}" if mine else ""
    if kind == "qa" and cfg.qa.workspace is not None:
        return words
    return ex.holder_words(store) or words


def run_view(cfg: Config, kind: str, n: int, now: float | None = None) -> dict | None:
    """A run's page before its report (and when a finished run has none): where it is, why it waits, the live figures,
    as a task's page shows them (execution.py). None when there is no such run."""
    now = now or time.time()
    try:
        store = ReviewStore.read_only(cfg.paths.db)
    except (FileNotFoundError, RuntimeError) as e:
        raise ConsoleError(str(e)) from None
    try:
        run = store.run(f"{kind}-{n}")
        if run is None:
            return None
        m = status.read_metrics(cfg)
        runner = status.runner_status(m, now)
        running = run.state == "running"
        view = ex.live(m, run.id, now, ex.observed_stale_after(cfg)) if running else None
        holder = "" if run.state in RUN_HOLDS else waits_for(cfg, store, kind)
        facts = ex.Facts(state=f"run:{run.state}", turn=(view or {}).get("turn", ""),
                         used_up=ex.used_up(m, cfg.schedule.tz) if running else [],
                         holder=holder, mode=m.mode or cfg.mode,
                         can_take=m.can_take,
                         drain_by=ex.drain_by(store), reason=run.reason or "", stop_asked=bool(run.stop_requested))
        why = ex.reason(facts)
        own = OWN_STATE_TEXT.get((kind, run.state))
        if own:
            why["title"] = own
        if run.state == "done":
            # Finished, and this page is shown because its report is not there: said as such, never as a result.
            why = {"code": "no_report", "tone": "bad", "title": "Finished, but its report is missing",
                   "detail": (f"The run ended ({(run.outcome or 'no outcome').replace('_', ' ')}) and the console "
                              "finds no report for it." + (f" {run.reason}" if run.reason else ""))}
        noun = status.RUN_WORDS[kind][0]
        plan_view = plan(run) if kind == "qa" else None
        scenario = scenario_name(store, run) if kind == "qa" else ""
        return {"kind": kind, "n": n, "id": run.id, "title": f"{noun}, run {n}", "state": run.state,
                "words": own or stage(run, view), "why": why, "runner": runner, "live": view,
                "clock": clock(run, now), "plan": plan_view, "scenario": scenario,
                "fresh": ex.freshness(runner, running, view, now), "stop_asked": bool(run.stop_requested),
                "requested_by": run.requested_by, "source": run.source, "created_at": run.created_at,
                "started_at": run.started_at or "", "finished_at": run.finished_at or "", "updated_at": run.updated_at,
                "base": CONSOLE_PATHS[kind], "list": KINDS[kind][2], "now": now}
    finally:
        store.close()


def _can_stop(ctx: Ctx, view: dict) -> bool:
    action = f"{view['kind']}.run"  # none for a triage run: it is stopped on the server
    return action in auth.ACTIONS and ctx.can(action) and view["state"] in RUN_UNDER_WAY and not view["stop_asked"]


def status_page(ctx: Ctx, view: dict) -> Response:
    return ctx.render("run_status.html", title=view["title"], r=view, ago=status.ago, duration=ex.duration,
                      can_stop=_can_stop(ctx, view))


def status_live(ctx: Ctx, view: dict) -> Response:
    """The part of a run's page that follows the server. The stop button is in it, so it goes once the run is over
    or someone has asked to stop it."""
    html = ctx.app.templates.get_template("run_live.html").render(ctx=ctx, user=ctx.user, r=view, ago=status.ago,
                                                                 duration=ex.duration, csrf=ctx.csrf,
                                                                 can_stop=_can_stop(ctx, view))
    return HTMLResponse(html)


# -- a report written as result.json (a test run's, a triage run's) -------------------------------------------------

def report_file(cfg: Config, kind: str, n: int) -> Path | None:
    """The run's result.json, or None while it has none (queued, under way, or finished without one)."""
    result = section(cfg, kind).reports / str(n) / "result.json"
    if result.is_symlink():
        raise Gone()
    return result if result.is_file() else None


def report_page(ctx: Ctx, cfg: Config, kind: str, n: int, run_body) -> Response:
    """The run's report once it is written, rendered from its result.json, the same view report.html was written from
    (`run_body`: the kind's title and body); until then, and for a finished run without one, where the run is."""
    try:
        result = report_file(cfg, kind, n)
    except OSError:
        result = None
    if result is None:
        view = run_view(cfg, kind, n)
        if view is None:
            raise Gone()
        return status_page(ctx, view)
    try:
        view = json.loads(result.read_text(encoding="utf-8"))
    except (FileNotFoundError, NotADirectoryError):
        raise Gone() from None
    except (OSError, ValueError):
        return ctx.render("message.html", status=500, title=f"Run {n}",
                          message=f"The run's report cannot be read ({result}).")
    title, body = run_body(view, cfg.project, cfg.schedule.tz)
    return ctx.render("report.html", title=title, body=Markup(body))


def report_live(ctx: Ctx, cfg: Config, kind: str, n: int) -> Response:
    view = run_view(cfg, kind, n)
    if view is None:
        raise Gone()
    try:
        view["report_ready"] = report_file(cfg, kind, n) is not None
    except OSError:
        pass
    return status_live(ctx, view)
