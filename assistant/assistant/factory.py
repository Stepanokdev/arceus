"""Who the factory takes work from, read from the runner's own config so the assistant and the runner never disagree.

The runner takes a ticket only when its queue tag was put on by someone in `tracker.allowed_users` (by the ticket's
history), in its queued state, and never its own bot's tag. The assistant does not queue tickets: the manager moves
the ticket to To Do and tags it. The assistant reads the same facts to tell the manager what is still missing and,
after that, whether the factory has taken it.

`guide()` is the same config in words, for her prompt: how people start work, answer the agent, review its merge
requests and pick a failed task up again, so what she tells them is what this runner actually does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class FactoryPolicy:
    """What the runner accepts, read from its own config so the two never disagree."""
    allowed_users: tuple[str, ...] = ()
    queue_tag: str = "agent"
    queued_state: str = "To Do"
    bot_user: str = ""
    source: str = ""
    raw: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None, fallback_users: list[str] | None = None,
             fallback_state: str = "To Do") -> "FactoryPolicy":
        if path is not None:
            try:
                raw = yaml.safe_load(path.read_text()) or {}
            except (OSError, yaml.YAMLError):
                raw = None
            if raw is not None:
                t = raw.get("tracker") or {}
                return cls(allowed_users=tuple(str(u) for u in t.get("allowed_users") or ()),
                           queue_tag=str((t.get("tags") or {}).get("queue") or "agent"),
                           queued_state=str((t.get("states") or {}).get("queued") or "To Do"),
                           bot_user=str(t.get("bot_user") or ""), source=str(path), raw=raw)
        return cls(allowed_users=tuple(fallback_users or ()), queued_state=fallback_state, source="config")

    def accepts(self, login: str | None) -> bool:
        return bool(login) and login in self.allowed_users and login != self.bot_user

    def guide(self) -> str:
        """How this factory works, in her words for people, from the runner's config. "" without one."""
        if not self.raw:
            return ""
        t = self.raw.get("tracker") or {}
        tags, states = t.get("tags") or {}, t.get("states") or {}
        host = self.raw.get("code_host") or {}
        sched = self.raw.get("schedule") or {}
        tag = lambda k, d: str(tags.get(k) or d)            # noqa: E731
        state = lambda k, d: str(states.get(k) or d)        # noqa: E731
        queue, needs, failed, research = tag("queue", "agent"), tag("needs_input", "agent-needs-input"), \
            tag("failed", "agent-failed"), tag("research", "agent-research")
        progress, review = state("in_progress", "In Progress"), state("code_review", "Code Review")
        merged = states.get("merged", "Testing") if "merged" in states else "Testing"
        who = ", ".join(self.allowed_users) or "nobody (the list is empty)"
        gl_bot = str(host.get("bot_user") or "")
        group = f"{str(host.get('url') or '').rstrip('/')}/{host.get('group')}" if host.get("group") else ""
        lines = [
            "The factory is AI development agents: they take the tickets of project " + str(t.get("project") or "") +
            " from YouTrack, write code in the repositories of " + (f"group {group}" if group else "the project") +
            " and open a merge request (MR). Below is how it really works (from its settings). When a person asks how "
            "to do something with the factory, explain it with exactly these steps, with the names of the states and "
            "tags: technical words are fine here.",
            f"- Start a ticket: state \"{self.queued_state}\" and tag \"{queue}\". The tag must be put on by someone "
            f"the factory trusts (YouTrack logins): {who}. The factory does not take a tag from anyone else and "
            "writes a comment about it. "
            + (f"The bot account {self.bot_user}" if self.bot_user else "The factory's bot account")
            + " cannot start a ticket, and you do not put tickets into work.",
            f"- Research without code: tag \"{research}\" next to \"{queue}\". The agent writes the answer as a "
            f"comment in the ticket, there will be no MR; to continue, a comment \"/continue\" with a new question.",
        ]
        plats = t.get("platforms") or {}
        if plats:
            pairs = "; ".join(f"tag \"{k}\" — {', '.join(v)}" for k, v in plats.items())
            lines.append(f"- Platforms: {pairs}. A ticket with such a tag does not go to review until that repository "
                         "has changes; the tag is put on when the change is really needed on that platform.")
        lines += [
            f"- Once taken: the ticket is in \"{progress}\", the assignee is the bot, and its first comment has a "
            "\"Progress\" section: what is being done now, the steps, links to the MRs. It updates itself.",
            f"- The agent asks: tag \"{needs}\" and a question as a comment mentioning whoever put the tag on. The "
            "answer is any comment in the ticket from a person on the list above, after the question. Without an "
            "answer it sends a reminder, then the attempt ends as failed.",
            f"- Done: MRs are opened only where the agent changed something; the links are in the ticket, state "
            f"\"{review}\".",
            "- Review in GitLab: a comment in an MR that the agent should act on must be addressed to the factory's bot"
            + (f" — @{gl_bot}" if gl_bot else "") + " — in every thread where changes are expected (in the remark "
            "itself or as a reply in the thread). The agent does not pick up comments without the mention on its own: "
            "people talk to each other there. Publish right away (Add comment now) or finish the review with the "
            "Submit review button: the factory does not see review drafts. Another way is a comment in the ticket from "
            "a person on the list: then the agent gets it together with all unresolved MR threads (you can write "
            "\"see the comments in the MR\"). The agent collects remarks until nobody has written anything for "
            f"{sched.get('review_debounce') or '5 minutes'}, takes them together, replies in each thread and commits "
            "to the same branch.",
            f"- Failed: tag \"{failed}\" and a comment with the reason. To continue, a comment starting with "
            "\"/continue\" from a person on the list (within 14 days), optionally explaining what to do next; branches "
            "and commits are kept. The same if all MRs were closed without merging.",
            f"- Merge: when all of the task's MRs are merged, the ticket goes to \"{merged}\". If a person moved the "
            f"ticket past \"{review}\" by hand, the factory lets it go.",
            "- How fast: during working hours the factory checks YouTrack every "
            f"{sched.get('poll_working') or '2 min'}, outside them every {sched.get('poll_off_hours') or '10 min'}; "
            "it works on one task at a time, fixes to open MRs go before new tickets.",
        ]
        return "\n".join(lines)
