"""Test runs of the platform (qa.py) in the console: the test scenarios (qa_scenarios.py) with their schedules and
instructions, the list of runs, a run's report with its screenshots, starting a scenario's run and stopping one.

Only the report's own files are served, as the runner's old page did (qa_report.resolve): a run's media by its
exact name. Not the copy of the instructions kept with a run (it holds the test accounts), not result.json, not any
other path. The report is rendered from the run's result.json, the same view report.html was written from."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from starlette.responses import FileResponse, Response
from starlette.routing import Route

from arceus_runner import qa as qa_mod
from arceus_runner import qa_report, qa_scenarios
from arceus_runner.console import files, runs, views_instructions
from arceus_runner.console.store import ConsoleError
from arceus_runner.console.web import Ctx, Gone, as_number, page
from arceus_runner.db import RUN_UNDER_WAY, AgentRun, ReviewStore
from arceus_runner.workspace_run import MAX_INSTRUCTIONS

SECTIONS = ("/qa/",)


def _cfg(ctx: Ctx):
    cfg, problems = ctx.config()
    if cfg is None:
        raise ConsoleError("config.yaml has errors: " + "; ".join(problems[:3]))
    return cfg


def _n(ctx: Ctx) -> int:
    return ctx.number("n")


def _scenario_rows(ctx: Ctx, store: ReviewStore, cfg) -> list[dict]:
    """Each scenario as the list shows it: its schedule in words, when it is next due, its run queued or under way,
    if any, and what is wrong with its instructions file, if anything (a run would stop on it, or it was changed on
    the server)."""
    busy: dict[int, AgentRun] = {}
    for r in store.runs("qa", *RUN_UNDER_WAY):
        busy.setdefault(r.scenario, r)
    now = datetime.now(timezone.utc)
    rows = []
    for sc in store.scenarios():
        plan = qa_scenarios.plan_of(sc)
        due = qa_scenarios.next_time(plan, now, cfg.schedule.tz)
        rows.append({"n": sc.n, "name": sc.name, "when": qa_scenarios.describe(plan), "on": plan.active,
                     "next": f"{due:%a %d %b, %H:%M}" if due else "", "run": busy.get(sc.n),
                     "warn": views_instructions.warning(ctx, qa_scenarios.history_kind(sc),
                                                        qa_scenarios.file_of(cfg.qa, sc), required=True)})
    return rows


def index(ctx: Ctx) -> Response:
    cfg, problems = ctx.config()
    if cfg is None:
        return ctx.render("message.html", status=503, title="Test runs",
                          message="config.yaml has errors, so the runs cannot be found: " + "; ".join(problems[:3]))
    error = ""
    if ctx.request.method == "POST":
        user = ctx.require("qa.run")
        n = as_number(ctx.field("scenario"))
        try:
            if n is None and cfg.qa.enabled:
                raise ConsoleError("Pick the scenario to run: nothing was queued.")
            run_n = runs.start(cfg, "qa", user, scenario=n or 0)
            ctx.store.log(user, user.label, "qa_run_queued", f"qa-{run_n}", {"scenario": n})
            return ctx.redirect("/qa/", f"Run {run_n} queued. The runner starts it once the workspace is free.")
        except ConsoleError as e:
            error = str(e)
    try:
        store = ReviewStore.read_only(cfg.paths.db)
    except (FileNotFoundError, RuntimeError) as e:
        rows, scenarios, db_error = [], [], str(e)
    else:
        try:
            rows, db_error = qa_mod.index_rows(store, cfg.qa.reports), ""
            scenarios = _scenario_rows(ctx, store, cfg)
        finally:
            store.close()
    return ctx.render("runs.html", status=400 if error else 200, title="Test runs", kind="qa",
                      rows=runs.dress(rows, qa_report.head_of),
                      enabled=cfg.qa.enabled, error=error or db_error, notice=ctx.query("notice"),
                      mode=cfg.mode, can_run=ctx.can("qa.run"), can_edit=ctx.can("qa.instructions"),
                      scenarios=scenarios, max_scenarios=cfg.qa.max_scenarios, timezone=cfg.schedule.timezone,
                      db_ok=not db_error)


def run(ctx: Ctx) -> Response:
    """The run's report once it is written; until then (and for a finished run without one) where the run is."""
    n = _n(ctx)
    return runs.report_page(ctx, _cfg(ctx), "qa", n, qa_report.run_body)


def live(ctx: Ctx) -> Response:
    n = _n(ctx)
    return runs.report_live(ctx, _cfg(ctx), "qa", n)


def stop(ctx: Ctx) -> Response:
    user = ctx.require("qa.run")
    n = _n(ctx)
    try:
        runs.stop(_cfg(ctx), "qa", n)
    except ConsoleError as e:
        return ctx.redirect("/qa/", str(e))
    ctx.store.log(user, user.label, "qa_run_stop_asked", f"qa-{n}")
    return ctx.redirect("/qa/", f"Run {n}: asked to stop. The runner stops it on its next pass and writes its report.")


def media(ctx: Ctx) -> Response:
    n = _n(ctx)
    cfg = _cfg(ctx)
    root = cfg.qa.reports.resolve()
    found = qa_report.resolve(root, f"/{n}/media/{ctx.param('file')}")
    if found is None:
        raise Gone()
    path, kind = found
    if path.is_symlink() or not path.is_file() or root not in path.resolve().parents:
        raise Gone()
    return FileResponse(path, media_type=kind, headers={"Cache-Control": "private, max-age=86400"})


# -- the scenarios (qa_scenarios.py) -------------------------------------------------------------------------------

def _form(ctx: Ctx) -> dict:
    return {"name": ctx.field("name"), "days": ctx.fields("day"), "at": ctx.field("at")}


def _scenario(store: ReviewStore, ctx: Ctx):
    sc = store.scenario(ctx.number("s"))
    if sc is None or sc.deleted_at:
        raise Gone()
    return sc


def _values(sc) -> dict:
    plan = qa_scenarios.plan_of(sc)
    return {"name": sc.name, "days": list(plan.days), "at": f"{plan.at:%H:%M}" if plan.at else ""}


def _writable(cfg) -> ReviewStore:
    try:
        return ReviewStore.existing(cfg.paths.db)
    except (FileNotFoundError, RuntimeError) as e:
        raise ConsoleError(f"Not saved: {e}") from None


def _render_form(ctx: Ctx, cfg, *, status: int = 200, sc=None, values: dict, error: str = "", text: str = "",
                 version: int | None = None, saved_now: str = "") -> Response:
    title = f"Scenario \u201c{sc.name}\u201d" if sc else "New test scenario"
    return ctx.render("scenario.html", status=status, title=title, sc=sc, v=values, error=error,
                      notice=ctx.query("notice"), text=text, version=version if version is not None else
                      (sc.version if sc else 0), saved_now=saved_now, days=qa_scenarios.DAYS,
                      day_words=qa_scenarios.DAY_WORDS, timezone=cfg.schedule.timezone, limit=MAX_INSTRUCTIONS,
                      max_name=qa_scenarios.MAX_NAME,
                      path=qa_scenarios.file_of(cfg.qa, sc) if sc else None)


def scenario_new(ctx: Ctx) -> Response:
    """A new scenario: its name, when it runs and what it tests, saved together."""
    user = ctx.require("qa.instructions")
    cfg = _cfg(ctx)
    if ctx.request.method != "POST":
        return _render_form(ctx, cfg, values={"name": "", "days": [], "at": ""})
    values, text = _form(ctx), ctx.field("text")
    try:
        body = files.normal(text)
        given = re.sub(r"<!--.*?-->", "", body, flags=re.S).strip()
        if not given:
            raise qa_scenarios.Invalid("Write what the scenario tests: a run without instructions stops at once.")
        if len(given.encode()) > MAX_INSTRUCTIONS:
            raise qa_scenarios.Invalid(f"The instructions are larger than {MAX_INSTRUCTIONS // 1024} KB without "
                                       "comments.")
        store = _writable(cfg)
        try:
            with store.transaction():
                sc = qa_scenarios.create(store, cfg.qa, values["name"], values["days"], values["at"], user.email)
                path = qa_scenarios.file_of(cfg.qa, sc)
                # A file left at this name by a save whose scenario was never committed is replaced, its text kept
                # in the history.
                files.save(ctx.store, qa_scenarios.history_kind(sc), path, body, files.read(path).sha, user,
                           "Scenario created")
        finally:
            store.close()
    except qa_scenarios.Invalid as e:
        return _render_form(ctx, cfg, status=400, values=values, error=str(e), text=text)
    except files.Conflict:
        return _render_form(ctx, cfg, status=409, values=values, text=text,
                            error="The instructions file changed while saving: nothing was created. Save again.")
    except (ConsoleError, OSError) as e:
        why = str(e) if isinstance(e, ConsoleError) else f"Not saved: {e.strerror or e}"
        return _render_form(ctx, cfg, status=400, values=values, error=why, text=text)
    ctx.store.log(user, user.label, "qa_scenario_created", f"scenario-{sc.n}", {"name": sc.name})
    return ctx.redirect("/qa/", f"Scenario \u201c{sc.name}\u201d created: "
                        + qa_scenarios.describe(qa_scenarios.plan_of(sc)).lower() + ".")


def scenario_edit(ctx: Ctx) -> Response:
    """A scenario's name and schedule. A save over a scenario changed since the page was opened is refused."""
    user = ctx.require("qa.instructions")
    cfg = _cfg(ctx)
    try:
        store = _writable(cfg) if ctx.request.method == "POST" else ReviewStore.read_only(cfg.paths.db)
    except (FileNotFoundError, RuntimeError) as e:
        raise ConsoleError(str(e)) from None
    try:
        sc = _scenario(store, ctx)
        if ctx.request.method != "POST":
            return _render_form(ctx, cfg, sc=sc, values=_values(sc))
        values = _form(ctx)
        version = as_number(ctx.field("version")) or 0
        try:
            name, days, at, on = qa_scenarios.clean(values["name"], values["days"], values["at"])
            with store.transaction():
                if qa_scenarios.name_taken(store, name, but=sc.n):
                    raise qa_scenarios.Invalid(f"A scenario named \u201c{name}\u201d exists already.")
                changed = store.update_scenario(sc.n, version, user.email, name=name, days=days, at=at, scheduled=on)
        except qa_scenarios.Invalid as e:
            return _render_form(ctx, cfg, status=400, sc=sc, values=values, error=str(e), version=version)
        if changed is None:
            now = store.scenario(sc.n)
            if now is None or now.deleted_at:
                raise Gone()
            return _render_form(ctx, cfg, status=409, sc=now, values=values, version=now.version,
                                saved_now=f"\u201c{now.name}\u201d, "
                                + qa_scenarios.describe(qa_scenarios.plan_of(now)).lower(),
                                error="Someone changed this scenario since you opened it. Your changes are still in "
                                      "the form; save again to replace theirs.")
    finally:
        store.close()
    ctx.store.log(user, user.label, "qa_scenario_changed", f"scenario-{sc.n}",
                  {"name": changed.name, "days": changed.days, "at": changed.at, "scheduled": changed.scheduled})
    return ctx.redirect("/qa/", f"Scenario \u201c{changed.name}\u201d saved: "
                        + qa_scenarios.describe(qa_scenarios.plan_of(changed)).lower() + ".")


def scenario_delete(ctx: Ctx) -> Response:
    user = ctx.require("qa.instructions")
    cfg = _cfg(ctx)
    try:
        store = _writable(cfg)
    except ConsoleError as e:
        return ctx.redirect("/qa/", str(e))
    try:
        sc = _scenario(store, ctx)
        if not store.delete_scenario(sc.n, as_number(ctx.field("version")) or 0, user.email):
            return ctx.redirect(f"/qa/scenarios/{sc.n}/", "Someone changed this scenario since you opened it: it "
                                "was not deleted. Look at it again first.")
    finally:
        store.close()
    ctx.store.log(user, user.label, "qa_scenario_deleted", f"scenario-{sc.n}", {"name": sc.name})
    return ctx.redirect("/qa/", f"Scenario \u201c{sc.name}\u201d deleted. Its past runs and reports stay.")


def scenario_instructions(ctx: Ctx) -> Response:
    cfg = _cfg(ctx)
    try:
        store = ReviewStore.read_only(cfg.paths.db)
    except (FileNotFoundError, RuntimeError) as e:
        raise ConsoleError(str(e)) from None
    try:
        sc = _scenario(store, ctx)
    finally:
        store.close()
    return runs.file_page(
        ctx, kind=qa_scenarios.history_kind(sc), action="qa.instructions", path=qa_scenarios.file_of(cfg.qa, sc),
        title=f"Instructions: {sc.name}",
        lede="What a run of this scenario is told: the platform's address, its test accounts and what to test. Each "
             "run reads it when it starts and keeps a copy, so a change never alters a run already made.",
        back=(f"/qa/scenarios/{sc.n}/", sc.name), required=True, sensitive=True)


def instructions(ctx: Ctx) -> Response:
    """Where the one instructions file was edited before there were scenarios: the first scenario's, if it is still
    there."""
    ctx.require("qa.instructions")
    cfg = _cfg(ctx)
    try:
        store = ReviewStore.read_only(cfg.paths.db)
    except (FileNotFoundError, RuntimeError):
        return ctx.redirect("/qa/")
    try:
        first = next((sc for sc in store.scenarios() if not sc.own_file), None)
    finally:
        store.close()
    return ctx.redirect(f"/qa/scenarios/{first.n}/instructions/" if first else "/qa/")


def routes() -> list[Route]:
    return [Route("/qa/", page(index), methods=["GET", "POST"]),
            Route("/qa/instructions/", page(instructions)),
            Route("/qa/scenarios/new", page(scenario_new), methods=["GET", "POST"]),
            Route("/qa/scenarios/{s}/", page(scenario_edit), methods=["GET", "POST"]),
            Route("/qa/scenarios/{s}/delete", page(scenario_delete), methods=["POST"]),
            Route("/qa/scenarios/{s}/instructions/", page(scenario_instructions), methods=["GET", "POST"]),
            Route("/qa/{n}/", page(run)),
            Route("/qa/{n}/live", page(live, api=True)),
            Route("/qa/{n}/stop", page(stop), methods=["POST"]),
            Route("/qa/{n}/media/{file}", page(media))]
