"""The test runs' reports (qa.py) as pages, and the small server that shows them on localhost.

Each run's folder under `qa.reports` holds `report.html`, `result.json`, the copy of the instructions it was given
and `media/` with its screenshots; `index.html` beside the folders lists every run. The pages are plain HTML with
their styles inline and nothing loaded from elsewhere, so a run's folder copied off the server still opens.

The server answers on `qa.address:qa.port` (127.0.0.1 by default, reached through an SSH tunnel like Grafana) and
serves exactly three kinds of path: `/` (the list, built from runner.db on every visit, so a queued or running run
shows there at once), `/<n>/` (a run's report) and `/<n>/media/<file>` (its screenshots). Nothing else under the folder is served: not the instructions, which hold the test accounts, not
result.json, not a link.
"""

from __future__ import annotations

import html
import re
import threading
from collections.abc import Callable
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from arceus_runner import media
from arceus_runner.literals import spans

HEADS = {"passed": "No problems found", "partial": "No problems in what was checked; some could not be checked",
         "found": None, "blocked": "Could not test",
         "unfinished": "Did not finish", "stopped": "Stopped"}
RESULT_WORDS = {"ok": "Works", "problem": "Problem", "blocked": "Could not check", "skipped": "Skipped"}
SEVERITY_WORDS = {"critical": "Critical", "major": "Major", "minor": "Minor"}
TYPE_AC = "ac"
TYPE_SECURITY = "security"
TYPE_UI = "ui"
TYPE_PERFORMANCE = "performance"
TYPE_OTHER = "other"
TYPE_WORDS = {TYPE_AC: "AC", TYPE_SECURITY: "Security", TYPE_UI: "UI/UX", TYPE_PERFORMANCE: "Performance",
              TYPE_OTHER: "Other"}
TYPES = tuple(TYPE_WORDS)
# What the report can be filtered to: the problems, then the checks by their result.
FILTERS = (("finding", "Findings"), ("ok", "Works"), ("problem", "Problem"), ("blocked", "Could not check"),
           ("skipped", "Skipped"))
NEW_TAB = ' target="_blank" rel="noopener"'

STYLE = """
:root {
  --bg: #f4f5f7; --surface: #ffffff; --ink: #1f2430; --muted: #5d6576; --line: #dfe3ea;
  --blue: #2f74c8; --good: #157f5b; --warn: #a66e00; --bad: #c03333; --quiet: #7a7f88;
  --bad-soft: #fbeaea; --warn-soft: #fbf3e2; --good-soft: #e6f4ee; --blue-soft: #e9f1fb;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #181b1f; --surface: #22252b; --ink: #e4e7ec; --muted: #a2a9b6; --line: #343840;
    --blue: #6ea8f0; --good: #3fbf8f; --warn: #e3a92c; --bad: #ef6b6b; --quiet: #8d929b;
    --bad-soft: #3a2326; --warn-soft: #3a3121; --good-soft: #1e352d; --blue-soft: #1f2d40;
    color-scheme: dark;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, "Helvetica Neue", Arial, sans-serif;
}
main { max-width: 920px; margin: 0 auto; padding: 32px 16px 64px; }
a { color: var(--blue); text-underline-offset: 2px; }
a:focus-visible, summary:focus-visible { outline: 2px solid var(--blue); outline-offset: 2px; border-radius: 3px; }
code { font: 0.875em/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; overflow-wrap: anywhere; }
code.lit { padding: 0 4px; border-radius: 4px; background: var(--bg); border: 1px solid var(--line); }
h1 { font-size: 1.75rem; line-height: 1.2; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 1.15rem; margin: 40px 0 12px; }
h3 { font-size: 1.05rem; line-height: 1.35; margin: 0; }
.crumb { margin: 0 0 20px; font-size: 0.9rem; }
.verdict { font-size: 1.15rem; font-weight: 600; margin: 0 0 16px; }
.verdict.passed { color: var(--good); } .verdict.found, .verdict.unfinished, .verdict.partial { color: var(--warn); }
.verdict.blocked { color: var(--bad); } .verdict.stopped { color: var(--quiet); }
.facts { display: grid; grid-template-columns: max-content 1fr; gap: 4px 20px; margin: 0; color: var(--muted);
         font-size: 0.925rem; }
.facts dt { color: var(--muted); } .facts dd { margin: 0; color: var(--ink); overflow-wrap: anywhere; }
.summary { margin: 24px 0 0; padding: 16px 20px; background: var(--surface); border: 1px solid var(--line);
           border-radius: 8px; white-space: pre-line; }
.alert { margin: 16px 0 0; padding: 12px 16px; border-radius: 8px; background: var(--bad-soft); }
.alert.note { background: var(--warn-soft); }
.alert p { margin: 0; } .alert ul { margin: 6px 0 0; padding-left: 20px; }
.alert .objections { margin: 8px 0 0; white-space: pre-line; }
.checks { list-style: none; margin: 0; padding: 0; background: var(--surface); border: 1px solid var(--line);
          border-radius: 8px; }
.checks li { display: grid; grid-template-columns: 9.5rem 1fr; gap: 12px; padding: 10px 16px;
             border-top: 1px solid var(--line); }
.checks li:first-child { border-top: 0; }
.checks .note { display: block; color: var(--muted); font-size: 0.9rem; }
.tag { display: inline-block; font-size: 0.8rem; font-weight: 600; padding: 1px 8px; border-radius: 999px;
       white-space: nowrap; align-self: start; justify-self: start; }
.tag.ok { color: var(--good); background: var(--good-soft); }
.tag.problem, .tag.critical { color: var(--bad); background: var(--bad-soft); }
.tag.major { color: var(--warn); background: var(--warn-soft); }
.tag.blocked, .tag.skipped, .tag.minor { color: var(--quiet); background: var(--bg); }
.tag.kind { color: var(--blue); background: var(--blue-soft); }
.finding { background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 20px;
           margin: 0 0 16px; }
.finding header { display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; }
.finding header h3 { flex: 1 1 18rem; }
.finding .meta { margin: 6px 0 0; color: var(--muted); font-size: 0.9rem; overflow-wrap: anywhere; }
.steps { margin: 16px 0 0; padding: 0; list-style: none; counter-reset: step; }
.steps > li { counter-increment: step; position: relative; padding: 6px 0 6px 40px; }
.steps > li::before { content: counter(step); position: absolute; left: 0; top: 6px; width: 26px; height: 26px;
                      border-radius: 50%; background: var(--bg); color: var(--muted); font-size: 0.8rem;
                      font-weight: 600; display: grid; place-items: center; }
.steps > li.failed::before { background: var(--bad); color: #fff; }
.steps > li.failed > .text { font-weight: 600; }
.shots { display: flex; flex-wrap: wrap; gap: 12px; margin: 10px 0 4px; }
.shots figure { margin: 0; max-width: 100%; }
.shots img, .shots video { display: block; max-width: min(100%, 420px); max-height: 320px; border-radius: 6px;
                           border: 1px solid var(--line); background: var(--bg); }
.shots figcaption { font-size: 0.85rem; color: var(--muted); margin-top: 4px; max-width: 420px; }
.compare { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 16px 0 0; }
.compare div { padding: 12px 14px; border-radius: 6px; background: var(--bg); }
.compare h4, .cause h4 { margin: 0 0 4px; font-size: 0.85rem; color: var(--muted); font-weight: 600; }
.compare p, .cause p { margin: 0; white-space: pre-line; }
.compare .actual { background: var(--bad-soft); }
.cause { margin: 16px 0 0; padding: 12px 14px; border: 1px dashed var(--line); border-radius: 6px; }
.cause ul { margin: 8px 0 0; padding-left: 20px; }
table { width: 100%; border-collapse: collapse; background: var(--surface); border: 1px solid var(--line);
        border-radius: 8px; overflow: hidden; }
th, td { text-align: left; padding: 10px 14px; border-top: 1px solid var(--line); vertical-align: top; }
th { font-size: 0.85rem; color: var(--muted); font-weight: 600; border-top: 0; }
td.nowrap { white-space: nowrap; }
td .passed { color: var(--good); } td .found, td .unfinished, td .partial { color: var(--warn); }
td .blocked { color: var(--bad); } td .stopped { color: var(--quiet); }
.run-summary { color: var(--muted); font-size: 0.9rem; display: block; margin-top: 2px; }
.run-scenario { color: var(--muted); font-size: 0.875rem; display: block; }
.empty { padding: 24px; background: var(--surface); border: 1px solid var(--line); border-radius: 8px; }
.qa-radio { position: absolute; width: 1px; height: 1px; opacity: 0; pointer-events: none; }
.qa-filter { display: flex; flex-wrap: wrap; gap: 8px; margin: 32px 0 0; }
.qa-filter label, .qa-filter .qa-off { padding: 4px 12px; border: 1px solid var(--line); border-radius: 999px;
                                       background: var(--surface); font-size: 0.9rem; cursor: pointer; }
.qa-filter b { margin-left: 4px; color: var(--muted); font-weight: 600; }
.qa-filter .qa-off { opacity: 0.5; cursor: default; }
.qa-filter.qa-types { margin: 0 0 12px; }
.qa-radio:focus-visible ~ .qa-filter { outline: 2px solid var(--blue); outline-offset: 4px; border-radius: 8px; }
.qa-group h3 { margin: 20px 0 8px; font-size: 1rem; color: var(--muted); }
.qa-item { margin: 0 0 8px; background: var(--surface); border: 1px solid var(--line); border-radius: 8px; }
.qa-item > summary { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: baseline; padding: 10px 16px;
                     cursor: pointer; list-style: none; color: var(--ink); }
.qa-item > summary::-webkit-details-marker { display: none; }
.qa-item > summary::before { content: "\\25B8"; width: 1em; color: var(--muted); }
.qa-item[open] > summary::before { content: "\\25BE"; }
.qa-item:target { border-color: var(--blue); box-shadow: 0 0 0 2px var(--blue-soft); }
.qa-item:target::details-content { content-visibility: visible; }
.qa-title { flex: 1 1 16rem; font-weight: 600; }
.qa-chip { padding: 0 6px; border: 1px solid var(--line); border-radius: 4px; color: var(--muted);
           font: 0.8rem/1.6 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; white-space: nowrap; }
.qa-meta { color: var(--muted); font-size: 0.85rem; white-space: nowrap; }
.qa-body { padding: 4px 16px 16px; border-top: 1px solid var(--line); }
.qa-body .meta { margin: 12px 0 0; color: var(--muted); font-size: 0.9rem; overflow-wrap: anywhere; }
.qa-spec { margin: 12px 0 0; padding: 10px 14px; background: var(--blue-soft); border-left: 3px solid var(--blue);
           border-radius: 0 6px 6px 0; }
.qa-spec h4 { margin: 0 0 4px; font-size: 0.85rem; color: var(--muted); font-weight: 600; }
.qa-spec p { margin: 0; white-space: pre-line; }
.qa-none { color: var(--muted); }
.qa-tried { margin: 12px 0 0; font-weight: 600; }
.qa-note { margin: 12px 0 0; }
.qa-cases { list-style: none; margin: 12px 0 0; padding: 0; }
.qa-cases li { display: grid; grid-template-columns: 9.5rem 1fr; gap: 12px; padding: 6px 0;
               border-top: 1px solid var(--line); }
.qa-cases li:first-child { border-top: 0; }
.qa-cases .note { display: block; color: var(--muted); font-size: 0.9rem; }
.qa-open { display: block; margin-top: 4px; font-size: 0.85rem; }
footer { margin-top: 48px; color: var(--muted); font-size: 0.875rem; }
footer ul { padding-left: 20px; }
@media (max-width: 640px) {
  main { padding-top: 20px; }
  .compare { grid-template-columns: 1fr; }
  .checks li, .qa-cases li { grid-template-columns: 1fr; gap: 4px; }
  .facts { grid-template-columns: 1fr; gap: 0; } .facts dd { margin-bottom: 6px; }
  .hide-narrow { display: none; }
}
"""


def _filter_css() -> str:
    """
    The filter's rules, one set per kind of FILTERS: its label lit, the other list hidden, and for a check result
    the checks and groups of other results hidden.
    """
    results = [kind for kind, __ in FILTERS if kind != "finding"]
    lit = ",\n".join(f'#qa-show-{kind}:checked ~ .qa-filter label[for="qa-show-{kind}"]'
                     for kind in ["all", *(kind for kind, __ in FILTERS)])
    hide = ["#qa-show-finding:checked ~ .qa-checks"]
    hide += [f"#qa-show-{kind}:checked ~ .qa-findings" for kind in results]
    for kind in results:
        hide += [f"#qa-show-{kind}:checked ~ .qa-checks .qa-item:not(.{kind})",
                 f"#qa-show-{kind}:checked ~ .qa-checks .qa-group:not(:has(.qa-item.{kind}))"]
    lit += ",\n" + ",\n".join(f'#qa-type-{kind}:checked ~ .qa-findings .qa-types label[for="qa-type-{kind}"]'
                              for kind in ["all", *TYPES])
    hide += [f"#qa-type-{kind}:checked ~ .qa-findings .qa-finding:not(.qa-type-{kind})" for kind in TYPES]
    hidden = ",\n".join(hide)
    return (f"{lit} {{\n  color: var(--blue); background: var(--blue-soft); border-color: var(--blue); }}\n"
            f"{hidden} {{ display: none; }}\n")


STYLE += _filter_css()


def e(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def rich(value: object) -> str:
    """An agent's text as HTML: escaped, with the code in it (backticks, file paths) set as code (literals.py)."""
    return "".join(e(p) if kind == "text" else f'<code class="lit">{e(p)}</code>'
                   for p, kind in spans(str(value or "")))


def _when(iso: str | None, tz: ZoneInfo) -> str:
    if not iso:
        return ""
    try:
        at = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz)
    except ValueError:
        return iso
    return f"{at:%a %d %b %Y, %H:%M}"


def _took(start: str | None, end: str | None) -> str:
    try:
        a = datetime.fromisoformat((start or "").replace("Z", "+00:00"))
        b = datetime.fromisoformat((end or "").replace("Z", "+00:00"))
    except ValueError:
        return ""
    minutes = max(0, round((b - a).total_seconds() / 60))
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60:02d} min"


DISPUTED_PARTIAL = "No problems found, but the report did not pass its closing review"


def head_of(outcome: str | None, findings: int, disputed: bool = False) -> str:
    """The result in a few words. `disputed`: the report did not pass its closing review (qa.outcome_of makes such a
    run that found nothing partial, though it may have checked everything)."""
    if outcome == "partial" and disputed:
        return DISPUTED_PARTIAL
    if outcome == "found":
        return f"{findings} problem{'s' if findings != 1 else ''} found"
    return HEADS.get(outcome or "", None) or "In progress"


def _page(title: str, body: str, style: str = "") -> str:
    """A whole page: its styles inline (the reports' own, then `style`), nothing loaded from elsewhere."""
    return (f'<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<meta name="robots" content="noindex">\n<title>{e(title)}</title>\n<style>{STYLE}{style}</style>\n'
            f'</head>\n<body>\n<main>\n{body}\n</main>\n</body>\n</html>\n')


def _shot(shot: dict) -> str:
    """
    A screenshot or recording; either opens on its own in a new tab.
    """
    src = f"media/{e(shot['file'])}"
    caption = f"<figcaption>{rich(shot['caption'])}</figcaption>" if shot.get("caption") else ""
    if shot.get("kind") == "video":
        thing = (f'<video src="{src}" controls preload="metadata"></video>'
                 f'<a class="qa-open" href="{src}"{NEW_TAB}>Open in a new tab</a>')
    else:
        thing = f'<a href="{src}"{NEW_TAB}><img src="{src}" alt="{e(shot.get("caption") or "Screenshot")}"></a>'
    return f"<figure>{thing}{caption}</figure>"


def _spec_ref(spec: dict | None) -> str:
    """
    The requirement's numbers as a chip for an accordion's line: `US-2 · 2.3`.
    """
    numbers = [spec.get(key) for key in ("story", "point") if spec and spec.get(key)]
    return f'<span class="qa-chip">{e(" · ".join(numbers))}</span>' if numbers else ""


def _requirement(spec: dict | None) -> str:
    """
    The requirement a check or finding is about: its story, its point and its text, as the tester cited them.
    """
    if not spec:
        return ('<div class="qa-spec"><h4>Requirement</h4><p class="qa-none">No requirement cited by the tester.</p>'
                "</div>")
    where = ", ".join(label for label in (f"Story {e(spec['story'])}" if spec.get("story") else "",
                                          f"point {e(spec['point'])}" if spec.get("point") else "") if label)
    text = f'"{rich(spec["text"])}"' if spec.get("text") else ""
    line = f"<strong>{where}</strong>{': ' if where and text else ''}{text}"
    return f'<div class="qa-spec"><h4>Requirement</h4><p>{line}</p></div>'


def _check(check: dict) -> str:
    """
    One check as an accordion: its result, what it was, its requirement and the cases it was made of. A check that
    could not be done or was skipped says whether any of it was tried.
    """
    result = check["result"]
    listed = check.get("cases")  # None: the tester did not list them (and reports from before cases)
    cases = listed or []
    tally = ""
    if cases:
        passed = sum(case["result"] == "ok" for case in cases)
        tally = f'<span class="qa-meta">{passed} of {len(cases)} cases ok</span>'
    summary = (f'<summary><span class="tag {e(result)}">{RESULT_WORDS.get(result, e(result))}</span>'
               f'<span class="qa-title">{rich(check["what"])}</span>{_spec_ref(check.get("spec"))}{tally}</summary>')
    body = [_requirement(check.get("spec"))]
    if listed is None:
        body.append('<p class="qa-none">The tester did not list the cases checked.</p>')
    elif result in ("blocked", "skipped"):
        tried = sum(case["result"] in ("ok", "problem") for case in cases)
        body.append(f'<p class="qa-tried">Tried {tried} of {len(cases)} cases before it stopped.</p>' if tried
                    else '<p class="qa-tried">Nothing of this check was tried.</p>')
    if cases:
        body.append('<ul class="qa-cases">' + "".join(
            f'<li><span class="tag {e(case["result"])}">{RESULT_WORDS.get(case["result"], e(case["result"]))}</span>'
            f'<span>{rich(case["what"])}'
            + (f'<span class="note">{rich(case["note"])}</span>' if case.get("note") else "") + "</span></li>"
            for case in cases) + "</ul>")
    if check.get("note"):
        label = "Reason" if result in ("blocked", "skipped") else "Note"
        body.append(f'<p class="qa-note"><strong>{label}:</strong> {rich(check["note"])}</p>')
    if check.get("finding"):
        number = int(check["finding"])
        body.append(f'<p><a href="#f{number}"{NEW_TAB}>See problem {number}</a></p>')
    return (f'<details class="qa-item {e(result)}">{summary}<div class="qa-body">{"".join(body)}</div>'
            "</details>")


def _board(tested: list[dict], findings: list[dict]) -> str:
    """
    The problems and the checks as accordions under a filter by type. The filter is radio buttons the stylesheet
    reads (STYLE): the pages run no script.
    """
    counts = {"finding": len(findings), **{result: sum(check["result"] == result for check in tested)
                                           for result in RESULT_WORDS}}
    radios = ['<input type="radio" class="qa-radio" name="qa-show" id="qa-show-all" checked>']
    labels = [f'<label for="qa-show-all">All <b>{len(findings) + len(tested)}</b></label>']
    for kind, word in FILTERS:
        if counts[kind]:
            radios.append(f'<input type="radio" class="qa-radio" name="qa-show" id="qa-show-{kind}">')
            labels.append(f'<label for="qa-show-{kind}">{word} <b>{counts[kind]}</b></label>')
        else:
            labels.append(f'<span class="qa-off">{word} <b>0</b></span>')
    types = [type_of(finding) for finding in findings]
    type_labels = []
    if findings:
        radios.append('<input type="radio" class="qa-radio" name="qa-type" id="qa-type-all" checked>')
        type_labels.append(f'<label for="qa-type-all">All types <b>{len(findings)}</b></label>')
        for kind, word in TYPE_WORDS.items():
            if types.count(kind):
                radios.append(f'<input type="radio" class="qa-radio" name="qa-type" id="qa-type-{kind}">')
                type_labels.append(f'<label for="qa-type-{kind}">{word} <b>{types.count(kind)}</b></label>')
            else:
                type_labels.append(f'<span class="qa-off">{word} <b>0</b></span>')
    parts = radios + [f'<nav class="qa-filter" aria-label="Show">{"".join(labels)}</nav>',
                      '<section class="qa-findings"><h2>Problems</h2>']
    if findings:
        parts.append(f'<nav class="qa-filter qa-types" aria-label="Problem type">{"".join(type_labels)}</nav>')
    parts += [_finding(number, finding) for number, finding in enumerate(findings, 1)] if findings \
        else ['<p class="empty">None reported.</p>']
    parts.append('</section><section class="qa-checks"><h2>What was checked</h2>')
    if not tested:
        parts.append('<p class="empty">The agent did not say what it checked.</p>')
    groups: dict[str, list[dict]] = {}
    for check in tested:
        groups.setdefault(check.get("group") or "", []).append(check)
    named = any(groups)
    for group in sorted(groups, key=lambda name: name == ""):
        heading = group or ("Other checks" if named else "")
        parts.append('<div class="qa-group">' + (f"<h3>{rich(heading)}</h3>" if heading else "")
                     + "".join(_check(check) for check in groups[group]) + "</div>")
    parts.append("</section>")
    return '<div class="qa-board">' + "\n".join(parts) + "</div>"


def type_of(finding: dict) -> str:
    """
    A finding's type; one from before types, or of a type not known, is TYPE_OTHER.
    """
    return finding.get("type") if finding.get("type") in TYPES else TYPE_OTHER


def _finding(number: int, finding: dict) -> str:
    """
    One problem as an accordion: its severity, type, title and requirement on its line, then its steps with their
    screenshots, expected against actual, and the suspected cause.
    """
    severity = finding.get("severity") or "major"
    kind = type_of(finding)
    tries = ""
    if finding.get("attempts"):
        tries = f"Happened {finding.get('reproduced') or 0} of {finding['attempts']} times"
        if (finding.get("reproduced") or 0) < 2:
            tries += ", may be intermittent"
    url = finding.get("url")
    link = f'<a href="{e(url)}"{NEW_TAB}>{e(url)}</a>' if url else ""
    meta = [text for text in (tries, link) if text]
    parts = [f'<details class="qa-item qa-finding qa-type-{kind}" id="f{number}">',
             f'<summary><span class="tag {e(severity)}">{SEVERITY_WORDS.get(severity, e(severity))}</span>'
             f'<span class="tag kind">{TYPE_WORDS[kind]}</span>'
             f'<span class="qa-title">{rich(finding["title"])}</span>{_spec_ref(finding.get("spec"))}</summary>',
             '<div class="qa-body">']
    if meta:
        parts.append(f'<p class="meta">{"<br>".join(meta)}</p>')
    parts.append(_requirement(finding.get("spec")))
    by_step: dict[int, list[dict]] = {}
    loose = []
    for shot in finding.get("evidence") or []:
        (by_step.setdefault(shot["step"], []) if shot.get("step") else loose).append(shot)
    if finding.get("steps"):
        parts.append('<ol class="steps">')
        for step_number, step in enumerate(finding["steps"], 1):
            failed = ' class="failed"' if finding.get("failed_step") == step_number else ""
            shots = by_step.get(step_number) or []
            parts.append(f'<li{failed}><span class="text">{rich(step)}</span>'
                         + (f'<div class="shots">{"".join(_shot(shot) for shot in shots)}</div>' if shots else "")
                         + "</li>")
        parts.append("</ol>")
    if finding.get("expected") or finding.get("actual"):
        parts.append('<div class="compare">'
                     f'<div><h4>Expected</h4><p>{rich(finding.get("expected")) or "Not said"}</p></div>'
                     f'<div class="actual"><h4>Actual</h4><p>{rich(finding.get("actual")) or "Not said"}</p></div>'
                     "</div>")
    if loose:
        parts.append(f'<div class="shots">{"".join(_shot(shot) for shot in loose)}</div>')
    if finding.get("cause") or finding.get("code"):
        code = []
        for place in finding.get("code") or []:
            where = f"{place['repo']}/{place['file']}" if place.get("repo") else place["file"]
            where += f":{place['line']}" if place.get("line") else ""
            label = f"<code>{e(where)}</code>" + (f" at <code>{e(place['sha'][:12])}</code>" if place.get("sha")
                                                  else "")
            code.append(f'<li><a href="{e(place["url"])}"{NEW_TAB}>{label}</a></li>' if place.get("url")
                        else f"<li>{label}</li>")
        parts.append('<div class="cause"><h4>Suspected cause in the code, not confirmed</h4>'
                     + (f"<p>{rich(finding['cause'])}</p>" if finding.get("cause") else "")
                     + (f"<ul>{''.join(code)}</ul>" if code else "") + "</div>")
    parts.append("</div></details>")
    return "\n".join(parts)


def render_run(view: dict, project: str, tz: ZoneInfo) -> str:
    """One run's report as a page of its own (report.html in the run's folder)."""
    return _page(*run_body(view, project, tz))


def run_body(view: dict, project: str, tz: ZoneInfo) -> tuple[str, str]:
    """One run's report, its title and body: the verdict and the facts, the agent's summary, what it checked, then
    each problem with its steps, its screenshots at the step they show, expected against actual, and the suspected
    cause. The web console shows the body inside its own page."""
    findings = view.get("findings") or []
    outcome = view.get("outcome")
    disputed = bool(view.get("engine_disputed"))
    # Who typed the command on the server is the runner's own user: only the schedule says something.
    by = ", on schedule" if view.get("source") == "schedule" else ""
    facts = [("Scenario", view.get("scenario") or ""),
             ("Started", f"{_when(view.get('started_at') or view.get('created_at'), tz)}{by}"),
             ("Finished", _when(view.get("finished_at"), tz)),
             ("Took", _took(view.get("started_at"), view.get("finished_at")))]
    ins = view.get("instructions") or {}
    if ins:
        facts.append(("Instructions", f"{ins.get('path')}, version {str(ins.get('sha256'))[:12]}"))
    body = [f'<p class="crumb"><a href="../">All runs of {e(project)}</a></p>',
            f"<h1>Platform test, run {e(view['n'])}</h1>",
            f'<p class="verdict {e(outcome)}">{e(head_of(outcome, len(findings), disputed))}</p>',
            '<dl class="facts">' + "".join(f"<dt>{e(k)}</dt><dd>{e(v)}</dd>" for k, v in facts if v) + "</dl>"]
    if view.get("summary"):
        body.append(f'<p class="summary">{rich(view["summary"])}</p>')
    if view.get("blocked"):
        body.append(f'<div class="alert"><p><strong>Could not test:</strong> {rich(view["blocked"])}</p></div>')
    fatal = view.get("fatal") or []
    if fatal:
        body.append('<div class="alert"><p><strong>The run did not finish as it should.</strong> What is below is '
                    'what it reported before that.</p><ul>' + "".join(f"<li>{e(x)}</li>" for x in fatal)
                    + "</ul></div>")
    if view.get("engine_disputed") or view.get("engine_review"):
        objections = (f'<p class="objections">{rich(view["engine_review"])}</p>' if view.get("engine_review")
                      else '<p class="objections">The review did not say what it objected to.</p>')
        body.append('<div class="alert note"><p><strong>The tester\'s report as a whole did not pass its closing '
                    "review.</strong> Everything it reported is below. These objections to the report were not "
                    "settled, so read it with them in mind:</p>" + objections + "</div>")
    doubts = view.get("doubts") or []
    if doubts:
        body.append('<div class="alert note"><p><strong>Not a clean result:</strong></p><ul>'
                    + "".join(f"<li>{e(x)}</li>" for x in doubts) + "</ul></div>")
    body.append(_board(view.get("tested") or [], findings))
    problems = [*(view.get("problems") or []), *(view.get("notes") or [])]
    if problems:
        body.append('<div class="alert note"><p><strong>Left out of this report</strong> (the agent\'s report did not '
                    'hold up there):</p><ul>' + "".join(f"<li>{e(p)}</li>" for p in problems) + "</ul></div>")
    repos = view.get("repos") or []
    if repos:
        lines = []
        for r in repos:
            if not r.get("sha"):
                lines.append(f"<li><code>{e(r['repo'])}</code>: no {e(r['default'])} on the code host</li>")
            else:
                note = "" if r.get("switch") else ", read with git show: other work was checked out"
                lines.append(f"<li><code>{e(r['repo'])}</code> {e(r['default'])} at <code>{e(r['sha'][:12])}</code>"
                             f"{note}</li>")
        body.append("<footer><p>Code the agent read to explain problems:</p><ul>" + "".join(lines) + "</ul></footer>")
    return f"Run {view['n']}: {head_of(outcome, len(findings), disputed)}", "\n".join(body)


def render_index(rows: list[dict], project: str, tz: ZoneInfo) -> str:
    """Every run, newest first."""
    body = [f"<h1>Platform tests of {e(project)}</h1>"]
    if not rows:
        body.append('<p class="empty">No runs yet. Start one on the server with <code>arceus-runner qa run --scenario N</code>.</p>')
        return _page(f"Platform tests of {project}", "\n".join(body))
    trs = []
    for r in sorted(rows, key=lambda r: r["n"], reverse=True):
        if r["state"] != "done":
            result = "Queued" if r["state"] == "pending" else "Testing now" if r["state"] in ("preparing", "running") \
                else "Writing the report"
            result += ", stop asked" if r.get("stop_requested") else ""
        else:
            result = head_of(r["outcome"], r["findings"], bool(r.get("disputed")))
        run = f'<a href="{e(r["n"])}/">Run {e(r["n"])}</a>' if r.get("has_report") else f"Run {e(r['n'])}"
        if r.get("scenario"):
            run += f'<span class="run-scenario">{e(r["scenario"])}</span>'
        summary = f'<span class="run-summary">{rich(r["summary"])}</span>' if r.get("summary") else ""
        by = "Schedule" if r["source"] == "schedule" else "Command"
        cls = e(r["outcome"]) if r["state"] == "done" else ""
        trs.append(f'<tr><td class="nowrap">{run}</td><td><span class="{cls}">{e(result)}</span>{summary}</td>'
                   f"<td class=\"hide-narrow nowrap\">{e(_when(r.get('started_at') or r.get('created_at'), tz))}</td>"
                   f"<td class=\"hide-narrow\">{by}</td></tr>")
    body.append('<table><thead><tr><th>Run</th><th>Result</th><th class="hide-narrow">Started</th>'
                '<th class="hide-narrow">How</th></tr></thead><tbody>' + "".join(trs) + "</tbody></table>")
    return _page(f"Platform tests of {project}", "\n".join(body))


# -- the server -----------------------------------------------------------------------------------------------------

_RUN = re.compile(r"^/(\d{1,9})/$")
_MEDIA = re.compile(r"^/(\d{1,9})/media/([0-9a-f]{16}\.[a-z0-9]{2,5})$")


def resolve(root: Path, path: str) -> tuple[Path, str] | None:
    """The file a request path means and its type, or None: only the list, a run's report and its media."""
    path = path.split("?", 1)[0].split("#", 1)[0]
    if path == "/":
        return root / "index.html", "text/html; charset=utf-8"
    if m := _RUN.match(path):
        return root / m.group(1) / "report.html", "text/html; charset=utf-8"
    if m := _MEDIA.match(path):
        ext = Path(m.group(2)).suffix
        if ext not in media.TYPES:
            return None
        return root / m.group(1) / "media" / m.group(2), media.TYPES[ext][0]
    if _RUN.match(path + "/"):
        return None  # /12 without the slash: the handler sends it to /12/
    return None


class _Handler(BaseHTTPRequestHandler):
    root: Path
    index: Callable[[], str] | None = None
    server_version = "arceus-qa"
    sys_version = ""
    csp = ("default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; "
           "form-action 'none'")

    def do_GET(self) -> None:  # noqa: N802 (the stdlib's name)
        bare = self.path.split("?", 1)[0]
        if re.match(r"^/\d{1,9}$", bare):
            self.send_response(301)
            self.send_header("Location", bare + "/")
            self.end_headers()
            return
        if bare == "/" and self.index is not None:
            self._send(self.index().encode(), "text/html; charset=utf-8")
            return
        found = resolve(self.root, self.path)
        if found is None or found[0].is_symlink() or not found[0].is_file():
            self.send_error(404, "Not found")
            return
        self._send(found[0].read_bytes(), found[1])

    def _send(self, data: bytes, kind: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store" if kind.startswith("text/html") else "max-age=86400")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", self.csp)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return  # who looked at a report is not the runner's news


def serve(root: Path, address: str, port: int, index: Callable[[], str] | None = None):
    """Start the reports server in a thread of its own. Returns the port it listens on and a function that stops it.
    `index` builds the list for `/` on each visit; without it, `/` is the index.html beside the runs."""
    root.mkdir(parents=True, exist_ok=True)
    handler = type("Handler", (_Handler,), {"root": root.resolve(), "index": staticmethod(index) if index else None})
    server = ThreadingHTTPServer((address, port), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="qa-reports", daemon=True)
    thread.start()

    def stop() -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return server.server_address[1], stop
