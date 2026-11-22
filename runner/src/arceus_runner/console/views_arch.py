"""Architecture reviews (architect.py) in the console: the list, a review's report, every finding kept, people's
decisions on them, starting and stopping a review, and the review instructions.

Each finding shows the second check (Codex's verdict) apart from the person's decision, who took it, when and why,
and every decision on it before. Only architects and admins decide; the decision carries the version of the finding
the page showed, so two people deciding at once do not overwrite each other (architect.decide)."""

from __future__ import annotations

import re
import urllib.parse

from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from arceus_runner import arch_report
from arceus_runner import architect as architect_mod
from arceus_runner.console import links, runs, views_instructions
from arceus_runner.console.store import ConsoleError
from arceus_runner.console.web import Ctx, Gone, Markup, page
from arceus_runner.db import ReviewStore

SECTIONS = ("/architecture/",)
_BACK = re.compile(r"^/architecture/(?:\d{1,9}/|findings/)(?:#f\d{1,9})?$")


def _cfg(ctx: Ctx):
    cfg, problems = ctx.config()
    if cfg is None:
        raise ConsoleError("config.yaml has errors: " + "; ".join(problems[:3]))
    return cfg


def _read(cfg):
    try:
        return ReviewStore.read_only(cfg.paths.db)
    except (FileNotFoundError, RuntimeError) as e:
        raise ConsoleError(str(e)) from None


def _forms(ctx: Ctx) -> arch_report.Forms:
    return arch_report.Forms(token=ctx.csrf, field="csrf", prefix="/architecture", can_decide=ctx.can("arch.decide"),
                             when=ctx.app.when)


def index(ctx: Ctx) -> Response:
    cfg = _cfg(ctx)
    error = ""
    if ctx.request.method == "POST":
        user = ctx.require("arch.run")
        try:
            n = runs.start(cfg, "arch", user)
            ctx.store.log(user, user.label, "arch_run_queued", f"arch-{n}")
            return ctx.redirect("/architecture/", f"Review {n} queued. The runner starts it once the workspace is free.")
        except ConsoleError as e:
            error = str(e)
    try:
        store = ReviewStore.read_only(cfg.paths.db)
    except (FileNotFoundError, RuntimeError) as e:
        rows, ledger, db_error = [], [], str(e)
    else:
        try:
            rows, ledger, db_error = architect_mod.index_rows(store), architect_mod.ledger_rows(store), ""
        finally:
            store.close()
    open_, decided, fixed = arch_report._ledger_counts(ledger)
    return ctx.render("arch_runs.html", status=400 if error else 200, title="Architecture reviews", kind="arch",
                      rows=runs.dress(rows, arch_report.head_of), enabled=cfg.architect.enabled,
                      error=error or db_error, notice=ctx.query("notice"), mode=cfg.mode,
                      can_run=ctx.can("arch.run"), can_edit=ctx.can("arch.instructions"),
                      schedule=cfg.architect.schedule, counts=(open_, decided, fixed),
                      warn=views_instructions.warning(ctx, "arch.instructions", cfg.architect.instructions, False))


def report(ctx: Ctx) -> Response:
    n = ctx.number("n")
    cfg = _cfg(ctx)
    store = _read(cfg)
    try:
        run = store.run(f"arch-{n}")
        if run is None:
            raise Gone()
        if run.state != "done":  # queued or under way: where it is, until the report is written
            status = runs.run_view(cfg, "arch", n)
            if status is None:
                raise Gone()
            return runs.status_page(ctx, status)
        _, host = links.providers(cfg)
        ledger = {r["n"]: r for r in architect_mod.ledger_rows(store)}
        view = architect_mod.run_view(run, host)
    finally:
        store.close()
    title, body = arch_report.run_body(view, ledger, cfg.project, cfg.schedule.tz, _forms(ctx),
                                       criteria=architect_mod.CRITERIA)
    return ctx.render("report.html", title=title, body=Markup(body), notice=ctx.query("notice"))


def live(ctx: Ctx) -> Response:
    n = ctx.number("n")
    view = runs.run_view(_cfg(ctx), "arch", n)
    if view is None:
        raise Gone()
    view["report_ready"] = view["state"] == "done"  # an architecture review's report is built from runner.db
    return runs.status_live(ctx, view)


def findings(ctx: Ctx) -> Response:
    cfg = _cfg(ctx)
    store = _read(cfg)
    try:
        ledger = architect_mod.ledger_rows(store)
    finally:
        store.close()
    title, body = arch_report.findings_body(ledger, cfg.project, _forms(ctx))
    return ctx.render("report.html", title=title, body=Markup(body), notice=ctx.query("notice"))


def decide(ctx: Ctx) -> Response:
    user = ctx.require("arch.decide")
    n = ctx.number("n")
    cfg = _cfg(ctx)
    decision, note = ctx.field("decision"), ctx.field("note").strip()[:arch_report.MAX_NOTE]
    back = ctx.field("back")
    back = back if _BACK.match(back) else "/architecture/findings/"
    try:
        store = ReviewStore.existing(cfg.paths.db)
    except (FileNotFoundError, RuntimeError) as e:
        raise ConsoleError(f"Nothing was decided: {e}") from None
    try:
        problem = architect_mod.decide(store, n, decision, note, user.label, ctx.field("version") or None)
    finally:
        store.close()
    if problem:
        status = 404 if problem.startswith("no finding") else 400 if problem.startswith(("say why", "no such")) \
            else 409
        return ctx.render("message.html", status=status, title="Not decided", message=problem[0].upper() + problem[1:]
                          + ".", back=back.split("#")[0])
    ctx.store.log(user, user.label, "arch_decided", f"finding:{n}", {"decision": decision, "note": note[:200]})
    words = {"accepted": "risk accepted", "false_positive": "marked a false positive", "open": "decision taken back"}
    path, _, anchor = back.partition("#")
    # Back to the finding on the page it was decided on: the notice before the anchor.
    query = urllib.parse.urlencode({"notice": f"Finding {n}: {words.get(decision, decision)}."})
    return RedirectResponse(f"{path}?{query}" + (f"#{anchor}" if anchor else ""), status_code=303)


def stop(ctx: Ctx) -> Response:
    user = ctx.require("arch.run")
    n = ctx.number("n")
    try:
        runs.stop(_cfg(ctx), "arch", n)
    except ConsoleError as e:
        return ctx.redirect("/architecture/", str(e))
    ctx.store.log(user, user.label, "arch_run_stop_asked", f"arch-{n}")
    return ctx.redirect("/architecture/", f"Review {n}: asked to stop. The runner stops it on its next pass.")


def instructions(ctx: Ctx) -> Response:
    cfg = _cfg(ctx)
    return runs.file_page(
        ctx, kind="arch.instructions", action="arch.instructions", path=cfg.architect.instructions,
        title="Review instructions",
        lede="The team's own words for the architecture reviews: what to look at harder, what to leave alone, the "
             "rules to hold the code to. Optional: the runner's criteria stand without them. Each review reads them "
             "when it starts and keeps a copy.",
        back=("/architecture/", "Architecture reviews"), required=False)


def routes() -> list[Route]:
    return [Route("/architecture/", page(index), methods=["GET", "POST"]),
            Route("/architecture/instructions/", page(instructions), methods=["GET", "POST"]),
            Route("/architecture/findings/", page(findings)),
            Route("/architecture/findings/{n}", page(decide), methods=["POST"]),
            Route("/architecture/{n}/", page(report)),
            Route("/architecture/{n}/live", page(live, api=True)),
            Route("/architecture/{n}/stop", page(stop), methods=["POST"])]
