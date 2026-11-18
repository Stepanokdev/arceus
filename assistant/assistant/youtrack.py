"""YouTrack, as the assistant uses it: read the project's tickets, and create one when a person presses the button.

The token (YOUTRACK_TOKEN in assistant.env, assistant/secrets.py) is read by whichever process talks to YouTrack (the
web server, the tools server) and never goes into a prompt, a command line or a reply. Claude's session cannot read
that file (session-settings.json denies the data folder).
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from assistant import secrets

ISSUE_FIELDS = ("idReadable,summary,resolved,updated,created,reporter(login,fullName),tags(name),"
                "customFields(name,value(name,login,fullName))")


class YouTrackError(Exception):
    """Something a person should read: what went wrong, in plain words."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class YouTrackConfig:
    url: str = ""
    project: str = ""
    secrets: Path | None = None
    queue_tag: str = ""
    board: str = ""
    state_field: str = "State"
    queued_state: str = "To Do"
    type_field: str = "Type"
    types: dict[str, str] = field(default_factory=lambda: {"bug": "Bug", "feature": "Feature", "research": "Task"})
    timeout_s: float = 20.0
    recheck_s: tuple[float, ...] = (2.0, 4.0, 8.0)

    @property
    def configured(self) -> bool:
        return bool(self.url and self.project and self.secrets)

    def token(self) -> str | None:
        if not self.secrets:
            return None
        try:
            return secrets.get(self.secrets, "YOUTRACK_TOKEN")
        except (OSError, secrets.SecretsError):
            return None

    @property
    def ready(self) -> bool:
        return self.configured and self.token() is not None

    def issue_url(self, key: str) -> str:
        return f"{self.url}/issue/{key}"


FOOTER_RE = re.compile(r"\n*^-{3,}[ \t]*\n(?:Created|Updated) by the factory assistant[^\n]*\n?", re.M)
MARK_RE = re.compile(r"(?:Draft|Edit) label: (fa-[A-Za-z0-9-]+)")


def strip_footer(description: str) -> str:
    """The ticket's text without the assistant's own footer (who asked, the draft's marks)."""
    return re.sub(r"\n{3,}", "\n\n", FOOTER_RE.sub("\n\n", description or "")).strip()


def marks(description: str) -> list[str]:
    """Every mark the assistant left in a ticket: a retry finds the ticket by them, so an edit keeps them all."""
    return list(dict.fromkeys(MARK_RE.findall(description or "")))


def why(resp: httpx.Response) -> str:
    if resp.status_code in (401, 403):
        return "YouTrack refuses the bot: the token is invalid or the bot account has no rights to this project."
    if resp.status_code == 404:
        return "YouTrack could not find the project or the ticket."
    try:
        detail = resp.json().get("error_description") or resp.json().get("error") or ""
    except ValueError:
        detail = ""
    return f"YouTrack answered with error {resp.status_code}{': ' + detail[:200] if detail else ''}."


def summarize(issue: dict, cfg: YouTrackConfig) -> dict:
    """An issue as the assistant reads it: key, link, state, who has it, the factory's tags."""
    fields = {}
    for f in issue.get("customFields") or []:
        v = f.get("value")
        if isinstance(v, dict):
            v = v.get("name") or v.get("fullName") or v.get("login")
        elif isinstance(v, list):
            v = ", ".join(x.get("name") or x.get("login") or "" for x in v if isinstance(x, dict))
        if v:
            fields[f.get("name")] = v
    out = {"key": issue.get("idReadable"), "url": cfg.issue_url(issue.get("idReadable", "")),
           "summary": issue.get("summary"), "state": fields.get("State"), "assignee": fields.get("Assignee"),
           "type": fields.get(cfg.type_field) if cfg.type_field else None,
           "tags": [t.get("name") for t in issue.get("tags") or []], "resolved": bool(issue.get("resolved"))}
    if issue.get("reporter"):
        out["reporter"] = issue["reporter"].get("login")
    return out


class Client:
    def __init__(self, cfg: YouTrackConfig, transport: httpx.BaseTransport | None = None):
        token = cfg.token()
        if not cfg.configured or token is None:
            raise YouTrackError("YouTrack is not connected yet: the assistant has no bot token.")
        self.cfg = cfg
        self.http = httpx.Client(base_url=f"{cfg.url}/api", timeout=cfg.timeout_s, transport=transport,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})

    def close(self) -> None:
        self.http.close()

    def _call(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            resp = self.http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise YouTrackError(f"YouTrack is unavailable ({type(e).__name__}). Try again in a minute.") from e
        if resp.status_code >= 400:
            raise YouTrackError(why(resp), resp.status_code)
        return resp

    def search(self, query: str, limit: int = 15) -> list[dict]:
        q = f"project: {{{self.cfg.project}}} {query}".strip()
        resp = self._call("GET", "/issues", params={"query": q, "fields": ISSUE_FIELDS, "$top": max(1, min(limit, 50))})
        return [summarize(i, self.cfg) for i in resp.json()]

    def issue(self, key: str, comments: int = 10) -> dict:
        fields = ISSUE_FIELDS + ",description,comments(text,created,author(login,fullName))"
        data = self._call("GET", f"/issues/{key}", params={"fields": fields}).json()
        if not str(data.get("idReadable", "")).startswith(f"{self.cfg.project}-"):
            raise YouTrackError(f"Ticket {key} is not from project {self.cfg.project}.")
        out = summarize(data, self.cfg)
        out["description"] = (data.get("description") or "")[:8000]
        out["comments"] = [{"author": (c.get("author") or {}).get("login"), "created": c.get("created"),
                            "text": (c.get("text") or "")[:2000]} for c in (data.get("comments") or [])[-comments:]]
        return out

    def current(self, key: str) -> dict:
        """A ticket's title and whole description, exactly as stored: what an edit starts from and is checked against."""
        if not re.fullmatch(rf"{re.escape(self.cfg.project)}-\d+", key.strip().upper()):
            raise YouTrackError(f"Ticket {key} is not from project {self.cfg.project}.")
        data = self._call("GET", f"/issues/{key.strip().upper()}", params={"fields": "id,idReadable,summary,description"}).json()
        if not str(data.get("idReadable", "")).startswith(f"{self.cfg.project}-"):
            raise YouTrackError(f"Ticket {key} is not from project {self.cfg.project}.")
        return {"id": data["id"], "key": data["idReadable"], "summary": data.get("summary") or "",
                "description": data.get("description") or ""}

    def update(self, key: str, summary: str, description: str) -> None:
        """Replace one of this project's tickets' title and description (YouTrack keeps the earlier text in history)."""
        cur = self.current(key)
        self._call("POST", f"/issues/{cur['id']}", params={"fields": "id"},
                   json={"summary": summary, "description": description})

    def _project_id(self) -> str:
        resp = self._call("GET", "/admin/projects", params={"fields": "id,shortName", "query": self.cfg.project,
                                                            "$top": 50})
        for p in resp.json():
            if p.get("shortName") == self.cfg.project:
                return p["id"]
        raise YouTrackError(f"The bot account cannot see project {self.cfg.project} in YouTrack.")

    def find_marked(self, marker: str) -> dict | None:
        """The ticket this bot already made from a draft, found by the draft's mark in its description."""
        resp = self._call("GET", "/issues", params={"query": f'project: {{{self.cfg.project}}} "{marker}"',
                                                     "fields": "id,idReadable,description", "$top": 5})
        whole = re.compile(rf"(?<![\w-]){re.escape(marker)}(?![\w-])")
        for i in resp.json():
            if whole.search(i.get("description") or ""):
                return {"id": i["id"], "key": i["idReadable"], "url": self.cfg.issue_url(i["idReadable"]),
                        "tagged": None}
        return None

    def create(self, summary: str, description: str, kind: str, marker: str | None = None) -> dict:
        """Make the ticket. With a marker (the draft's own, already in the description) it is made at most once:
        a ticket that exists from an earlier attempt is returned instead, and when YouTrack's answer to the create
        is lost, the ticket is looked for before anyone is told to try again."""
        if marker and (found := self.find_marked(marker)):
            return self._placed(found)
        body: dict = {"project": {"id": self._project_id()}, "summary": summary, "description": description}
        type_name = self.cfg.types.get(kind) if self.cfg.type_field else None
        if type_name:
            body["customFields"] = [{"name": self.cfg.type_field, "$type": "SingleEnumIssueCustomField",
                                     "value": {"name": type_name}}]
        try:
            data = self._call("POST", "/issues", params={"fields": "id,idReadable"}, json=body).json()
        except YouTrackError as e:
            if not marker or (e.status is not None and e.status < 500):
                raise
            for delay in self.cfg.recheck_s:
                time.sleep(delay)
                try:
                    if found := self.find_marked(marker):
                        return self._placed(found)
                except YouTrackError:
                    pass
            raise YouTrackError("YouTrack did not answer, and it is not yet clear whether the ticket was created. "
                                "Press \"Create ticket\" again in a minute: the assistant first looks for a ticket "
                                "made from this draft and will not create a second one.", e.status) from e
        key = data["idReadable"]
        tagged = None
        if self.cfg.queue_tag:
            tagged = self._tag(data["id"], self.cfg.queue_tag)
        return self._placed({"id": data["id"], "key": key, "url": self.cfg.issue_url(key), "tagged": tagged})

    def _placed(self, made: dict) -> dict:
        """Put the ticket on the project's board. Also for a ticket found again after a lost answer: that attempt may
        have stopped before the board. The ticket exists either way; a board that fails is reported, not fatal."""
        made["board"] = self._to_board(made["id"]) if self.cfg.board else None
        return made

    def _to_board(self, issue_id: str) -> str:
        name = self.cfg.board
        try:
            board = self._board(name)
            if board is None:
                return f"board \"{name}\" not found"
            name = board.get("name") or name
            sprint = (board.get("currentSprint") or {}).get("id")
            if not sprint:
                return f"board \"{name}\" has no current sprint"
            self._call("POST", f"/agiles/{board['id']}/sprints/{sprint}/issues", params={"fields": "id"},
                       json={"id": issue_id, "$type": "Issue"})
            return f"on board \"{name}\""
        except YouTrackError as e:
            return f"not added to board \"{name}\": {e}"

    def _board(self, wanted: str) -> dict | None:
        """The board by its id (104-17) or its exact name. A bot may see hundreds of boards: page through them."""
        fields = "id,name,currentSprint(id)"
        if re.fullmatch(r"\d+-\d+", wanted):
            return self._call("GET", f"/agiles/{wanted}", params={"fields": fields}).json()
        page, skip = 200, 0
        while True:
            boards = self._call("GET", "/agiles", params={"fields": fields, "$top": page, "$skip": skip}).json()
            if found := next((b for b in boards if b.get("name") == wanted), None):
                return found
            if len(boards) < page:
                return None
            skip += page

    def ticket(self, key: str) -> dict:
        """What the factory looks at: internal id, state, assignee and tags."""
        if not key.upper().startswith(f"{self.cfg.project.upper()}-"):
            raise YouTrackError(f"Ticket {key} is not from project {self.cfg.project}.")
        i = self._call("GET", f"/issues/{key}", params={
            "fields": "id,idReadable,tags(id,name),customFields(name,value(name,login))"}).json()
        fields = {f.get("name"): f.get("value") for f in i.get("customFields") or []}
        state = (fields.get(self.cfg.state_field) or {}).get("name") or ""
        assignee = (fields.get("Assignee") or {}).get("login") or ""
        return {"id": i["id"], "key": i["idReadable"], "state": state, "assignee": assignee,
                "tags": {t["name"]: t["id"] for t in i.get("tags") or []}}

    def tag_setter(self, issue_id: str, tag: str) -> tuple[str, int] | None:
        """Who put the tag on most recently and when (ms), by the ticket's history: exactly what the runner reads."""
        last, cursor = None, None
        while True:
            params = {"categories": "TagsCategory", "$top": 100,
                      "fields": "afterCursor,hasAfter,activities(timestamp,author(login),added(name),removed(name))"}
            if cursor:
                params["cursor"] = cursor
            page = self._call("GET", f"/issues/{issue_id}/activitiesPage", params=params).json()
            for a in page.get("activities") or []:
                if tag in {t.get("name") for t in a.get("removed") or []}:
                    last = None
                if tag in {t.get("name") for t in a.get("added") or []}:
                    last = ((a.get("author") or {}).get("login") or "", int(a.get("timestamp") or 0))
            if not page.get("hasAfter") or not page.get("afterCursor"):
                return last
            cursor = page["afterCursor"]

    def notes_from(self, issue_id: str, author: str, since_ms: int) -> list[str]:
        """Comments by one author (the factory's bot) written at or after a moment, oldest first."""
        out, skip = [], 0
        while True:
            page = self._call("GET", f"/issues/{issue_id}/comments", params={
                "fields": "text,created,author(login)", "$top": 100, "$skip": skip}).json()
            out += [c.get("text") or "" for c in page
                    if (c.get("author") or {}).get("login") == author and int(c.get("created") or 0) >= since_ms]
            if len(page) < 100:
                return out
            skip += 100

    def _tag(self, issue_id: str, name: str) -> str | None:
        """Put the factory's tag on. The ticket exists either way: a tag that fails is reported, not fatal."""
        try:
            tags = self._call("GET", "/tags", params={"fields": "id,name", "query": name, "$top": 20}).json()
            tag = next((t for t in tags if t.get("name") == name), None)
            if tag is None:
                return f"tag \"{name}\" not found"
            self._call("POST", f"/issues/{issue_id}/tags", params={"fields": "id"}, json={"id": tag["id"]})
            return name
        except YouTrackError as e:
            return f"tag \"{name}\" not set: {e}"
