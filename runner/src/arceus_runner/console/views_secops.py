"""Advisory triage (secops.py) in the console: the list of triage runs and a run's report, rendered from its
result.json as a test run's is (runs.report_page), and the way to the team's notes, which admins edit
(views_instructions). Nothing here starts or stops a run: a run is started by `arceus-runner secops run` or
`secops.schedule` and stopped by `secops stop`, on the server."""

from __future__ import annotations

from starlette.responses import Response
from starlette.routing import Route

from arceus_runner import secops
from arceus_runner.console import runs, views_instructions
from arceus_runner.console.store import ConsoleError
from arceus_runner.console.web import Ctx, Markup, page
from arceus_runner.db import ReviewStore
from arceus_runner.qa_report import rich

SECTIONS = ("/secops/",)


def _cfg(ctx: Ctx):
    cfg, problems = ctx.config()
    if cfg is None:
        raise ConsoleError("config.yaml has errors: " + "; ".join(problems[:3]))
    return cfg


def index(ctx: Ctx) -> Response:
    cfg = _cfg(ctx)
    try:
        store = ReviewStore.read_only(cfg.paths.db)
    except (FileNotFoundError, RuntimeError) as e:
        rows, error = [], str(e)
    else:
        try:
            rows, error = secops.index_rows(store, cfg.secops.reports), ""
        finally:
            store.close()
    for r in rows:  # escaped, with the code in it set as code, as the runner's own list shows it (render_index)
        r["summary"] = Markup(rich(r["summary"]))
    return ctx.render("runs.html", title="Advisory triage", kind="secops",
                      rows=runs.dress(rows, secops.head_of, "secops"), enabled=cfg.secops.enabled, error=error,
                      notice=ctx.query("notice"),
                      mode=cfg.mode, schedule=cfg.secops.schedule, can_edit=ctx.can("secops.instructions"),
                      warn=views_instructions.warning(ctx, "secops.instructions", cfg.secops.instructions, False))


def run(ctx: Ctx) -> Response:
    n = ctx.number("n")
    return runs.report_page(ctx, _cfg(ctx), "secops", n, secops.run_body)


def live(ctx: Ctx) -> Response:
    n = ctx.number("n")
    return runs.report_live(ctx, _cfg(ctx), "secops", n)


def routes() -> list[Route]:
    return [Route("/secops/", page(index)),
            Route("/secops/{n}/", page(run)),
            Route("/secops/{n}/live", page(live, api=True))]
