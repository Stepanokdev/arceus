"""Test runs in the console: the list, a run's report with its media, only the report's own files, starting and
stopping a run by role, and the test instructions."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from arceus_runner import qa_report  # noqa: E402
from arceus_runner.config import load_config  # noqa: E402
from arceus_runner.console.store import ConsoleStore  # noqa: E402
from arceus_runner.console.web import Settings, create_app  # noqa: E402
from arceus_runner.db import ReviewStore  # noqa: E402
from arceus_runner.workspace_run import report_address  # noqa: E402

from tests.conftest import example_raw  # noqa: E402
from tests.test_console_auth import PASSWORD, csrf_of, make_admin, post, signed_in  # noqa: E402

SHOT = "0123456789abcdef.png"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def person(settings: Settings, email: str, roles: set[str]) -> TestClient:
    with ConsoleStore(settings.db_path) as s:
        token = s.make_link("invite", email, roles, actor="test")
        s.use_link(token, PASSWORD, email.split("@")[0].title())
    return signed_in(settings, email)


@pytest.fixture
def w(tmp_path: Path):
    raw = example_raw()
    reports = tmp_path / "qa" / "runs"
    raw["paths"] = {"db": str(tmp_path / "runner.db"), "secrets": str(tmp_path / "secrets.env")}
    raw["qa"] = {"enabled": True, "reports": str(reports), "instructions": str(tmp_path / "qa" / "instructions.md")}
    raw["mode"] = "live"
    raw["security"] = {"reviewers": ["ivan"]}
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(raw))
    settings = Settings(config_path=config, db_path=tmp_path / "console" / "console.db")
    make_admin(settings)

    store = ReviewStore(tmp_path / "runner.db")
    run = store.add_run("qa", "schedule", "schedule", scenario=1)
    store.update_run(run.id, state="done", outcome="found", started_at="2026-11-08T06:00:00Z",
                     finished_at="2026-11-08T07:40:00Z",
                     result=json.dumps({"summary": "Signing in is broken.", "findings": [{}]}))
    store.close()
    folder = reports / "1"
    (folder / "media").mkdir(parents=True)
    (folder / "media" / SHOT).write_bytes(PNG)
    (folder / "instructions.md").write_text("admin / hunter2\n")
    view = {"n": 1, "outcome": "found", "source": "schedule", "started_at": "2026-11-08T06:00:00Z",
            "finished_at": "2026-11-08T07:40:00Z", "summary": "Signing in is broken.",
            "instructions": {"path": "qa/instructions.md", "sha256": "ab" * 32},
            "tested": [{"what": "Signing in", "result": "problem"}, {"what": "The catalogue", "result": "ok"}],
            "findings": [{"title": "The Sign in button does nothing", "severity": "critical",
                          "type": qa_report.TYPE_SECURITY,
                          "steps": ["Open `/login`", "Type the test account", "Press Sign in"], "failed_step": 3,
                          "evidence": [{"file": SHOT, "step": 3, "caption": "Nothing happens", "kind": "image"}],
                          "expected": "The dashboard opens", "actual": "The page stays as it is",
                          "cause": "The form posts to a route that was renamed",
                          "code": [{"repo": "web", "file": "src/login.ts", "line": 12, "sha": "c0ffee" * 6,
                                    "url": "https://gitlab.example.com/web/-/blob/x/src/login.ts#L12"}]}]}
    (folder / "result.json").write_text(json.dumps(view))
    (folder / "report.html").write_text("<p>report</p>")

    class W:
        pass
    x = W()
    x.settings, x.tmp, x.config, x.raw, x.reports = settings, tmp_path, config, raw, reports
    x.instructions = tmp_path / "qa" / "instructions.md"
    return x


def test_the_list_shows_every_run_and_a_queued_one_at_once(w):
    viewer = person(w.settings, "v@example.com", set())
    page = viewer.get("/qa/").text
    assert "Run 1" in page and "1 problem found" in page and "Signing in is broken." in page
    assert 'href="/qa/1/"' in page and "Schedule" in page
    assert "Run now" not in page and "New scenario" not in page and "Instructions</a>" not in page  # no role
    assert "Main" in page and "By hand only" in page
    s = ReviewStore(w.tmp / "runner.db")
    s.add_run("qa", "command", "q@example.com", scenario=1)
    s.close()
    page = viewer.get("/qa/").text
    assert "Run 2" in page and "Queued" in page and "q@example.com" in page


def test_with_qa_off_the_old_reports_stay_and_the_page_says_why_none_start(w):
    w.raw["qa"]["enabled"] = False
    w.config.write_text(yaml.safe_dump(w.raw))
    qa = person(w.settings, "q@example.com", {"qa"})
    page = qa.get("/qa/").text
    assert "off in config.yaml" in page and "Run 1" in page
    assert "Run now" not in page
    r = qa.post("/qa/", data={"csrf": csrf_of(page)})
    assert r.status_code == 400 and "nothing was queued" in r.text


def test_the_link_a_finished_run_is_told_with_opens_its_report_after_signing_in(w):
    w.raw["console"] = {"enabled": True, "public_url": "https://console.example.com"}
    w.config.write_text(yaml.safe_dump(w.raw))
    url, tunnel = report_address(load_config(w.config), "qa", 3300, 1)
    assert (url, tunnel) == ("https://console.example.com/qa/1/", False)
    path = url.removeprefix("https://console.example.com")
    r = TestClient(create_app(w.settings), base_url="https://console.example.com").get(path, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Fqa%2F1%2F"
    page = person(w.settings, "v@example.com", set()).get(path).text
    assert "Platform test, run 1" in page and f'src="media/{SHOT}"' in page


@pytest.mark.parametrize("enabled, queued, listed", [
    (True, "the report will be at https://console.example.com/qa/2/.", "Reports: https://console.example.com/qa/\n"),
    (False, "the report will be at http://localhost:3300/2/ (through the SSH tunnel).",
     "Reports: http://localhost:3300/ (through the SSH tunnel)\n")])
def test_the_command_points_at_the_same_reports(w, capsys, enabled, queued, listed):
    from arceus_runner import cli
    w.raw["console"] = {"enabled": enabled, "public_url": "https://console.example.com"}
    w.config.write_text(yaml.safe_dump(w.raw))
    assert cli.main(["--config", str(w.config), "qa", "run"]) == 0
    assert queued in capsys.readouterr().out
    assert cli.main(["--config", str(w.config), "qa", "status"]) == 0
    assert capsys.readouterr().out.endswith(listed)


def test_a_run_opens_as_its_report_with_its_screenshot(w):
    viewer = person(w.settings, "v@example.com", set())
    page = viewer.get("/qa/1/").text
    for words in ("Platform test, run 1", "The Sign in button does nothing", "Critical", "Press Sign in",
                  "The dashboard opens", "The page stays as it is", "Suspected cause in the code",
                  "https://gitlab.example.com/web/-/blob/x/src/login.ts#L12", "What was checked", "The catalogue"):
        assert words in page, words
    assert f'<span class="tag kind">{qa_report.TYPE_WORDS[qa_report.TYPE_SECURITY]}</span>' in page
    assert f'src="media/{SHOT}"' in page
    r = viewer.get(f"/qa/1/media/{SHOT}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == PNG
    assert viewer.get("/qa/7/").status_code == 404


@pytest.mark.parametrize("path", ["/qa/1/instructions.md", "/qa/1/result.json", "/qa/1/report.html",
                                  "/qa/1/media/../result.json", "/qa/1/media/..%2Fresult.json",
                                  "/qa/1/media/%2e%2e%2finstructions.md", "/qa/1/media/notours.png",
                                  "/qa/1/media/0123456789abcdef.sh", "/qa/../config.yaml", "/qa/1/media/"])
def test_only_the_reports_own_files_are_served(w, path):
    viewer = person(w.settings, "v@example.com", set())
    r = viewer.get(path)
    assert r.status_code == 404, (path, r.status_code)
    assert b"hunter2" not in r.content and b"sha256" not in r.content


def test_qa_starts_and_stops_a_run_and_it_records_who_asked(w):
    qa = person(w.settings, "q@example.com", {"qa"})
    r = post(qa, "/qa/", {"scenario": "1"})
    assert r.status_code == 303 and "Run+2+queued" in r.headers["location"]
    s = ReviewStore.read_only(w.tmp / "runner.db")
    run = s.run("qa-2")
    assert run.state == "pending" and run.source == "command" and run.requested_by == "q@example.com"
    assert run.scenario == 1
    s.close()
    r = post(qa, "/qa/", {"scenario": "1"})
    assert r.status_code == 400 and "Run 2 of this scenario is queued: one at a time" in r.text
    r = post(qa, "/qa/", {})
    assert r.status_code == 400 and "Pick the scenario to run" in r.text
    r = post(qa, "/qa/2/stop", {}, page="/qa/")
    assert r.status_code == 303
    s = ReviewStore.read_only(w.tmp / "runner.db")
    assert s.run("qa-2").stop_requested == 1
    s.close()
    with ConsoleStore(w.settings.db_path) as cs:
        assert {j["action"] for j in cs.journal()} >= {"qa_run_queued", "qa_run_stop_asked"}


def test_others_cannot_start_or_stop_a_run_by_posting_directly(w):
    for roles in (set(), {"architect"}, {"operator"}):
        c = person(w.settings, f"{'-'.join(sorted(roles)) or 'none'}@example.com", roles)
        token = csrf_of(c.get("/qa/").text)
        assert c.post("/qa/", data={"csrf": token}).status_code == 403
        assert c.post("/qa/1/stop", data={"csrf": token}).status_code == 403
    s = ReviewStore.read_only(w.tmp / "runner.db")
    assert [r.n for r in s.runs("qa")] == [1]
    s.close()


def test_the_instructions_are_edited_by_qa_and_hidden_from_everyone_else(w):
    w.instructions.write_text("<!-- what to test -->\nhttps://staging.example.com, admin / hunter2\n")
    w.instructions.chmod(0o640)
    for roles in (set(), {"architect"}, {"operator"}):
        c = person(w.settings, f"x{'-'.join(sorted(roles))}@example.com", roles)
        r = c.get("/qa/scenarios/1/instructions/")
        assert r.status_code == 403 and "hunter2" not in r.text
    qa = person(w.settings, "q@example.com", {"qa"})
    page = qa.get("/qa/scenarios/1/instructions/").text
    assert "hunter2" in page and "bytes of instructions" in page
    r = post(qa, "/qa/scenarios/1/instructions/", {"text": "https://staging.example.com\r\nadmin / swordfish\r\n",
                                       "sha": re.search(r'name="sha" value="([^"]+)"', page).group(1),
                                       "note": "new password"})
    assert r.status_code == 303
    assert w.instructions.read_text() == "https://staging.example.com\nadmin / swordfish\n"
    assert w.instructions.stat().st_mode & 0o777 == 0o640  # the file keeps its own mode
    assert not list(w.instructions.parent.glob(".*tmp"))  # no temporary file left beside it
    page = qa.get("/qa/scenarios/1/instructions/").text
    assert "new password" in page and "Quinn" not in page


def test_a_save_over_a_newer_version_is_refused_with_the_difference(w):
    w.instructions.write_text("first\n")
    qa = person(w.settings, "q@example.com", {"qa"})
    page = qa.get("/qa/scenarios/1/instructions/").text
    sha = re.search(r'name="sha" value="([^"]+)"', page).group(1)
    w.instructions.write_text("changed over SSH\n")  # someone else, meanwhile
    r = qa.post("/qa/scenarios/1/instructions/", data={"csrf": csrf_of(page), "sha": sha, "text": "mine\n"})
    assert r.status_code == 409 and "Someone changed the file" in r.text
    assert "-changed over SSH" in r.text and "+mine" in r.text
    assert w.instructions.read_text() == "changed over SSH\n"
    # The page now carries the new version's hash: saving again replaces it, knowingly.
    sha2 = re.search(r'name="sha" value="([^"]+)"', r.text).group(1)
    r = qa.post("/qa/scenarios/1/instructions/", data={"csrf": csrf_of(r.text), "sha": sha2, "text": "mine\n"})
    assert r.status_code == 303 and w.instructions.read_text() == "mine\n"


def test_every_saved_version_is_kept_and_an_earlier_one_comes_back(w):
    w.instructions.write_text("v1\n")
    admin = signed_in(w.settings, "owner@example.com")
    for text in ("v2\n", "v3\n"):
        page = admin.get("/qa/scenarios/1/instructions/").text
        post(admin, "/qa/scenarios/1/instructions/", {"text": text, "sha": re.search(r'name="sha" value="([^"]+)"', page).group(1)})
    with ConsoleStore(w.settings.db_path) as cs:
        versions = cs.versions("qa.instructions")
    assert [v["after_text"] for v in versions] == ["v3\n", "v2\n"]
    assert versions[1]["before_text"] == "v1\n" and versions[0]["actor"].startswith("Owner")
    page = admin.get(f"/qa/scenarios/1/instructions/?version={versions[1]['id']}").text
    assert "It is not saved yet" in page and ">v2\n</textarea>" in page


def test_the_page_warns_when_the_agent_would_get_nothing(w):
    w.instructions.write_text("<!-- comments only -->\n")
    qa = person(w.settings, "q@example.com", {"qa"})
    page = qa.get("/qa/scenarios/1/instructions/").text
    assert "is empty (comments only)" in page and "a run would stop at once" in page


def test_a_status_line_breaks_a_long_path_inside_its_card():
    """The status line under "What the agent gets" carries the file's full path, which has no spaces to break at:
    the line itself wraps it, whatever its tone, so the path stays inside the narrow card and whole. The same path
    above the editor wraps too, or on a phone it would stretch the whole form past the screen, and so does a notice,
    which shows the same path when the file cannot be read."""
    from arceus_runner.console.assets import CONSOLE_CSS
    rule = re.search(r"^\.value-line \{([^}]*)\}", CONSOLE_CSS, re.M).group(1)
    assert "overflow-wrap: anywhere" in rule
    assert re.search(r"^\.file-split \.lit \{[^}]*overflow-wrap: anywhere", CONSOLE_CSS, re.M)
    assert re.search(r"^\.notice \{[^}]*overflow-wrap: anywhere", CONSOLE_CSS, re.M)



# -- scenarios ---------------------------------------------------------------------------------------------------

def new_scenario(c: TestClient, **fields):
    page = c.get("/qa/scenarios/new").text
    data = {"csrf": csrf_of(page), "name": "Smoke", "day": ["mon", "fri"], "at": "07:30",
            "text": "https://staging.example.com\nSign in and open the dashboard.\n"} | fields
    return c.post("/qa/scenarios/new", data=data, follow_redirects=False)


def test_qa_makes_scenarios_each_with_its_name_schedule_and_instructions(w):
    qa = person(w.settings, "q@example.com", {"qa"})
    r = new_scenario(qa)
    assert r.status_code == 303 and "Smoke" in r.headers["location"]
    r = new_scenario(qa, name="Regression", day=["sat"], at="2:00", text="Everything, slowly.\n")
    assert r.status_code == 303
    r = new_scenario(qa, name="Payments", day=[], at="", text="Pay with the test card.\n")
    assert r.status_code == 303
    s = ReviewStore.read_only(w.tmp / "runner.db")
    by_name = {sc.name: sc for sc in s.scenarios()}
    s.close()
    assert list(by_name) == ["Main", "Smoke", "Regression", "Payments"]
    smoke, reg, pay = by_name["Smoke"], by_name["Regression"], by_name["Payments"]
    assert (json.loads(smoke.days), smoke.at, smoke.scheduled) == (["mon", "fri"], "07:30", 1)
    assert (json.loads(reg.days), reg.at, reg.scheduled) == (["sat"], "02:00", 1)
    assert pay.scheduled == 0
    folder = w.tmp / "qa" / "scenarios"
    assert (folder / f"{smoke.n}.md").read_text() == "https://staging.example.com\nSign in and open the dashboard.\n"
    assert (folder / f"{reg.n}.md").read_text() == "Everything, slowly.\n"
    page = qa.get("/qa/").text
    for words in ("Smoke", "Mon, Fri at 07:30", "Regression", "Sat at 02:00", "Payments", "By hand only",
                  "Times are in Europe/Kyiv", "New scenario"):
        assert words in page, words
    assert page.count("Run now") == 4
    # Opened again, the form has what was saved.
    page = qa.get(f"/qa/scenarios/{smoke.n}/").text
    assert 'value="Smoke"' in page and 'value="07:30"' in page
    assert 'value="mon" checked' in page and 'value="fri" checked' in page and 'value="tue" checked' not in page
    # No separate switch: days and a time are the schedule, and the form says what they do.
    assert 'name="scheduled"' not in page and "Runs by itself on the days and at the time picked." in page
    page = qa.get(f"/qa/scenarios/{pay.n}/").text
    assert 'Runs only when someone presses "Run now".' in page
    # Its instructions open in the versioned editor, under a history of their own.
    page = qa.get(f"/qa/scenarios/{smoke.n}/instructions/").text
    assert "Sign in and open the dashboard." in page and "Instructions: Smoke" in page
    with ConsoleStore(w.settings.db_path) as cs:
        assert [v["note"] for v in cs.versions(f"qa.instructions.{smoke.n}")] == ["Scenario created"]
        assert cs.versions("qa.instructions") == []


@pytest.mark.parametrize("fields, words", [
    ({"name": "  "}, "Give the scenario a name"),
    ({"name": "main"}, "exists already"),
    ({"day": []}, "Pick the days to run on, or clear the time"),
    ({"at": ""}, "Set the time to run at, or clear the days"),
    ({"at": "25:00"}, "The time is HH:MM"),
    ({"day": ["mon", "someday"]}, "Unknown day"),
    ({"text": "<!-- only a comment -->"}, "Write what the scenario tests"),
])
def test_a_scenario_that_cannot_be_kept_is_refused_with_the_text_still_in_the_form(w, fields, words):
    qa = person(w.settings, "q@example.com", {"qa"})
    r = new_scenario(qa, **fields)
    assert r.status_code == 400 and words in r.text
    s = ReviewStore.read_only(w.tmp / "runner.db")
    assert [sc.name for sc in s.scenarios()] == ["Main"]
    s.close()
    assert not (w.tmp / "qa" / "scenarios").exists()
    if "text" not in fields:
        assert "Sign in and open the dashboard." in r.text


def test_no_more_scenarios_than_the_limit(w):
    w.raw["qa"]["max_scenarios"] = 2
    w.config.write_text(yaml.safe_dump(w.raw))
    qa = person(w.settings, "q@example.com", {"qa"})
    assert new_scenario(qa).status_code == 303
    page = qa.get("/qa/").text
    assert "New scenario" not in page and "2 of 2" in page
    r = new_scenario(qa, name="Regression")
    assert r.status_code == 400 and "qa.max_scenarios" in r.text


def test_a_scenario_is_changed_and_a_save_over_a_newer_one_is_refused(w):
    qa = person(w.settings, "q@example.com", {"qa"})
    new_scenario(qa)
    page = qa.get("/qa/scenarios/2/").text
    token, version = csrf_of(page), re.search(r'name="version" value="(\d+)"', page).group(1)
    other = person(w.settings, "q2@example.com", {"qa"})
    page2 = other.get("/qa/scenarios/2/").text
    r = other.post("/qa/scenarios/2/", data={"csrf": csrf_of(page2), "version": version, "name": "Smoke test",
                                             "day": ["tue"], "at": "08:00"},
                   follow_redirects=False)
    assert r.status_code == 303
    r = qa.post("/qa/scenarios/2/", data={"csrf": token, "version": version, "name": "Smoke",
                                          "day": ["wed"], "at": "09:00"}, follow_redirects=False)
    assert r.status_code == 409 and "Someone changed this scenario" in r.text
    assert "Smoke test" in r.text and 'value="wed" checked' in r.text  # theirs said, mine kept in the form
    s = ReviewStore.read_only(w.tmp / "runner.db")
    sc = s.scenario(2)
    s.close()
    assert (sc.name, json.loads(sc.days), sc.at, sc.scheduled) == ("Smoke test", ["tue"], "08:00", 1)
    # Saved again from the refused page, knowingly.
    version = re.search(r'name="version" value="(\d+)"', r.text).group(1)
    r = qa.post("/qa/scenarios/2/", data={"csrf": csrf_of(r.text), "version": version, "name": "Smoke",
                                          "day": ["wed"], "at": "09:00"}, follow_redirects=False)
    assert r.status_code == 303 and "wed+at+09%3A00" in r.headers["location"]
    # Days and time cleared: it runs only by hand.
    page = qa.get("/qa/scenarios/2/").text
    r = qa.post("/qa/scenarios/2/", data={"csrf": csrf_of(page), "name": "Smoke", "at": "",
                                          "version": re.search(r'name="version" value="(\d+)"', page).group(1)},
                follow_redirects=False)
    assert r.status_code == 303 and "by+hand+only" in r.headers["location"]
    s = ReviewStore.read_only(w.tmp / "runner.db")
    sc = s.scenario(2)
    s.close()
    assert (json.loads(sc.days), sc.at, sc.scheduled) == ([], None, 0)


def test_a_deleted_scenario_leaves_the_list_and_its_past_runs_keep_its_name(w):
    qa = person(w.settings, "q@example.com", {"qa"})
    new_scenario(qa)
    post(qa, "/qa/", {"scenario": "2"})
    page = qa.get("/qa/scenarios/2/").text
    r = qa.post("/qa/scenarios/2/delete", data={"csrf": csrf_of(page),
                                                "version": re.search(r'name="version" value="(\d+)"', page).group(1)},
                follow_redirects=False)
    assert r.status_code == 303 and "deleted" in r.headers["location"]
    page = qa.get("/qa/").text
    assert 'href="/qa/scenarios/2/"' not in page
    assert '<span class="run-scenario">Smoke</span>' in page  # its queued run still says whose it is
    assert qa.get("/qa/scenarios/2/").status_code == 404
    assert qa.get("/qa/scenarios/2/instructions/").status_code == 404
    r = post(qa, "/qa/", {"scenario": "2"})
    assert r.status_code == 400 and "no longer exists" in r.text


def test_only_qa_and_admins_make_change_or_delete_scenarios(w):
    for roles in (set(), {"architect"}, {"operator"}):
        c = person(w.settings, f"{'-'.join(sorted(roles)) or 'none'}@example.com", roles)
        token = csrf_of(c.get("/qa/").text)
        assert c.get("/qa/scenarios/new").status_code == 403
        assert c.get("/qa/scenarios/1/").status_code == 403
        assert c.post("/qa/scenarios/new", data={"csrf": token, "name": "X", "text": "x"}).status_code == 403
        assert c.post("/qa/scenarios/1/", data={"csrf": token, "name": "X", "version": "1"}).status_code == 403
        assert c.post("/qa/scenarios/1/delete", data={"csrf": token, "version": "1"}).status_code == 403
        assert c.get("/qa/instructions/").status_code == 403
    s = ReviewStore.read_only(w.tmp / "runner.db")
    assert [(sc.name, sc.version) for sc in s.scenarios()] == [("Main", 1)]
    s.close()


def test_the_old_address_of_the_instructions_opens_the_first_scenario_s(w):
    qa = person(w.settings, "q@example.com", {"qa"})
    r = qa.get("/qa/instructions/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/qa/scenarios/1/instructions/"


def test_the_command_queues_a_named_scenario_and_asks_which_when_there_are_several(w, capsys):
    from arceus_runner import cli
    args = ["--config", str(w.config), "qa"]
    assert cli.main([*args, "scenario", "add", "Smoke", "--days", "mon,fri", "--at", "07:30"]) == 0
    out = capsys.readouterr().out
    assert "Scenario 2 \u201cSmoke\u201d made: Mon, Fri at 07:30" in out and "scenarios/2.md" in out
    assert cli.main([*args, "run"]) == 1
    assert "name one with --scenario N" in capsys.readouterr().err
    assert cli.main([*args, "run", "--scenario", "2"]) == 0
    assert "\u201cSmoke\u201d queued" in capsys.readouterr().out
    assert cli.main([*args, "run", "--scenario", "2"]) == 1
    assert "is queued; nothing more was queued" in capsys.readouterr().err
    assert cli.main([*args, "run", "--scenario", "1"]) == 0  # another scenario queues behind it
    assert cli.main([*args, "scenario", "set", "2", "--by-hand"]) == 0
    assert "By hand only" in capsys.readouterr().out
    assert cli.main([*args, "status"]) == 0
    out = capsys.readouterr().out
    assert "run 2 [Smoke]: pending" in out and "run 3 [Main]: pending" in out
    assert cli.main([*args, "scenario", "delete", "2"]) == 0
    capsys.readouterr()
    assert cli.main([*args, "scenario", "list"]) == 0
    assert "Smoke" not in capsys.readouterr().out
