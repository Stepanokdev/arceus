"""The editors of the project's notes (opened from the top of Settings) and of the advisory triage's notes (from
Advisory triage), each checked by the runner's own rules before it is saved, and the check every text the agents are
given goes through before it is shown: whether the agent can be given what the file holds now. A run's instructions
are judged here for the warning on its process's page; the page names the problem, never the file's text (a test
scenario's holds the test accounts)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from arceus_runner.console import files, runs
from arceus_runner.console.store import ConsoleError
from arceus_runner.console.web import Ctx, message, page
from arceus_runner.texts import PROJECT_NOTES_LIMIT, ProjectNotesError, project_notes, project_notes_text
from arceus_runner.workspace_run import MAX_INSTRUCTIONS, read_instructions

NOTES_PATH = "/settings/project-notes/"
SECOPS_PATH = "/secops/instructions/"

NOTES_READS = ("A task reads them when it first starts and keeps its copy: its follow-ups, a /continue and its code "
               "review keep the version it started with. Tasks under way are not changed.")
SECOPS_READS = "A triage run reads them when it starts and keeps a copy: a run under way is not changed."


@dataclass
class Row:
    path: Path | None
    what: str = ""       # what it is for, in a line
    reader: str = ""     # who reads it and when
    state: str = ""      # good / bad / unknown, for the line under it
    said: str = ""       # that line
    outside: dict | None = None  # the last save here, when the file was changed on the server since


def _cfg(ctx: Ctx):
    cfg, problems = ctx.config()
    if cfg is None:
        raise ConsoleError("config.yaml has errors: " + "; ".join(problems[:3]))
    return cfg


def _notes(load, refused: str) -> runs.Verdict:
    """The project's notes as the runner takes them (texts.project_notes), as the editor's verdict."""
    try:
        notes = load()
    except ProjectNotesError as e:
        return None, str(e).removeprefix("paths.project_notes: ") + refused
    return (notes + "\n" if notes else ""), ""


def notes_reader(path: Path):
    return lambda: _notes(lambda: project_notes(path), "; no task starts until it is fixed")


def notes_check(path: Path):
    # Named "the text" in the refusal: the draft is not the file yet.
    return lambda text: _notes(lambda: project_notes_text(text, Path("the text")), "")


def _judge(ctx: Ctx, row: Row, kind: str, reader, limit: int, required: bool) -> Row:
    """The line about a file: whether the agent can be given it as the runner reads it and how much of
    the limit it takes, and whether it was changed on the server since the last save here."""
    gets, why = reader()
    if gets is None:
        row.state, row.said = "bad", why
    elif not gets.strip():
        row.state = "bad" if required else "unknown"
        row.said = "empty: the agent gets nothing" if row.path.exists() else "no file yet: the agent gets nothing"
    else:
        size = len(gets.rstrip("\n").encode())  # as the runner counts it, against the same limit
        row.state, row.said = "good", f"{size} of {limit} bytes for the agent, {limit - size} left"
    row.outside = runs.changed_outside(ctx.store.versions(kind, limit=1), files.read(row.path))
    return row


def notes_row(ctx: Ctx, cfg) -> Row:
    """The project's notes as the top of Settings shows them: whether every task's agent can be given what the file
    holds now, how much of the limit it takes, and whether it was changed on the server since the last save here."""
    notes = cfg.paths.project_notes
    row = Row(notes, "What the product is, the team's words, how the repositories fit together, the rules the code "
              "does not show.", "Every task's agent reads them in AGENTS.md when the task first starts; tasks under "
              "way keep the version they started with.")
    if notes is None:
        row.state, row.said = "unknown", "paths.project_notes is not set in config.yaml: tasks get no notes"
        return row
    return _judge(ctx, row, "project_notes", notes_reader(notes), PROJECT_NOTES_LIMIT, required=False)


def warning(ctx: Ctx, kind: str, path: Path, required: bool) -> Row | None:
    """A run's instructions file judged as its run reads it, only when something is wrong: the run would stop on it
    (required and empty, too large, unreadable) or it was changed on the server since the last save here. Shown on
    the row or the header of its process, so the problem is seen before a run starts. A file in order says nothing."""
    row = _judge(ctx, Row(path), kind, lambda: read_instructions(path, required),
                 MAX_INSTRUCTIONS, required)
    return row if row.state == "bad" or row.outside else None


def notes_page(ctx: Ctx) -> Response:
    ctx.require("notes.edit")  # before the config: its errors are not shown to someone who may not edit
    cfg = _cfg(ctx)
    path = cfg.paths.project_notes
    if path is None:
        # paths is locked in Settings (settings_form.LOCKED_KEYS): it is set on the server, over SSH.
        return message(ctx, "Project notes", "paths.project_notes is not set in config.yaml, so tasks get no notes "
                       "and there is no file to edit. On the server, add `project_notes: "
                       "~/arceus/data/project-notes.md` under `paths:` in config.yaml, then run `sudo ./setup.sh` "
                       "(it creates the file from the template) and restart the runner. Settings shows paths but "
                       "does not change them.", back="/settings/")
    return runs.file_page(
        ctx, kind="project_notes", action="notes.edit", path=path, title="Project notes",
        lede="The project's own rules for the agent: what the product is, the team's words, how the repositories fit "
             "together, decisions already made. Added to every task's AGENTS.md under Project notes. The server's notes, "
             "which every session reads from the run user's ~/.claude/CLAUDE.md, win where the two disagree.",
        back=("/settings/", "Settings"), saved_to="/settings/", required=False,
        limit=PROJECT_NOTES_LIMIT, reads=NOTES_READS,
        reader=notes_reader(path), check=notes_check(path))


def secops_page(ctx: Ctx) -> Response:
    ctx.require("secops.instructions")
    cfg = _cfg(ctx)
    return runs.file_page(
        ctx, kind="secops.instructions", action="secops.instructions", path=cfg.secops.instructions,
        title="Advisory triage notes",
        lede="The team's notes on its stack for the triage of security advisories: what is deployed where, which "
             "repositories are legacy, what to weigh an advisory against. Optional: the triage stands without them.",
        back=("/secops/", "Advisory triage"), saved_to="/secops/", required=False, reads=SECOPS_READS)


def _moved(to: str):
    """An address the editors had before they moved to their own sections. 307 keeps the method and the body, so a
    form opened at the old address still saves; a link to a version keeps its query."""
    def handler(ctx: Ctx) -> Response:
        query = ctx.request.url.query
        return RedirectResponse(to + (f"?{query}" if query else ""), status_code=307)
    return handler


def _old_list(ctx: Ctx) -> Response:
    """The list of every text the agents are given, which this address had: the project's notes are at the top of
    Settings now, every other text on the page of its own process."""
    return ctx.redirect("/settings/#project-notes" if ctx.can("settings.view") else "/")


def routes() -> list[Route]:
    # Registered before views_secops and views_settings (web.create_app), whose /secops/{n}/ and /settings/{name}/
    # would take these addresses.
    return [Route(NOTES_PATH, page(notes_page), methods=["GET", "POST"]),
            Route(SECOPS_PATH, page(secops_page), methods=["GET", "POST"]),
            Route("/instructions/", page(_old_list)),
            Route("/instructions/project-notes/", page(_moved(NOTES_PATH)), methods=["GET", "POST"]),
            Route("/instructions/secops/", page(_moved(SECOPS_PATH)), methods=["GET", "POST"])]
