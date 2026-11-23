"""What the runner writes for the agent and on tickets and merge requests, as text: the workspace's AGENTS.md and
the project's notes, a research ticket's brief, an answer cut into comments, the screenshots section of an MR.
Nothing here keeps state or calls a service; delivery.py and the agents put these texts where they go."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from pathlib import Path

from arceus_runner import materials, media
from arceus_runner.integrations import Comment, IssueDetails, IssueTracker, Repo
from arceus_runner.replies import unattributed

# Why the engine refused to start a task, by its exit code (engine/SERVER.md).
START_REFUSED = {
    76: "the repository folder is not a git repository",
    77: "the working tree has uncommitted changes from an earlier attempt",
    78: "no decision was recorded for the repository's MCP servers",
    79: "the repository has files too large for a checkpoint",
}
HOME_IGNORE = """\
# Every folder here is a repository of the project's group on its code host, cloned by the Arceus runner.
# This repository holds only the runner's notes for the agent and is never published.
/*
!/.gitignore
!/AGENTS.md
!/CLAUDE.md
"""


PROJECT_NOTES_LIMIT = 16 * 1024  # bytes: every session of every task reads them whole
PROJECT_NOTES_HEAD = ("These are the project's notes. Where they contradict the server's "
                      "notes or the rules above, those win.")


class ProjectNotesError(Exception):
    """paths.project_notes is set, but the agent cannot be given what it holds."""


def project_notes(path: Path | None) -> str:
    """The project's notes as the agent gets them: HTML comments (the template's guidance) left out.
    Empty when none are set. A file that is set but missing, unreadable or too long is an error, never skipped
    or cut short: the agent would work without rules the project counts on."""
    if path is None:
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ProjectNotesError(f"paths.project_notes: {path} does not exist") from None
    except (OSError, UnicodeDecodeError) as e:
        raise ProjectNotesError(f"paths.project_notes: {path} cannot be read ({e})") from None
    return project_notes_text(text, path)


def project_notes_text(text: str, path: Path) -> str:
    """project_notes for a text already in hand, such as a draft the console checks before saving."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()
    size = len(text.encode())
    if size > PROJECT_NOTES_LIMIT:
        raise ProjectNotesError(f"paths.project_notes: {path} is {size} bytes without comments, more than "
                                f"{PROJECT_NOTES_LIMIT}; shorten it")
    return text


def home_files(where: str, repos: list[Repo], home: Path, notes: str = "", reading: bool = False) -> dict[str, str]:
    """What the agent finds where its session starts: what the folder is, its path, the repositories, and
    the project's notes when there are any. `where` names the scope: GitLab group `clients/acme`. `reading`: a folder
    of a test run's own (workspace_run.py), whose checkouts are there to be read, never changed."""
    if reading:
        lines = [f"# {where}", "",
                 "This folder is not a project. Each folder in it is a checkout of a repository of the group at",
                 "its default branch, made by the Arceus runner for a test run.", "",
                 f"Its absolute path: `{home}`", "",
                 "- Read the code here to explain what you find. Never change it: edit no file, commit nothing,",
                 "  push nothing, here or in any repository.",
                 "- The runner removes these checkouts when the run is over; write only where your brief says.",
                 "", "## Repositories", ""]
    else:
        lines = [f"# {where}", "",
                 "This folder is not a project. Each folder in it is a separate git repository of the group,",
                 "cloned by the Arceus runner and switched to the task's branch.", "",
                 f"Its absolute path (the workspace): `{home}`", "",
                 "- Work out from the ticket which repositories it needs, and change only those.",
                 "- Commit inside each repository you change, on the branch checked out there. Commit nothing",
                 "  in this folder: it belongs to the runner and is never published.",
                 "- Do not push. The runner pushes the commits Codex accepted and opens one merge request per",
                 "  changed repository.", "", "## Repositories", ""]
    for r in sorted(repos, key=lambda r: r.relative_path):
        lines.append(f"- `{r.relative_path}`" + (f": {' '.join(r.description.split())}" if r.description else ""))
    if notes:
        lines += ["", "## Project notes", "", PROJECT_NOTES_HEAD, "", notes]
    return {".gitignore": HOME_IGNORE, "AGENTS.md": "\n".join(lines) + "\n", "CLAUDE.md": "@AGENTS.md\n"}


def slugify(text: str, limit: int = 40) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")[:limit].strip("-")
    return slug or "task"


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


QUESTION_KEEP = 100_000  # characters of a question kept with the task: whole, as posted, for the console's task page


def short_text(text: str, limit: int = 1500) -> str:
    text = unattributed(text).strip()
    return text if len(text) <= limit else text[:limit] + "…"


ANSWER_PART = 12_000  # characters per ticket comment: a long answer is posted whole, as several, in order
ANSWER_BYTES = 512 * 1024  # an answer file larger than this is not read: the agent is asked to shorten it
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


def research_brief(answer: Path) -> str:
    """What the agent is told, after the ticket's own text, when the ticket asks a question instead of a change."""
    return "\n".join([
        "## How this ticket is handled", "",
        "This is a research ticket: it asks for an answer, not a code change. No merge request "
        "is opened for it, and nothing you commit is published.", "",
        "- Read the code, its history and whatever else you need, and run what helps you answer: tests, a script, "
        "the app. Leave every repository as you found it: commit nothing, and undo whatever you changed to check "
        "something.",
        f"- Write the full answer in Markdown to `{answer}`. Start with the answer itself, then the evidence for it "
        "(files and lines, commands and what they showed). Keep what you verified apart from what you assume or "
        "recommend. The runner posts the file on the ticket as it is, so write it for whoever reads the ticket, in "
        "English.",
        "- Then declare the outcome succeeded_research, with the answer in a few sentences as the summary.",
        "- When the answer depends on a decision or access only a person has, ask, as on any ticket.",
    ])


def split_answer(text: str, limit: int = ANSWER_PART) -> list[str]:
    """The answer in pieces of at most `limit` characters, cut between lines. A code block that a cut falls in is
    closed at the end of one piece and opened again at the start of the next, so each piece renders on its own."""
    room = limit - 16  # for the closing fence
    lines = [ln[i:i + room] for ln in text.splitlines() for i in range(0, max(len(ln), 1), room)]
    parts: list[str] = []
    cur: list[str] = []
    fence: str | None = None  # the opening line of the code block the current piece is inside
    for line in lines:
        if cur and sum(len(x) + 1 for x in cur) + len(line) > room:
            parts.append(_close_piece(cur, fence))
            cur = [fence] if fence else []
        cur.append(line)
        if _FENCE.match(line):
            fence = None if fence else line.strip()
    parts.append(_close_piece(cur, fence))
    return [p for p in parts if p.strip()]


def _close_piece(lines: list[str], fence: str | None) -> str:
    closing = [_FENCE.match(fence).group(1)] if fence else []
    return "\n".join([*lines, *closing]).strip()


def ticket_pieces(head: str, text: str, foot: str = "", tail: str = "") -> list[str]:
    """A ticket comment with the agent's words in it, never cut: one comment while they fit, else several in
    order, each marked "Part i of n". `head` opens the first; `tail` (the screenshots) and `foot` (what the reader
    does next) close the last, so the request to act comes after everything it refers to."""
    body = split_answer(unattributed(text).strip()) if (text or "").strip() else []
    n = max(len(body), 1)
    pieces = []
    for i in range(1, n + 1):
        parts = [head] if i == 1 and head else []
        if n > 1:
            parts.append(f"**Part {i} of {n}**")
        if body:
            parts.append(body[i - 1])
        if i == n:
            parts += [x for x in (tail, foot) if x]
        pieces.append("\n\n".join(parts))
    return pieces


def piece_kind(kind: str, i: int) -> str:
    """The journal kind of piece `i`: the first keeps the comment's own kind, so a comment posted as one piece
    before is recognised as the same comment. The others start with `part<i>:`, never with the comment's kind:
    failures and closings are numbered by counting the kinds that start with theirs."""
    return kind if i == 1 else f"part{i}:{kind}"


MEDIA_BEGIN, MEDIA_END = "<!-- arceus:media -->", "<!-- /arceus:media -->"


def media_section(shots: list[str]) -> str:
    """The MR's screenshots, between markers so a later round can replace them in place."""
    return "\n\n".join([MEDIA_BEGIN + "\n## Screenshots and recordings", *shots, MEDIA_END]) if shots else ""


def with_media_section(description: str, shots: list[str]) -> str:
    section = media_section(shots)
    start, end = description.find(MEDIA_BEGIN), description.find(MEDIA_END)
    if start != -1 and end > start:
        return description[:start] + section + description[end + len(MEDIA_END):]
    if not section:
        return description
    marker = "\n## Checks"
    at = description.find(marker)
    return (description[:at] + "\n\n" + section + description[at:]) if at != -1 else \
        description.rstrip() + "\n\n" + section


def ticket_media_text(tracker: IssueTracker, items: list[media.MediaItem]) -> str:
    """The comment's own attachments, shown in place the way the tracker does it: a screenshot as a preview, one of
    unknown size as a link to the file."""
    def shown(m: media.MediaItem) -> str:
        if m.shown_as == "link":
            return tracker.attachment_link(m.upload_name, m.caption)
        return tracker.attachment_markdown(m.upload_name, m.caption, m.preview_width)
    return "\n\n".join(["Screenshots and recordings of the change:",
                         *(f"**{m.caption}**\n\n{shown(m)}" for m in items)])


def mr_media_markdown(m: media.MediaItem, url: str) -> str:
    """An uploaded file in the MR description: a screenshot as a preview that GitLab opens full size on a click,
    a recording as its player, a screenshot of unknown size as a link to the file."""
    if m.shown_as == "link":
        return f"**{m.caption}**\n\n[Open the screenshot: {m.caption}]({url})"
    width = m.preview_width
    return f"**{m.caption}**\n\n![{m.caption}]({url})" + (f"{{width={width}}}" if width else "")


_CAUSE = re.compile(r"not found|no such file|not on path|\berror\b|\bfailed\b|denied|timed out", re.I)


def log_hint(log: Path) -> str:
    """The line of a check's log that names the cause: the first error-looking line that is not make's own
    `*** Error` echo, else the last line."""
    try:
        lines = [ln.strip() for ln in log.read_text(errors="replace").splitlines() if ln.strip()]
    except OSError:
        return ""
    hint = next((ln for ln in lines if _CAUSE.search(ln) and not ln.startswith("make: ***")),
                lines[-1] if lines else "")
    return short_text(hint, 200)


# The review gate's entry when its reviewers stopped the work for a person (engine/hooks/review-gate.sh): appended to
# the run's `reports/blocked.md` under `## <time> — session <id> — BLOCKED`. Its other entries there (a reviewer it
# could not reach, a run under review too long, rounds without progress) have other headings and stay failures.
_REVIEW_BLOCKED = re.compile(r"^## .* — BLOCKED\s*$")
_GATE_LINE = re.compile(r"^(STATE|VERDICT|REVIEW TEAM):")
_TEAM_MEMBER = re.compile(r"^— (.+?): ([A-Z][A-Z_-]*) —$")


def review_stop(note: str) -> str:
    """What the reviewers asked a person, from a run's `blocked.md`: the last entry, when it is the review's own
    BLOCKED, else nothing. With a review team, only the members that stopped the work, each under its name."""
    last = re.split(r"(?m)^(?=## )", note.strip())[-1]
    head, _, body = last.partition("\n")
    if not _REVIEW_BLOCKED.match(head):
        return ""
    loose: list[str] = []
    members: list[tuple[str, str, list[str]]] = []
    for line in body.splitlines():
        if _GATE_LINE.match(line.strip()):
            continue
        m = _TEAM_MEMBER.match(line.strip())
        if m:
            members.append((m.group(1), m.group(2), []))
        else:
            (members[-1][2] if members else loose).append(line)
    parts = [f"**{name}:** {unattributed(chr(10).join(said)).strip()}" for name, verdict, said in members
             if verdict == "BLOCKED" and "".join(said).strip()]
    if not parts:
        parts = [unattributed("\n".join(loose) or "\n".join(ln for _, _, said in members for ln in said)).strip()]
    return "\n\n".join(p for p in parts if p).strip()


_FINDING = re.compile(r"^\s*(?:\d+[.)]|[A-Z]\d+:)\s+(.*\S)\s*$")
_FINDING_NOISE = re.compile(r"\s*\[(?:repeat|acceptance_failure|regression_from_patch)\]\s*")
# What the gate never blocks on (hooks/review-gate.sh): the tool's own failures and remarks outside the task.
_FINDING_ASIDE = re.compile(r"\[(?:harness_failure|related_improvement|pre_existing)\]")


def review_findings(review: str, limit: int = 8) -> list[str]:
    """The distinct findings in a Codex review as the gate keeps it: its numbered list and the ids still open on the
    fix check's ledger (`T1: …`). A review team names one defect once per member and the ledger repeats them, so each
    text counts once; the gate's own tags (`[repeat]`, the blocking classes) are taken off. What does not block (the
    review tool's own failures, remarks outside the task) is left out, so it never takes a blocking finding's place."""
    out: list[str] = []
    for line in review.splitlines():
        m = _FINDING.match(line)
        if not m or m.group(1).split()[0] in ("FIXED", "OPEN", "SAME") or _FINDING_ASIDE.search(m.group(1)):
            continue
        text = unattributed(_FINDING_NOISE.sub(" ", m.group(1))).strip()
        if text and text not in out:
            out.append(text)
    return out[:limit]


def _unpublished(mrs: list[str]) -> str:
    """What is and is not published while the task waits: nothing yet, or, after its MRs were opened, only this
    round's changes, the MRs staying as they were."""
    if not mrs:
        return "Nothing is published yet; the branch and its commits are kept on the server."
    return ("The changes of this round are not published yet; they are kept on the server. The merge requests stay "
            "as they were: " + ", ".join(mrs) + ".")


def review_question(asked: str, report: str, mrs: list[str] = ()) -> str:
    """The question a review that stopped for a person's decision puts on the ticket: what it asks, then the work.
    `mrs` are the task's merge requests already open, when the stop came in a later round."""
    where = "before its changes were published" if mrs else "before a merge request"
    out = [f"The review stopped this work {where}: it needs a person's decision. {_unpublished(list(mrs))}",
           "", "**What the review asks:**", "", asked]
    if report.strip():
        out += ["", "**What the agent did:**", "", report.strip()]
    return "\n".join(out)


def stop_question(needs: list[str], report: str, mrs: list[str] = ()) -> str:
    """The question for a stop the agent declared without one of its own: that it waits for a person, what it named
    as needed (its blocker findings), then its report."""
    where = "before publishing this round's changes" if mrs else "before a merge request"
    out = [f"The agent stopped {where} and waits for a person to answer or act. Arceus publishes only work that "
           f"passed review. {_unpublished(list(mrs))}"]
    if needs:
        out += ["", "**What it needs:**", "", *(f"- {unattributed(n).strip()}" for n in needs)]
    if report.strip():
        out += ["", "**What the agent did:**", "", report.strip()]
    return "\n".join(out)


def no_change_question(report: str) -> str:
    """The question for a ticket the agent answered by changing nothing: its reasons, for a person to accept or not.
    Nothing reviews such a claim (Bulava banks it as unreviewed), so the ticket waits for a person, not a failure."""
    out = ["The agent changed no code: it concluded that nothing needs changing for this ticket. No review checks "
           "a conclusion like that, so it waits for you. Nothing was published."]
    if report.strip():
        out += ["", "**Why, in the agent's words:**", "", unattributed(report).strip()]
    return "\n".join(out)


def no_change_foot() -> str:
    """How to answer a no-change question: the two ways out, so "ok" is never the only thing a person can say."""
    return ("**If you agree,** close the ticket (any state the project counts as resolved): the task ends there, "
            "without a failure, and nothing is published.\n"
            "**If something does need doing,** reply with a comment saying what. The agent continues on the same "
            "branch, and whatever it changes is reviewed before anything is published.")


def comments_section(comments: list[Comment]) -> str:
    """People's comments on the ticket, for the agent, oldest first."""
    lines = ["## Comments on the ticket, oldest first", "",
             "What people wrote on the ticket. Later comments may change or narrow what the description asks.", ""]
    for x in comments:
        body = x.text.strip().replace("\n", "\n  ") or "(no text, only files: see below)"
        lines.append(f"- {x.author or 'someone unknown'}, {materials._when(x.created)}: {body}")
    return "\n".join(lines)


def parent_section(parent: IssueDetails) -> str:
    """The parent ticket's words, for a subtask that has none of its own (Delivery._parent)."""
    return "\n".join([f"## The parent ticket {parent.key}: {parent.summary}", "",
                      "This ticket has no description of its own. It is a subtask of the ticket below, and what it "
                      "asks is there:", "", parent.description.strip() or "(the parent has no description either)"])
