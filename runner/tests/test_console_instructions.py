"""The project's notes at the top of Settings and the advisory triage's notes on Advisory triage, each edited in the
console and checked by the runner's own rules before it is saved, and the warning a run's instructions file gives on
its process's page when a run would stop on it."""

from __future__ import annotations

import html
import re
from pathlib import Path

import pytest
import yaml

pytest.importorskip("starlette")

from arceus_runner.config import load_config  # noqa: E402
from arceus_runner.console.cli import _read_dirs  # noqa: E402
from arceus_runner.console.store import ConsoleStore  # noqa: E402
from arceus_runner.console.web import Settings  # noqa: E402
from arceus_runner.db import ReviewStore  # noqa: E402
from arceus_runner.texts import PROJECT_NOTES_LIMIT, ProjectNotesError, project_notes  # noqa: E402
from arceus_runner.workspace_run import MAX_INSTRUCTIONS, read_instructions  # noqa: E402

from tests.conftest import example_raw  # noqa: E402
from tests.test_console_auth import csrf_of, make_admin, signed_in  # noqa: E402
from tests.test_console_qa import person  # noqa: E402

NOTES = "/settings/project-notes/"
SECOPS = "/secops/instructions/"


@pytest.fixture
def w(tmp_path: Path):
    raw = example_raw()
    raw["paths"] = {"db": str(tmp_path / "runner.db"), "secrets": str(tmp_path / "secrets.env"),
                    "project_notes": str(tmp_path / "project-notes.md")}
    raw["qa"] = {"instructions": str(tmp_path / "qa" / "instructions.md")}
    raw["architect"] = {"instructions": str(tmp_path / "architect" / "instructions.md")}
    raw["secops"] = {"instructions": str(tmp_path / "secops" / "instructions.md")}
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(raw))
    settings = Settings(config_path=config, db_path=tmp_path / "console" / "console.db")
    make_admin(settings)
    ReviewStore(tmp_path / "runner.db").close()
    (tmp_path / "qa").mkdir()
    (tmp_path / "qa" / "instructions.md").write_text("https://staging.example.com, admin / hunter2\n")
    (tmp_path / "project-notes.md").write_text("<!-- how to edit -->\nThe product is a school.\n")

    class W:
        pass
    x = W()
    x.settings, x.tmp, x.config, x.raw = settings, tmp_path, config, raw
    x.notes, x.secops = tmp_path / "project-notes.md", tmp_path / "secops" / "instructions.md"
    x.admin = signed_in(settings, "owner@example.com")
    return x


def sha_of(page: str) -> str:
    return re.search(r'name="sha" value="([^"]+)"', page).group(1)


def form(page: str, **data) -> dict:
    """What the editor's form sends: its token, the version it showed, and the person's fields."""
    return {"csrf": csrf_of(page), "sha": sha_of(page), **data}


def save(c, path: str, text: str, note: str = ""):
    page = c.get(path).text
    return c.post(path, data=form(page, text=text, note=note))


def versions(w, kind: str) -> list[dict]:
    with ConsoleStore(w.settings.db_path) as s:
        return s.versions(kind)


def sized(n: int) -> str:
    """Notes that are exactly n bytes for the agent: Cyrillic (two bytes a letter) and a comment it never gets."""
    body = "Правило " + "ж" * ((n - len("Правило ".encode())) // 2)
    body += "x" * (n - len(body.encode()))
    assert len(body.encode()) == n
    return "<!-- for whoever edits: not counted -->\n" + body + "\n"


def notes_card(page: str) -> str:
    """The Project notes block at the top of Settings."""
    return page.split('id="project-notes"', 1)[1].split("</section>", 1)[0]


def test_settings_opens_with_the_project_notes_and_their_state_never_their_text(w):
    page = w.admin.get("/settings/").text
    card = notes_card(page)
    assert page.index('id="project-notes"') < page.index("The runner's settings")  # first on the page
    assert f'<a class="btn" href="{NOTES}">Edit</a>' in card and str(w.notes) in card
    assert f"24 of {PROJECT_NOTES_LIMIT} bytes for the agent" in card and "when the task first starts" in card
    assert "The product is a school." not in page  # the state, never the text
    assert "config/agent-notes.md" in card and "not edited here" in card  # the server's notes, as a note
    assert ">Instructions</a>" not in page  # no such section in the bar any more


def test_who_sees_the_notes_and_who_edits_what(w):
    viewer = person(w.settings, "v@example.com", set())
    qa = person(w.settings, "q@example.com", {"qa"})
    operator = person(w.settings, "o@example.com", {"operator"})
    assert viewer.get("/settings/").status_code == 403 and qa.get("/settings/").status_code == 403
    card = notes_card(operator.get("/settings/").text)
    assert f"24 of {PROJECT_NOTES_LIMIT} bytes for the agent" in card and "Admins edit it." in card
    assert NOTES not in card  # the state, without the editor
    for c in (viewer, qa, operator):
        for path in (NOTES, SECOPS, NOTES + "?version=1"):
            assert c.get(path).status_code == 403
        home = c.get("/").text
        for path in (NOTES, SECOPS):
            assert c.post(path, data={"csrf": csrf_of(home), "sha": "missing", "text": "mine"}).status_code == 403
    assert w.notes.read_text().endswith("The product is a school.\n") and not w.secops.exists()


def test_the_old_addresses_lead_to_the_new_ones(w):
    r = w.admin.get("/instructions/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings/#project-notes"
    qa = person(w.settings, "q@example.com", {"qa"})
    assert qa.get("/instructions/", follow_redirects=False).headers["location"] == "/"
    for old, new in (("/instructions/project-notes/", NOTES), ("/instructions/secops/", SECOPS)):
        r = w.admin.get(old + "?version=3", follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == new + "?version=3"  # a link to a version keeps it
    # A form opened at the old address before the move still saves: 307 keeps the method and the text.
    page = w.admin.get(NOTES).text
    sent = form(page, text="Saved from an old tab.\n")
    r = w.admin.post("/instructions/project-notes/", data=sent)
    assert r.status_code == 307 and r.headers["location"] == NOTES
    r = w.admin.post(r.headers["location"], data=sent)  # what the browser sends next
    assert r.status_code == 303 and r.headers["location"].startswith("/settings/?notice=Saved.")
    assert w.notes.read_text() == "Saved from an old tab.\n"


def test_a_save_leads_back_to_the_section_with_its_notice(w):
    r = save(w.admin, NOTES, "Saved from Settings.\n")
    where = r.headers["location"]
    # No #part: the notice is first on the page, and a jump to the block would scroll it out of sight.
    assert r.status_code == 303 and where.startswith("/settings/?notice=Saved.") and "#" not in where
    page = w.admin.get(where).text
    assert page.index('role="status">Saved.') < page.index('id="project-notes"')
    assert '<p class="notice" role="status">Saved. A task reads them when it first starts' in page
    assert "25 of" not in notes_card(page) and f"20 of {PROJECT_NOTES_LIMIT} bytes" in notes_card(page)
    r = save(w.admin, SECOPS, "Billing is legacy.\n")
    assert r.status_code == 303 and r.headers["location"].startswith("/secops/?notice=Saved.")
    page = w.admin.get(r.headers["location"]).text
    assert '<p class="notice" role="status">Saved. A triage run reads them when it starts' in page
    # A refusal stays in the editor with the text, as before.
    r = save(w.admin, NOTES, sized(PROJECT_NOTES_LIMIT + 1))
    assert r.status_code == 400 and "Not saved" in r.text and "ж" * 50 in r.text


def test_the_editors_lead_back_to_where_they_are_opened(w):
    assert 'href="/settings/"' in w.admin.get(NOTES).text.split("<main>", 1)[1]
    assert 'href="/secops/"' in w.admin.get(SECOPS).text.split("<main>", 1)[1]
    del w.raw["paths"]["project_notes"]
    w.config.write_text(yaml.safe_dump(w.raw))
    assert 'href="/settings/"' in w.admin.get(NOTES).text.split("<main>", 1)[1]


def test_a_file_a_run_would_stop_on_is_flagged_on_its_process(w):
    """The check the old list of every text made: shown where the process is, and only when something is wrong."""
    qa_file = Path(w.raw["qa"]["instructions"])
    arch_file = Path(w.raw["architect"]["instructions"])
    assert "file-warn" not in w.admin.get("/qa/").text  # a file in order says nothing
    assert '<p class="notice err">' not in w.admin.get("/architecture/").text
    qa_file.write_text("<!-- todo -->\n")
    arch_file.parent.mkdir()
    arch_file.write_text("x" * (MAX_INSTRUCTIONS + 1))
    w.secops.parent.mkdir()
    w.secops.write_text("x" * (MAX_INSTRUCTIONS + 1))
    page = person(w.settings, "o@example.com", {"operator"}).get("/qa/").text  # seen without the editor
    assert "is empty (comments only): its runs do not start until it is fixed" in page
    page = w.admin.get("/architecture/").text
    assert "Review instructions: the instructions file" in page and "larger than 64 KB" in page
    assert "Triage notes: the instructions file" in w.admin.get("/secops/").text
    assert "xxxx" not in page  # the problem, never the text


def test_a_broken_config_is_not_shown_to_someone_who_may_not_edit(w):
    qa = person(w.settings, "q@example.com", {"qa"})
    w.config.write_text(yaml.safe_dump({**w.raw, "no_such_key": 1}))
    for path in (NOTES, SECOPS):
        page = qa.get(path)
        assert page.status_code == 403 and "config.yaml has errors" not in page.text
        assert qa.post(path, data={"csrf": csrf_of(qa.get("/").text), "sha": "missing",
                                   "text": "mine"}).status_code == 403
        page = w.admin.get(path)
        assert page.status_code == 503 and "config.yaml has errors" in page.text


def test_admins_edit_the_notes_and_every_version_is_kept(w):
    page = w.admin.get(NOTES).text
    assert "Project notes" in page and str(w.notes) in page and "bytes left" in page and "keeps its copy" in page
    w.notes.chmod(0o640)
    r = save(w.admin, NOTES, "The product is a school.\r\nWords: a cohort is a group.\r\n", "words")
    assert r.status_code == 303
    assert w.notes.read_text() == "The product is a school.\nWords: a cohort is a group.\n"
    assert w.notes.stat().st_mode & 0o777 == 0o640
    assert project_notes(w.notes) == "The product is a school.\nWords: a cohort is a group."  # what a task pins
    [v] = versions(w, "project_notes")
    assert v["actor"] == "Owner <owner@example.com>" and v["note"] == "words"
    assert v["before_text"].endswith("The product is a school.\n")
    # An earlier version opens as a draft and is not written until saved.
    page = w.admin.get(f"{NOTES}?version={v['id']}").text
    assert "It is not saved yet" in page and "What the agent would get from this text" in page
    assert len(versions(w, "project_notes")) == 1


def test_the_notes_editor_refuses_what_the_runner_would_refuse(w):
    fits, over = sized(PROJECT_NOTES_LIMIT), sized(PROJECT_NOTES_LIMIT + 1)
    assert save(w.admin, NOTES, fits).status_code == 303
    assert project_notes(w.notes).encode() == fits.split("\n", 1)[1].strip().encode()
    before = w.notes.read_text()
    r = save(w.admin, NOTES, over)
    assert r.status_code == 400 and "Not saved" in r.text and f"{PROJECT_NOTES_LIMIT + 1} bytes" in r.text
    assert "ж" * 50 in r.text  # the draft stays in the editor
    assert w.notes.read_text() == before and len(versions(w, "project_notes")) == 1
    w.notes.write_text(over)
    with pytest.raises(ProjectNotesError):
        project_notes(w.notes)  # the runner's own word on the same text


def test_notes_not_set_and_notes_missing(w):
    w.notes.unlink()
    page = notes_card(w.admin.get("/settings/").text)
    assert "does not exist; no task starts until it is fixed" in page
    assert "does not exist yet" in w.admin.get(NOTES).text
    assert save(w.admin, NOTES, "Made from the console.").status_code == 303
    assert w.notes.read_text() == "Made from the console.\n"
    del w.raw["paths"]["project_notes"]
    w.config.write_text(yaml.safe_dump(w.raw))
    card = notes_card(w.admin.get("/settings/").text)
    assert "paths.project_notes is not set" in card and f'href="{NOTES}">How to set up</a>' in card
    r = w.admin.get(NOTES)
    assert r.status_code == 200 and "there is no file to edit" in r.text
    assert "under `paths:` in config.yaml" in r.text and "sudo ./setup.sh" in r.text
    assert "Set it under paths in Settings" not in r.text


def test_admins_edit_the_triage_notes_empty_or_up_to_the_limit(w):
    page = w.admin.get(SECOPS).text
    assert "Advisory triage notes" in page and "does not exist yet" in page
    assert save(w.admin, SECOPS, "Billing is legacy: not deployed.").status_code == 303
    assert read_instructions(w.secops, required=False) == ("Billing is legacy: not deployed.\n", "")
    assert save(w.admin, SECOPS, "<!-- nothing for now -->").status_code == 303  # optional: empty is fine
    assert read_instructions(w.secops, required=False) == ("", "")
    r = save(w.admin, SECOPS, "x" * (MAX_INSTRUCTIONS + 1))
    assert r.status_code == 400 and "larger than 64 KB" in r.text
    assert w.secops.read_text() == "<!-- nothing for now -->\n"
    assert [v["actor"] for v in versions(w, "secops.instructions")] == ["Owner <owner@example.com>"] * 2


def test_two_tabs_saving_the_same_version(w):
    page = w.admin.get(NOTES).text
    first = w.admin.post(NOTES, data=form(page, text="first tab\n"))
    second = w.admin.post(NOTES, data=form(page, text="second tab\n"))
    assert first.status_code == 303 and second.status_code == 409
    assert "second tab" in second.text and "+first tab" not in second.text and "-first tab" in second.text
    assert w.notes.read_text() == "first tab\n" and len(versions(w, "project_notes")) == 1


def test_a_post_without_the_token_changes_nothing(w):
    page = w.admin.get(NOTES).text
    r = w.admin.post(NOTES, data={"sha": sha_of(page), "text": "no token"})
    assert r.status_code in (400, 403) and "no token" not in w.notes.read_text()


def test_the_console_container_sees_the_triage_notes_folder(tmp_path):
    raw = example_raw()
    raw["paths"] = {"db": str(tmp_path / "data" / "runner.db"), "secrets": str(tmp_path / "data" / "secrets.env")}
    raw["secops"] = {"instructions": str(tmp_path / "elsewhere" / "secops" / "instructions.md")}
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(raw))
    assert (tmp_path / "elsewhere" / "secops").resolve() in _read_dirs(load_config(config))


def test_the_existing_editors_also_refuse_what_their_run_would(w):
    """A test scenario's instructions are required: comments only would stop its next run, so they are not saved."""
    qa = Path(w.raw["qa"]["instructions"])
    r = save(w.admin, "/qa/scenarios/1/instructions/", "<!-- todo -->")
    assert r.status_code == 400 and "comments only" in r.text and "hunter2" in qa.read_text()


def test_a_refused_draft_keeps_the_version_it_was_opened_from(w):
    """A change on the server while the person fixes a refused draft is still a conflict, never replaced unseen."""
    page = w.admin.get(NOTES).text
    opened = sha_of(page)
    w.notes.write_text("Changed over SSH meanwhile.\n")
    r = w.admin.post(NOTES, data={**form(page), "sha": opened, "text": sized(PROJECT_NOTES_LIMIT + 1),
                                  "note": "my rule"})
    assert r.status_code == 400 and sha_of(r.text) == opened and 'value="my rule"' in r.text
    r = w.admin.post(NOTES, data=form(r.text, text="Shorter.\n"))
    assert r.status_code == 409 and w.notes.read_text() == "Changed over SSH meanwhile.\n"


def test_a_change_made_on_the_server_shows_in_the_console(w):
    assert save(w.admin, NOTES, "Saved here.\n", "from the console").status_code == 303
    w.notes.write_text("Edited over SSH.\n")
    page = w.admin.get(NOTES).text
    assert "changed on the server, not here" in page and "Edited over SSH." in page
    assert "-Saved here." in page and "+Edited over SSH." in page
    assert "Changed on the server after the last save here" in notes_card(w.admin.get("/settings/").text)
    assert save(w.admin, NOTES, "Saved here again.\n").status_code == 303
    newest = versions(w, "project_notes")[0]
    assert newest["before_text"] == "Edited over SSH.\n"  # the server's edit is in the history now
    assert "changed on the server" not in w.admin.get(NOTES).text
    assert "Changed on the server" not in notes_card(w.admin.get("/settings/").text)


def test_the_count_matches_the_runner_for_a_file_with_windows_line_ends(w):
    w.notes.write_bytes(("<!-- c -->\r\n" + "ж" * 8000 + "\r\n" + "x" * 383 + "\r\n").encode())
    size = len(project_notes(w.notes).encode())
    assert size == PROJECT_NOTES_LIMIT
    page = notes_card(w.admin.get("/settings/").text)
    assert f"{size} of {PROJECT_NOTES_LIMIT} bytes for the agent, 0 left" in page


def test_a_file_the_editor_cannot_hold_is_judged_as_the_runner_reads_it(w):
    """A symbolic link is edited on the server, but the agent still gets what it points to."""
    real = w.tmp / "real-notes.md"
    real.write_text("Notes kept elsewhere.\n")
    w.notes.unlink()
    w.notes.symlink_to(real)
    assert project_notes(w.notes) == "Notes kept elsewhere."
    assert f"21 of {PROJECT_NOTES_LIMIT} bytes for the agent" in notes_card(w.admin.get("/settings/").text)
    page = w.admin.get(NOTES).text
    assert "symbolic link" in page and "21 bytes of instructions" in page and "disabled" in page


def test_a_folder_the_console_cannot_write_says_so_before_saving(w):
    """The file itself is writable; its folder is not, and a save makes a temporary file there first."""
    folder = w.secops.parent
    folder.mkdir()
    w.secops.write_text("Billing is legacy.\n")
    folder.chmod(0o555)
    try:
        page = w.admin.get(SECOPS).text
        assert "cannot be written from here" in page and re.search(r'<button[^>]*type="submit" disabled', page)
    finally:
        folder.chmod(0o755)
    page = w.admin.get(SECOPS).text
    w.secops.chmod(0o444)  # made read-only after the page was opened: the server refuses, not only the button
    r = w.admin.post(SECOPS, data=form(page, text="Must not land.\n", note="late"))
    assert r.status_code == 400 and "read-only to the console" in r.text and "Must not land." in r.text
    assert w.secops.read_text() == "Billing is legacy.\n" and versions(w, "secops.instructions") == []
    assert re.search(r'<button[^>]*type="submit" disabled', w.admin.get(SECOPS).text)


def test_an_old_version_opened_after_a_change_on_the_server_does_not_claim_to_be_the_file(w):
    assert save(w.admin, NOTES, "A\n").status_code == 303
    w.notes.write_text("B\n")
    [v] = versions(w, "project_notes")
    page = w.admin.get(f"{NOTES}?version={v['id']}").text
    assert "changed on the server, not here" in page and "which is what the editor holds" not in page
    assert "It is not saved yet" in page


def test_a_refusal_after_a_save_does_not_still_say_saved(w):
    """The form posts to the address the last save left, ?notice= and all: a refusal there says only the refusal.
    The review instructions' editor is one that a save leads back to."""
    path = "/architecture/instructions/"
    r = save(w.admin, path, "First.\n")
    after = r.headers["location"]
    assert after.startswith(path)
    page = w.admin.get(after).text
    assert "Saved." in page
    r = w.admin.post(after, data=form(page, text="x" * (MAX_INSTRUCTIONS + 1)))
    assert r.status_code == 400 and "Not saved" in r.text and "Saved." not in r.text


def test_a_stale_version_that_already_holds_the_text_saves_nothing(w):
    page = w.admin.get(NOTES).text
    w.notes.write_text("Mine, put there over SSH.\n")
    r = w.admin.post(NOTES, data=form(page, text="Mine, put there over SSH.\n"))
    assert r.status_code == 200 and "Nothing changed: the file is already this text." in r.text
    assert versions(w, "project_notes") == []


def test_the_text_a_save_replaced_opens_again(w):
    """The file before the first save here, and an edit made on the server between two saves, both come back."""
    original = w.notes.read_text()
    assert save(w.admin, NOTES, "First save.\n").status_code == 303
    w.notes.write_text("Edited over SSH.\n")
    assert save(w.admin, NOTES, "Second save.\n").status_code == 303
    second, first = versions(w, "project_notes")
    page = w.admin.get(f"{NOTES}?version={first['id']}&before=1").text
    assert "the text the save of" in page and html.escape(original.strip()) in page
    page = w.admin.get(f"{NOTES}?version={second['id']}&before=1").text
    assert "Edited over SSH." in page and "It is not saved yet" in page
    assert page.count("Open what it replaced") == 2
    assert w.notes.read_text() == "Second save.\n"


def test_the_file_is_the_one_source_and_the_history_is_its_chain(w):
    """No copy of its own: every save writes the runner's file, and the versions chain from the file as it was."""
    original = w.notes.read_text()
    assert save(w.admin, NOTES, "A\n").status_code == 303
    w.notes.write_text("B, over SSH\n")
    assert save(w.admin, NOTES, "C\n").status_code == 303
    second, first = versions(w, "project_notes")
    assert (first["before_text"], first["after_text"]) == (original, "A\n")
    assert (second["before_text"], second["after_text"]) == ("B, over SSH\n", "C\n")
    assert first["path"] == second["path"] == str(w.notes)
    assert w.notes.read_text() == second["after_text"] and project_notes(w.notes) == "C"


def triage_sized(n: int) -> str:
    body = "Стек: " + "ж" * ((n - len("Стек: ".encode())) // 2)
    body += "x" * (n - len(body.encode()))
    assert len(body.encode()) == n
    return "<!-- for whoever edits -->\n" + body + "\n"


def test_the_triage_notes_limit_is_the_runner_s(w):
    assert save(w.admin, SECOPS, triage_sized(MAX_INSTRUCTIONS)).status_code == 303
    got, why = read_instructions(w.secops, required=False)
    assert why == "" and len(got.rstrip("\n").encode()) == MAX_INSTRUCTIONS
    before = w.secops.read_text()
    r = save(w.admin, SECOPS, triage_sized(MAX_INSTRUCTIONS + 1), "one byte over")
    assert r.status_code == 400 and "larger than 64 KB" in r.text and "ж" * 50 in r.text
    assert 'value="one byte over"' in r.text
    assert w.secops.read_text() == before and len(versions(w, "secops.instructions")) == 1
    w.secops.write_text(triage_sized(MAX_INSTRUCTIONS + 1))
    assert read_instructions(w.secops, required=False)[0] is None  # the runner's own word on the same text
