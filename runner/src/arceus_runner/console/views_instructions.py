"""Every text the agents are given, in one place: what each is for, who reads it and when, who may edit it, and whether
the agent can be given what it holds now. The project's notes and the advisory triage's notes are edited here; the
test and review instructions keep their own pages, linked from the list. The server's notes are shown, never edited:
setup.sh puts them in place for every session on the server. The list names files and their state, never their text
(a test scenario's holds the test accounts)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from starlette.responses import Response
from starlette.routing import Route

from arceus_runner.console import files, runs
from arceus_runner.console.store import ConsoleError
from arceus_runner.console.web import Ctx, message, page
from arceus_runner.db import ReviewStore
from arceus_runner.qa_scenarios import file_of, history_kind
from arceus_runner.texts import PROJECT_NOTES_LIMIT, ProjectNotesError, project_notes, project_notes_text
from arceus_runner.workspace_run import MAX_INSTRUCTIONS, read_instructions

SECTIONS = ("/instructions/",)

NOTES_READS = ("A task reads them when it first starts and keeps its copy: its follow-ups, a /continue and its code "
               "review keep the version it started with. Tasks under way are not changed.")
SECOPS_READS = "A triage run reads them when it starts and keeps a copy: a run under way is not changed."


@dataclass
class Row:
    title: str
    what: str            # what it is for, in a line
    reader: str          # who reads it and when
    editors: str         # who may edit it
    href: str = ""       # its editor, or "" when it is not edited here
    path: Path | None = None
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
    """The lines under a file in the list: whether the agent can be given it as the runner reads it and how much of
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


def _scenarios(cfg) -> list:
    try:
        store = ReviewStore.read_only(cfg.paths.db)
    except (FileNotFoundError, RuntimeError):
        return []
    try:
        return store.scenarios()
    finally:
        store.close()


def rows(ctx: Ctx) -> list[Row]:
    cfg = _cfg(ctx)
    notes = cfg.paths.project_notes
    row = Row("Project notes", "What the product is, the team's words, how the repositories fit together, the rules "
              "the code does not show.", "Every task's agent, in AGENTS.md, when the task starts.", "Admins",
              "/instructions/project-notes/", notes)
    if notes is None:
        row.state, row.said = "unknown", "paths.project_notes is not set in config.yaml: tasks get no notes"
    else:
        _judge(ctx, row, "project_notes", notes_reader(notes), PROJECT_NOTES_LIMIT, required=False)
    out = [row]
    for sc in _scenarios(cfg):
        path = file_of(cfg.qa, sc)
        out.append(_judge(ctx, Row(f"Test scenario: {sc.name}", "The platform's address, its test accounts and what "
                                   "to test.", "A test run of this scenario, when it starts.", "QA, admins",
                                   f"/qa/scenarios/{sc.n}/instructions/", path),
                          history_kind(sc), lambda p=path: read_instructions(p), MAX_INSTRUCTIONS, required=True))
    path = cfg.architect.instructions
    out.append(_judge(ctx, Row("Architecture review", "What to look at harder, what to leave alone, the rules to hold "
                               "the code to.", "An architecture review, when it starts. Optional.",
                               "Architects, admins", "/architecture/instructions/", path),
                      "arch.instructions", lambda: read_instructions(path, False), MAX_INSTRUCTIONS, required=False))
    secops = cfg.secops.instructions
    out.append(_judge(ctx, Row("Advisory triage", "What is deployed where, which repositories are legacy, what the "
                               "advisories should be weighed against.", "A triage run, when it starts. Optional.",
                               "Admins", "/instructions/secops/", secops),
                      "secops.instructions", lambda: read_instructions(secops, False), MAX_INSTRUCTIONS,
                      required=False))
    out.append(Row("Server notes", "When to ask on the ticket, how to write the run summary, checks, proof, leaving "
                   "nothing behind. The same for every project on the server.",
                   "Every Claude Code session on the server, from the run user's ~/.claude/CLAUDE.md.",
                   "Arceus' maintainers: config/agent-notes.md in its repository, put in place by "
                   "setup.sh --phase agent_notes", state="unknown",
                   said="Not edited here: a change reaches every session at once, so it goes through Arceus' review"))
    return out


def index(ctx: Ctx) -> Response:
    ctx.require("instructions.view")
    try:
        listed, error = rows(ctx), ""
    except ConsoleError as e:
        listed, error = [], str(e)
    return ctx.render("instructions.html", title="Instructions", rows=listed, error=error)


def notes_page(ctx: Ctx) -> Response:
    cfg = _cfg(ctx)
    path = cfg.paths.project_notes
    if path is None:
        ctx.require("notes.edit")
        # paths is locked in Settings (settings_form.LOCKED_KEYS): it is set on the server, over SSH.
        return message(ctx, "Project notes", "paths.project_notes is not set in config.yaml, so tasks get no notes "
                       "and there is no file to edit. On the server, add `project_notes: "
                       "~/arceus/data/project-notes.md` under `paths:` in config.yaml, then run `sudo ./setup.sh` "
                       "(it creates the file from the template) and restart the runner. Settings shows paths but "
                       "does not change them.", back="/instructions/")
    return runs.file_page(
        ctx, kind="project_notes", action="notes.edit", path=path, title="Project notes",
        lede="The project's own rules for the agent: what the product is, the team's words, how the repositories fit "
             "together, decisions already made. Added to every task's AGENTS.md below the server's notes, which win "
             "where the two disagree.",
        back=("/instructions/", "Instructions"), required=False, limit=PROJECT_NOTES_LIMIT, reads=NOTES_READS,
        reader=notes_reader(path), check=notes_check(path))


def secops_page(ctx: Ctx) -> Response:
    cfg = _cfg(ctx)
    return runs.file_page(
        ctx, kind="secops.instructions", action="secops.instructions", path=cfg.secops.instructions,
        title="Advisory triage notes",
        lede="The team's notes on its stack for the triage of security advisories: what is deployed where, which "
             "repositories are legacy, what to weigh an advisory against. Optional: the triage stands without them.",
        back=("/instructions/", "Instructions"), required=False, reads=SECOPS_READS)


def routes() -> list[Route]:
    return [Route("/instructions/", page(index)),
            Route("/instructions/project-notes/", page(notes_page), methods=["GET", "POST"]),
            Route("/instructions/secops/", page(secops_page), methods=["GET", "POST"])]
