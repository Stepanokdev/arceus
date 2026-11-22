"""Advisory triage in the console, for reading: the list of triage runs, a run's report rendered from its result.json,
a run without one as where it is, and where the reports are read with the console on and off."""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

import pytest
import yaml

pytest.importorskip("starlette")

from arceus_runner import secops  # noqa: E402
from arceus_runner.config import load_config  # noqa: E402
from arceus_runner.console.web import Settings  # noqa: E402
from arceus_runner.db import ReviewStore  # noqa: E402
from arceus_runner.workspace_run import report_address  # noqa: E402

from tests.conftest import example_raw  # noqa: E402
from tests.test_console_auth import client, make_admin, signed_in  # noqa: E402
from tests.test_console_qa import person  # noqa: E402

SHA = "c0ffee" * 6
CODE = f"https://gitlab.example.com/acme/acme-platform/-/blob/{SHA}/docker/Dockerfile#L2"
SUMMARY = "The platform image pins `acme-imagelib==2.3.0` <b>unpatched</b>; the rest does not carry it."
# A finished run's result.json, as SecOpsAgent.view writes it.
VIEW = {"n": 1, "id": "secops-1", "source": "schedule", "requested_by": "schedule", "outcome": "attention",
        "created_at": "2026-11-14T01:00:00Z", "started_at": "2026-11-14T01:00:05Z",
        "finished_at": "2026-11-14T02:10:00Z", "register": "files", "summary": SUMMARY,
        "sending": {"why": "", "sent": 1, "known": 0, "refused": 0, "sending": 0, "unsent": 0},
        "repos": [{"repo": "acme-platform", "default": "main", "sha": SHA, "switch": True}],
        "problems": [], "fatal": [], "doubts": [],
        "advisories": [{"id": 11, "ref": "SEC-11", "title": "Heap overflow in acme-imagelib's PNG decoder",
                        "url": "https://register.example.com/advisories/11", "identifiers": ["CVE-2099-1011"],
                        "severity": "high", "exploited": False, "note": "Read against every Dockerfile.",
                        "left_out": None, "unaccounted": False,
                        "verdicts": [{"advisory": 11, "repo": "acme-platform", "verdict": "affected",
                                      "evidence": "Pins `acme-imagelib==2.3.0`, below the fixed 2.4.1.",
                                      "where": [{"repo": "acme-platform", "file": "docker/Dockerfile", "line": 2,
                                                 "sha": SHA, "url": CODE}],
                                      "sent": "Sent to the register"}]}]}


@pytest.fixture
def w(tmp_path: Path):
    raw = example_raw()
    raw["paths"] = {"db": str(tmp_path / "runner.db"), "secrets": str(tmp_path / "secrets.env")}
    raw["secops"] = {"enabled": True, "register": {"type": "files", "advisories": str(tmp_path / "osv")},
                     "reports": str(tmp_path / "secops" / "runs"), "instructions": str(tmp_path / "notes.md")}
    raw["metrics"] = {"enabled": False}  # a run's page asks the runner nothing
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(raw))
    settings = Settings(config_path=config, db_path=tmp_path / "console" / "console.db")
    make_admin(settings)
    store = ReviewStore(tmp_path / "runner.db")
    run = store.add_run("secops", "schedule", "schedule")
    store.update_run(run.id, state="done", outcome="attention", started_at=VIEW["started_at"],
                     finished_at=VIEW["finished_at"], result=json.dumps({"summary": SUMMARY, "verdicts": [
                         {"verdict": "affected"}, {"verdict": "not_affected"}]}))
    store.close()
    (tmp_path / "secops" / "runs" / "1").mkdir(parents=True)
    (tmp_path / "secops" / "runs" / "1" / "result.json").write_text(json.dumps(VIEW))

    class W:
        def save(self):
            config.write_text(yaml.safe_dump(raw))
    x = W()
    x.settings, x.tmp, x.config, x.raw, x.db = settings, tmp_path, config, raw, tmp_path / "runner.db"
    x.result = tmp_path / "secops" / "runs" / "1" / "result.json"
    return x


def add_run(w, state: str, **fields) -> None:
    s = ReviewStore(w.db)
    run = s.add_run("secops", "command", "s@example.com")
    s.update_run(run.id, state=state, **fields)
    s.close()


def test_the_list_shows_each_run_in_its_reports_words_newest_first_and_starts_nothing(w):
    add_run(w, "publishing", stop_requested=1, started_at="2026-11-15T01:00:00Z")
    page = signed_in(w.settings, "owner@example.com").get("/secops/").text  # an admin, who may do anything else
    assert '<a href="/secops/1/">Run 1</a>' in page and '<span class="blocked">1 verdict needs attention</span>' in page
    assert ('<span class="run-summary">The platform image pins <code class="lit">acme-imagelib==2.3.0</code> '
            '&lt;b&gt;unpatched&lt;/b&gt;; the rest does not carry it.</span>') in page  # as the runner's own list
    assert "Schedule" in page and "14 Nov 2026, 03:00" in page and "15 Nov 2026, 03:00" in page  # started, in Kyiv
    assert page.index("Run 2") < page.index("Run 1")
    assert "Sending the verdicts" in page and "Stop asked" in page and "s@example.com" in page
    main = page.split("<main>", 1)[1]  # the bar above it has the sign-out form
    assert "<form" not in main  # nothing to start or stop
    assert '<a class="btn" href="/secops/instructions/">Triage notes</a>' in main  # the admin's way to the notes
    operator = person(w.settings, "o@example.com", {"operator"})
    assert "/secops/instructions/" not in operator.get("/secops/").text  # admins edit them, nobody else


def test_with_no_runs_the_list_says_so_and_why_none_would_start(w):
    ReviewStore(w.tmp / "empty.db").close()
    w.raw["paths"]["db"] = str(w.tmp / "empty.db")
    w.save()
    viewer = person(w.settings, "v@example.com", set())
    page = viewer.get("/secops/").text
    assert "No runs yet." in page and "The runner is in mode: observe, which starts nothing" in page
    w.raw["secops"]["enabled"] = False
    w.save()
    page = viewer.get("/secops/").text
    assert "Advisory triage is off in config.yaml (secops.enabled: false): no run starts." in page
    assert "which starts nothing" not in page


def test_a_run_opens_as_its_report_from_result_json_with_a_link_back_to_the_list(w):
    viewer = person(w.settings, "v@example.com", set())
    page = viewer.get("/secops/1/").text
    _, body = secops.run_body(VIEW, "acme-shop", load_config(w.config).schedule.tz)
    assert body in page  # the runner's own report, inside the console's page
    assert "<title>Run 1: 1 verdict needs attention · acme-shop</title>" in page
    assert '<a href="https://register.example.com/advisories/11">SEC-11</a>' in page and "CVE-2099-1011" in page
    assert '<span class="tag problem">Affected</span>' in page and '<code class="lit">acme-imagelib==2.3.0</code>' \
        in page
    assert f'<a href="{CODE}">' in page and "Sent to the register" in page
    assert '<a href="../">All triage runs of acme-shop</a>' in page
    back = viewer.get(urllib.parse.urljoin("/secops/1/", "../"))
    assert back.url.path == "/secops/" and '<h1>Advisory triage</h1>' in back.text


def test_a_run_without_a_report_shows_where_it_is_and_no_stop_button(w):
    add_run(w, "publishing", started_at="2026-11-15T01:00:00Z")
    admin = signed_in(w.settings, "owner@example.com")  # can("secops.run") would raise: there is no such action
    page = admin.get("/secops/2/").text
    assert "Advisory triage, run 2" in page and 'href="/secops/">Advisory triage</a> / Run 2' in page
    assert 'data-refresh="/secops/2/live"' in page and "gives a verdict per advisory and repository" in page
    live = admin.get("/secops/2/live")
    assert live.status_code == 200 and "<html" not in live.text
    assert "Sending the verdicts" in live.text and "Writing the report" not in live.text
    assert "Stop this run" not in page + live.text
    s = ReviewStore(w.db)
    s.update_run("secops-2", state="done", outcome="unfinished", reason="the register did not answer")
    s.close()
    assert 'data-reason="no_report"' in admin.get("/secops/2/").text


def test_an_unknown_run_or_a_linked_report_is_not_found_and_an_unreadable_one_says_so(w):
    viewer = person(w.settings, "v@example.com", set())
    assert viewer.get("/secops/9/").status_code == 404 and viewer.get("/secops/9/live").status_code == 404
    elsewhere = w.tmp / "elsewhere.json"
    elsewhere.write_text(json.dumps(VIEW))
    w.result.unlink()
    w.result.symlink_to(elsewhere)
    assert viewer.get("/secops/1/").status_code == 404
    w.result.unlink()
    w.result.write_text("{not json")
    r = viewer.get("/secops/1/")
    assert r.status_code == 500 and "report cannot be read" in r.text


def test_signed_out_the_section_sends_to_the_sign_in(w):
    c = client(w.settings)
    for path in ("/secops/", "/secops/1/"):
        r = c.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login?" + urllib.parse.urlencode({"next": path})
    r = c.get("/secops/1/live")
    assert r.status_code == 401 and SUMMARY not in r.text


def test_the_bar_has_the_section_after_architecture_for_everyone(w):
    page = person(w.settings, "v@example.com", set()).get("/secops/").text
    nav = page.split('aria-label="Sections"', 1)[1].split("</nav>", 1)[0]
    here = '<a href="/secops/" aria-current="page">Advisory triage</a>'
    assert nav.index('href="/architecture/"') < nav.index(here) < nav.index('href="/grafana/"')


def test_the_reports_are_read_in_the_console_while_it_is_on_and_on_the_runners_page_while_it_is_off(w):
    cfg = load_config(w.config)
    assert report_address(cfg, "secops", 3302, 1) == ("http://localhost:3302/1/", True)
    assert secops.check(cfg)[0].detail.endswith("reports on http://localhost:3302/ (through the SSH tunnel)")
    w.raw["console"] = {"enabled": True, "public_url": "https://console.example.com"}
    w.save()
    cfg = load_config(w.config)
    assert report_address(cfg, "secops", 3302, 1) == ("https://console.example.com/secops/1/", False)
    assert secops.check(cfg)[0].detail.endswith("reports on https://console.example.com/secops/")
