"""The assistant in Slack, against a fake Slack: what she answers, where, for whom, once."""

import asyncio
import json
from pathlib import Path

from starlette.testclient import TestClient

from assistant import server
from assistant.brain import Event
from assistant.slack import SlackBridge, SlackConfig, chunks, slack_error, to_mrkdwn
from tests.test_chat import cfg

TEAM, BOT = "T1", "UBOT"
PEOPLE = {
    "UDANA": {"id": "UDANA", "team_id": TEAM, "profile": {"email": "dana@x.com", "real_name": "Dana"}},
    "USER2": {"id": "USER2", "team_id": TEAM, "profile": {"email": "serhii@x.com", "real_name": "Serhii"}},
    "UOUT": {"id": "UOUT", "team_id": TEAM, "profile": {"email": "stranger@x.com", "real_name": "Somebody"}},
    "UGUEST": {"id": "UGUEST", "team_id": TEAM, "is_restricted": True,
               "profile": {"email": "dana2@x.com", "real_name": "Guest"}},
}


class FakeWeb:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.n = 0
        self.fail_post = 0
        self.fail_attempts: set[int] = set()
        self.attempts = 0
        self.shared = {"C1": False, "CEXT": True}

    def _log(self, name, kw):
        self.calls.append((name, kw))

    def of(self, name):
        return [kw for n, kw in self.calls if n == name]

    async def users_info(self, user):
        self._log("users_info", {"user": user})
        return {"user": PEOPLE[user]}

    async def reactions_add(self, **kw):
        self._log("reactions_add", kw)

    async def reactions_remove(self, **kw):
        self._log("reactions_remove", kw)

    async def chat_postEphemeral(self, **kw):
        self._log("chat_postEphemeral", kw)

    async def conversations_info(self, channel):
        self._log("conversations_info", {"channel": channel})
        if channel not in self.shared:
            raise RuntimeError("missing_scope")
        return {"channel": {"id": channel, "is_ext_shared": self.shared[channel]}}

    async def chat_postMessage(self, **kw):
        self.attempts += 1
        if self.attempts in self.fail_attempts:
            raise RuntimeError("network")
        if self.fail_post:
            self.fail_post -= 1
            raise RuntimeError("network")
        self.n += 1
        self._log("chat_postMessage", kw)
        return {"ts": f"9.{self.n}"}

    async def users_lookupByEmail(self, email):
        self._log("users_lookupByEmail", {"email": email})
        return {"user": next(u for u in PEOPLE.values() if u["profile"]["email"] == email)}

    async def conversations_open(self, users):
        self._log("conversations_open", {"users": users})
        return {"channel": {"id": "D-" + users}}

    async def chat_getPermalink(self, channel, message_ts):
        return {"permalink": f"https://slack.example/{channel}/{message_ts}"}

    async def chat_update(self, **kw):
        self._log("chat_update", kw)

    async def conversations_replies(self, **kw):
        self._log("conversations_replies", kw)
        return {"messages": [{"ts": "5.0", "user": "USER2", "text": "The client says the video doesn't play"},
                             {"ts": "5.1", "user": "UDANA", "text": f"<@{BOT}> take a look"}]}


def settings(tmp: Path) -> server.Settings:
    s = cfg(tmp)
    s.allowed_users = ["dana@x.com", "serhii@x.com", "dana2@x.com"]
    s.ticket_creators = ["dana@x.com", "serhii@x.com"]
    s.expert = "dana@x.com"
    s.slack = SlackConfig(tmp / "assistant.env", web_url="https://f.example/assistant/")
    return s


def make(tmp: Path, monkeypatch, replies=None, gate: asyncio.Event | None = None):
    prompts: list[str] = []

    async def run(_cfg, prompt, session, new_session=None, env=None, read_dirs=(), mcp=None):
        prompts.append(prompt)
        if gate is not None:
            await gate.wait()
        text = (replies or {}).get(len(prompts), f"reply {len(prompts)}")
        yield Event("delta", text)
        yield Event("done", text)

    monkeypatch.setattr(server, "run_turn", run)
    app = server.App(settings(tmp))
    web = FakeWeb()
    bridge = SlackBridge(app, app.s.slack, web=web)
    bridge.team, bridge.bot_user = TEAM, BOT
    app.after_turn.append(bridge._after_turn)
    app.slack = bridge
    return app, bridge, web, prompts


async def settle(app, bridge, rounds=60):
    """Until nothing is being taken or answered, posting what there is."""
    for _ in range(rounds):
        await asyncio.sleep(0.01)
        if bridge.tasks:
            await asyncio.gather(*list(bridge.tasks), return_exceptions=True)
        if not app.turns and not bridge.tasks:
            await asyncio.sleep(0.01)
            if not app.turns and not bridge.tasks:
                break
    await bridge.post_pending()


def dm(text, ts="1.0", user="UDANA", **extra):
    return {"team_id": TEAM, "event": {"type": "message", "channel_type": "im", "channel": "D1", "user": user,
                                       "text": text, "ts": ts, **extra}}


def mention(text, ts="2.0", thread=None, user="UDANA", ext=False):
    ev = {"type": "app_mention", "channel": "C1", "user": user, "text": f"<@{BOT}> {text}", "ts": ts}
    if thread:
        ev["thread_ts"] = thread
    return {"team_id": TEAM, "is_ext_shared_channel": ext, "event": ev}


def test_direct_message_gets_eyes_then_her_answer(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, prompts = make(tmp_path, monkeypatch)
        assert bridge.receive(dm("Hi, what about the video?"))
        await settle(app, bridge)
        assert web.of("reactions_add") == [{"channel": "D1", "timestamp": "1.0", "name": "eyes"}]
        posts = web.of("chat_postMessage")
        assert [p["text"] for p in posts] == ["reply 1"] and posts[0]["thread_ts"] is None
        assert web.of("reactions_remove") == [{"channel": "D1", "timestamp": "1.0", "name": "eyes"}]
        assert "[Dana (dana@x.com)," in prompts[0] and "Hi, what about the video?" in prompts[0]
        assert bridge.receive(dm("And one more thing", ts="1.5"))
        await settle(app, bridge)
        assert len(app.store.slack_links()) == 1 and len(prompts) == 2
        await bridge.stop()
    asyncio.run(go())


def test_a_resent_event_and_a_mention_seen_twice_are_taken_once(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, prompts = make(tmp_path, monkeypatch)
        assert bridge.receive(dm("once"))
        assert not bridge.receive(dm("once"))
        m = mention("what's up?")
        assert bridge.receive(m)
        as_message = {"team_id": TEAM, "event": {**m["event"], "type": "message", "channel_type": "channel"}}
        assert not bridge.receive(as_message)
        await settle(app, bridge)
        assert len(prompts) == 2 and len(web.of("chat_postMessage")) == 2
        await bridge.stop()
    asyncio.run(go())


def test_she_ignores_herself_bots_edits_and_channel_chatter(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, prompts = make(tmp_path, monkeypatch)
        assert not bridge.receive(dm("it's me", user=BOT))
        assert not bridge.receive({"team_id": TEAM, "event": {"type": "message", "channel_type": "im", "channel": "D1",
                                                              "bot_id": "B9", "user": "UX", "text": "bot", "ts": "3"}})
        assert not bridge.receive(dm("edited", subtype="message_changed"))
        assert not bridge.receive({"team_id": TEAM, "event": {"type": "message", "channel_type": "channel",
                                                              "channel": "C1", "user": "UDANA", "text": "lunch?",
                                                              "ts": "4"}})
        await settle(app, bridge)
        assert prompts == [] and web.calls == []
        await bridge.stop()
    asyncio.run(go())


def test_anyone_of_the_team_may_write_but_guests_and_shared_channels_get_a_note(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, prompts = make(tmp_path, monkeypatch)
        bridge.receive(dm("and me?", ts="1.1", user="UGUEST"))
        bridge.receive(mention("look at the ticket", ext=True))
        await settle(app, bridge)
        assert prompts == [] and web.of("chat_postMessage") == []
        notes = web.of("chat_postEphemeral")
        assert len(notes) == 2 and any("shared with other companies" in n["text"] for n in notes)
        assert app.store.conversations() == []
        bridge.receive(dm("show the code", ts="1.2", user="UOUT"))
        await settle(app, bridge)
        assert len(prompts) == 1 and "stranger@x.com" in prompts[0]
        await bridge.stop()
    asyncio.run(go())


def test_a_mention_opens_a_thread_she_keeps_answering_in(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, prompts = make(tmp_path, monkeypatch)
        bridge.receive(mention("what about the video?", ts="5.1", thread="5.0"))
        await settle(app, bridge)
        assert "The client says the video doesn't play" in prompts[0]
        assert web.of("chat_postMessage")[0]["thread_ts"] == "5.0"
        follow = {"team_id": TEAM, "event": {"type": "message", "channel_type": "channel", "channel": "C1",
                                             "user": "USER2", "text": "and on the phone?", "ts": "5.2", "thread_ts": "5.0"}}
        assert bridge.receive(follow)
        await settle(app, bridge)
        assert len(prompts) == 2 and web.of("chat_postMessage")[1]["thread_ts"] == "5.0"
        await bridge.stop()
    asyncio.run(go())


def test_messages_sent_while_she_answers_wait_and_are_answered_together(tmp_path, monkeypatch):
    async def go():
        gate = asyncio.Event()
        app, bridge, web, prompts = make(tmp_path, monkeypatch, gate=gate)
        bridge.receive(dm("first"))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if prompts:
                break
        bridge.receive(dm("second", ts="1.1"))
        bridge.receive(dm("third", ts="1.2"))
        for _ in range(20):
            await asyncio.sleep(0.01)
        assert len(prompts) == 1
        gate.set()
        await settle(app, bridge)
        assert len(prompts) == 2 and "second" in prompts[1] and "third" in prompts[1]
        assert [p["text"] for p in web.of("chat_postMessage")] == ["reply 1", "reply 2"]
        await bridge.stop()
    asyncio.run(go())


def test_a_failed_post_is_posted_on_the_next_pass_once(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, _ = make(tmp_path, monkeypatch)
        web.fail_post = 1
        bridge.receive(dm("hi"))
        await settle(app, bridge)
        assert web.of("chat_postMessage") == []
        await bridge.post_pending()
        await bridge.post_pending()
        assert [p["text"] for p in web.of("chat_postMessage")] == ["reply 1"]
        await bridge.stop()
    asyncio.run(go())


def test_a_restart_finishes_what_was_taken_and_answers_what_was_left(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, prompts = make(tmp_path, monkeypatch)
        item = {"event": dm("did you make it?")["event"], "team": TEAM, "ext": False}
        assert app.store.slack_take(TEAM, "D1", "1.0", item)
        cid = app.store.link_slack(TEAM, "C1", "7.0", None, "dana@x.com", "t")
        app.store.add_user_message(cid, "no reply", "dana@x.com", [])
        app2, bridge2, web2, prompts2 = make(tmp_path, monkeypatch)
        assert app2.store.messages(cid)[-1].role == "user"
        bridge2.resume()
        await settle(app2, bridge2)
        assert len(prompts2) == 2 and len(web2.of("chat_postMessage")) == 2
        await bridge2.stop()
    asyncio.run(go())


def test_a_failed_turn_says_so_in_slack_words_and_the_next_message_asks_again(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, prompts = make(tmp_path, monkeypatch)

        async def broken(_cfg, prompt, *a, **k):
            prompts.append(prompt)
            yield Event("error", "Claude did not answer in 10 min, the reply was interrupted.")
        monkeypatch.setattr(server, "run_turn", broken)
        bridge.receive(dm("a question"))
        await settle(app, bridge)
        text = web.of("chat_postMessage")[0]["text"]
        assert "did not answer" in text and "Write to me again" in text and "Try again" not in text
        async def works(_cfg, prompt, *a, **k):
            prompts.append(prompt)
            yield Event("done", "now it works")
        monkeypatch.setattr(server, "run_turn", works)
        bridge.receive(dm("repeat", ts="1.1"))
        await settle(app, bridge)
        assert "a question" in prompts[-1] and "repeat" in prompts[-1]
        await bridge.stop()
    asyncio.run(go())


def test_without_the_teams_key_slack_hears_it_plainly_and_no_claude_starts(tmp_path, monkeypatch):
    """The real brain, its key missing: what the person in Slack reads, and that no Claude ran on anyone's sign-in."""
    from assistant import brain

    async def go():
        app, bridge, web, _ = make(tmp_path, monkeypatch)
        monkeypatch.setattr(server, "run_turn", brain.run_turn)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-owner")
        marker = tmp_path / "claude-ran"
        fake = tmp_path / "claude"
        fake.write_text(f"#!/bin/sh\ntouch {marker}\n")
        fake.chmod(0o755)
        app.s.brain.claude_bin, app.s.brain.auth = str(fake), "api_key"
        app.s.brain.secrets, app.s.brain.claude_home = tmp_path / "assistant.env", tmp_path / "claude-home"
        bridge.receive(dm("a question"))
        await settle(app, bridge)
        text = web.of("chat_postMessage")[0]["text"]
        assert "API key" in text and "assistant's settings" in text and not marker.exists()
        await bridge.stop()
    asyncio.run(go())


def test_a_draft_card_and_its_button_make_one_ticket(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, _ = make(tmp_path, monkeypatch)
        made = []

        def create(self, d, footer):
            made.append(d.id)
            return {"key": "NWND-9", "url": "https://yt/NWND-9", "tagged": None, "board": None}
        monkeypatch.setattr(server.App, "_create", create)
        monkeypatch.setattr(type(app.s.youtrack), "ready", property(lambda self: True))
        bridge.receive(dm("make a ticket"))
        await settle(app, bridge)
        cid = app.store.slack_links()[0].conversation_id
        msg_id = app.store.messages(cid)[-1].id
        d = app.store.add_draft(cid, "bug", "Video doesn't play", "## Problem\nVideo does **not** play")
        app.store.attach_drafts(cid, msg_id)
        await bridge.post_pending()
        card = web.of("chat_postMessage")[-1]
        buttons = [b for b in card["blocks"] if b["type"] == "actions"][0]["elements"]
        assert buttons[0]["text"]["text"] == "Create ticket" and buttons[0]["value"] == str(d.id)
        assert "https://f.example/assistant/#" + cid in json.dumps(card["blocks"])
        press = {"type": "block_actions", "user": {"id": "UDANA"}, "channel": {"id": "D1"},
                 "actions": [{"action_id": "fa_create", "value": str(d.id)}]}
        await asyncio.gather(bridge._interact(press), bridge._interact(press))
        await bridge.post_pending()
        assert made == [d.id] and app.store.draft(d.id).state == "created"
        update = web.of("chat_update")[-1]
        assert "NWND-9" in json.dumps(update["blocks"]) and "actions" not in json.dumps(update["blocks"])
        d2 = app.store.add_draft(cid, "bug", "Second one", "description")
        app.store.attach_drafts(cid, msg_id)
        await bridge._interact({**press, "user": {"id": "UOUT"}, "actions": [{"action_id": "fa_create",
                                                                              "value": str(d2.id)}]})
        assert app.store.draft(d2.id).state == "draft"
        await bridge.stop()
    asyncio.run(go())


def test_a_slack_dm_is_seen_in_the_web_chat_only_by_its_person(tmp_path):
    s = settings(tmp_path)
    s.allowed_users = ["dana@x.com", "serhii@x.com"]
    web = server.build(s)
    app = web.state.app
    cid = app.store.link_slack(TEAM, "D1", "", "dana@x.com", "dana@x.com", "Slack · Dana")
    m = app.store.add_user_message(cid, "private stuff", "dana@x.com", [])
    app.store.add_draft(cid, "bug", "Private ticket", "description")
    app.store.attach_drafts(cid, m.id)
    did = app.store.drafts_of(cid)[0].id
    thread = app.store.link_slack(TEAM, "C1", "5.0", None, "dana@x.com", "Slack · thread")
    with TestClient(web) as c:
        dana, serhii = {"X-Assistant-User": "dana@x.com"}, {"X-Assistant-User": "serhii@x.com"}
        mine = [x["id"] for x in c.get("/assistant/api/conversations", headers=dana).json()["conversations"]]
        theirs = [x["id"] for x in c.get("/assistant/api/conversations", headers=serhii).json()["conversations"]]
        assert cid in mine and thread in mine and cid not in theirs and thread in theirs
        assert c.get(f"/assistant/api/conversations/{cid}", headers=serhii).status_code == 404
        assert c.get(f"/assistant/api/conversations/{cid}/events", headers=serhii).status_code == 404
        assert c.get(f"/assistant/api/drafts/{did}", headers=serhii).status_code == 404
        assert c.post(f"/assistant/api/drafts/{did}/create", headers=serhii).status_code == 404
        assert c.get(f"/assistant/api/conversations/{cid}", headers=dana).json()["slack"] == "dm"
        r = c.post(f"/assistant/api/conversations/{cid}/messages", headers=dana, json={"text": "from the web"})
        assert r.status_code == 409 and "Slack" in r.json()["error"]


def test_formatting_for_slack():
    assert to_mrkdwn("## Problem\n**Important**: [ticket](https://yt/X-1)\n- one") == \
        "*Problem*\n*Important*: <https://yt/X-1|ticket>\n• one"
    assert to_mrkdwn("a < b & c\n```\n**x** <y>\n```") == "a &lt; b &amp; c\n```\n**x** <y>\n```"
    assert slack_error("The reply was interrupted. Press \"Try again\".").startswith("The reply was interrupted.")
    long = ("word " * 400 + "\n\n") * 3
    parts = chunks(long, 1000)
    assert all(len(p) <= 1000 for p in parts) and " ".join(" ".join(parts).split()) == " ".join(long.split())


def test_without_a_slack_section_there_is_no_slack(tmp_path):
    s = cfg(tmp_path)
    assert s.slack is None and server.App(s).slack is None


def test_a_channel_nobody_can_vouch_for_is_treated_as_shared(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, prompts = make(tmp_path, monkeypatch)
        m = mention("take a look", ts="6.0")
        m["event"]["channel"] = "CNONE"
        del m["is_ext_shared_channel"]
        assert bridge.receive(m)
        await settle(app, bridge)
        assert prompts == [] and web.of("chat_postMessage") == []
        assert "I can't check" in web.of("chat_postEphemeral")[0]["text"]
        await bridge.stop()
    asyncio.run(go())


def test_a_late_answer_is_held_when_the_channel_became_shared(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, prompts = make(tmp_path, monkeypatch)
        bridge.receive(mention("look into it", ts="8.0"))
        await settle(app, bridge)
        assert len(web.of("chat_postMessage")) == 1
        cid = app.store.slack_links()[0].conversation_id
        web.shared["C1"] = True
        app.store.add_message(cid, "assistant", "Found the cause.", meta={"investigation": 1})
        await bridge.post_pending()
        assert len(web.of("chat_postMessage")) == 1
        del web.shared["C1"]
        await bridge.post_pending()
        assert len(web.of("chat_postMessage")) == 1
        web.shared["C1"] = False
        await bridge.post_pending()
        assert web.of("chat_postMessage")[-1]["text"] == "Found the cause."
        d = app.store.add_draft(cid, "bug", "Video", "description")
        app.store.attach_drafts(cid, app.store.messages(cid)[-1].id)
        web.shared["C1"] = True
        await bridge.post_pending()
        assert not any(p.get("blocks") for p in web.of("chat_postMessage"))
        await bridge.stop()
    asyncio.run(go())


def test_messages_of_one_thread_are_taken_in_the_order_they_came(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, prompts = make(tmp_path, monkeypatch)
        real = bridge._files

        async def slow_first(cid, files_, who):
            if files_:
                await asyncio.sleep(0.2)
            return await real(cid, [], who)
        monkeypatch.setattr(bridge, "_files", slow_first)
        bridge.receive(dm("FIRST with a screenshot", ts="1.0", files=[{"name": "a.png"}]))
        bridge.receive(dm("SECOND a clarification", ts="1.1"))
        await settle(app, bridge)
        cid = app.store.slack_links()[0].conversation_id
        said = [m.text for m in app.store.messages(cid) if m.role == "user"]
        assert said == ["FIRST with a screenshot", "SECOND a clarification"]
        everything = "\n".join(prompts)
        assert everything.index("FIRST") < everything.index("SECOND")
        await bridge.stop()
    asyncio.run(go())


def test_a_long_answer_cut_off_midway_goes_on_from_the_missing_piece(tmp_path, monkeypatch):
    async def go():
        long = "\n\n".join(f"chunk {i} " + "word " * 500 for i in range(3))
        app, bridge, web, _ = make(tmp_path, monkeypatch, replies={1: long})
        web.fail_attempts = {2}
        bridge.receive(dm("spell it out"))
        await settle(app, bridge)
        assert [p["text"][:7] for p in web.of("chat_postMessage")] == ["chunk 0"]
        app2, bridge2, web2, _ = make(tmp_path, monkeypatch)
        await bridge2.post_pending()
        assert [p["text"][:7] for p in web2.of("chat_postMessage")] == ["chunk 1", "chunk 2"]
        await bridge2.post_pending()
        assert len(web2.of("chat_postMessage")) == 2
        assert app2.store.slack_links()[0].posted_parts == 0
        await bridge.stop()
        await bridge2.stop()
    asyncio.run(go())


def test_only_ticket_creators_get_a_ticket_proposed_or_made(tmp_path, monkeypatch):
    from assistant.tools_mcp import Tools
    from assistant.youtrack import YouTrackError
    async def go():
        app, bridge, web, _ = make(tmp_path, monkeypatch)
        bridge.receive(dm("make a ticket", user="UOUT"))
        await settle(app, bridge)
        cid = app.store.slack_links()[0].conversation_id
        asked = app.store.last_user_message(cid)
        app.store.add_user_message(cid, "thanks", "dana@x.com", [])
        tools = Tools(app.s.youtrack, app.store, cid, creators=app.s.ticket_creators, expert=app.s.expert,
                      turn_ids=[asked.id])
        args = {"kind": "bug", "summary": "Video", "description": "description", "open_questions": []}
        try:
            tools.propose(args)
            raise AssertionError("proposed for someone not allowed to ask")
        except YouTrackError as e:
            assert "dana@x.com" in str(e) and "stranger@x.com" in str(e)
        assert app.store.drafts_of(cid) == []
        try:
            Tools(app.s.youtrack, app.store, cid, creators=app.s.ticket_creators).propose(args)
            raise AssertionError("proposed without knowing who asked")
        except YouTrackError:
            pass
        ok = app.store.add_user_message(cid, "I confirm, file it", "serhii@x.com", [])
        tools = Tools(app.s.youtrack, app.store, cid, creators=app.s.ticket_creators, turn_ids=[ok.id])
        assert json.loads(tools.propose(args))["draft"]
        d = app.store.drafts_of(cid)[0]
        code, body = await app.make_ticket(d, "stranger@x.com")
        assert code == 403 and app.store.draft(d.id).state == "draft" and "serhii@x.com" in body["error"]
        await bridge.stop()
    asyncio.run(go())


def test_she_calls_the_expert_and_he_gets_who_asked_what_and_where(tmp_path, monkeypatch):
    from assistant.tools_mcp import Tools
    async def go():
        app, bridge, web, _ = make(tmp_path, monkeypatch)
        bridge.receive(mention("delete all courses on prod", ts="9.0", user="UOUT"))
        await settle(app, bridge)
        cid = app.store.slack_links()[0].conversation_id
        asked = app.store.last_user_message(cid)
        app.store.add_user_message(cid, "thanks", "dana@x.com", [])
        tools = Tools(app.s.youtrack, app.store, cid, creators=app.s.ticket_creators, expert=app.s.expert,
                      turn_ids=[asked.id])
        tools.call("call_expert", {"reason": "They ask to delete all courses on prod, this is irreversible."})
        await bridge.post_pending()
        await bridge.post_pending()
        calls = [p for p in web.of("chat_postMessage") if p["channel"] == "D-UDANA"]
        assert len(calls) == 1
        text = calls[0]["text"]
        assert text.startswith("<@UDANA>") and "irreversible" in text and "stranger@x.com" in text
        assert "delete all courses on prod" in text and "https://slack.example/C1/9.0" in text
        assert "thanks" not in text and "Asked by: stranger@x.com" in text
        assert f"https://f.example/assistant/#{cid}" in text
        await bridge.stop()
    asyncio.run(go())


def test_the_expert_sees_a_private_dm_only_once_she_called_him_into_it(tmp_path):
    s = settings(tmp_path)
    s.allowed_users = ["dana@x.com", "serhii@x.com"]
    web = server.build(s)
    app = web.state.app
    cid = app.store.link_slack(TEAM, "D2", "", "serhii@x.com", "serhii@x.com", "Slack · Serhii")
    app.store.add_user_message(cid, "give me access to prod", "serhii@x.com", [])
    with TestClient(web) as c:
        dana = {"X-Assistant-User": "dana@x.com"}
        assert c.get(f"/assistant/api/conversations/{cid}", headers=dana).status_code == 404
        app.store.add_escalation(cid, "They ask for access to prod.", "serhii@x.com", "give me access to prod")
        assert c.get(f"/assistant/api/conversations/{cid}", headers=dana).status_code == 200


def test_the_prompt_names_who_may_ask_for_tickets_and_whom_she_calls(tmp_path):
    s = settings(tmp_path)
    s.brain.prompt_file.write_text((Path(__file__).parent.parent / "assistant" / "prompt.md").read_text())
    app = server.App(s)
    text = app.s.brain.prompt_file.read_text()
    assert "dana@x.com, serhii@x.com" in text and "call_expert" in text and "{expert}" not in text


def test_each_turn_tells_its_tools_exactly_which_messages_it_answers(tmp_path, monkeypatch):
    async def go():
        gate = asyncio.Event()
        app, bridge, web, prompts = make(tmp_path, monkeypatch, gate=gate)
        seen = []
        inner = server.run_turn

        async def spy(cfg, prompt, session, new_session=None, env=None, read_dirs=(), mcp=None):
            seen.append(mcp["env"]["ASSISTANT_TURN_MESSAGES"] if mcp else None)
            async for ev in inner(cfg, prompt, session, new_session, env, read_dirs, mcp):
                yield ev
        monkeypatch.setattr(server, "run_turn", spy)
        app.s.config_path = tmp_path / "c.yaml"
        bridge.receive(dm("first", user="UOUT"))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if prompts:
                break
        bridge.receive(dm("second", ts="1.1"))
        bridge.receive(dm("third", ts="1.2"))
        await asyncio.sleep(0.1)
        gate.set()
        await settle(app, bridge)
        cid = app.store.slack_links()[0].conversation_id
        ids = [m.id for m in app.store.messages(cid) if m.role == "user"]
        assert seen == [str(ids[0]), f"{ids[1]},{ids[2]}"]
        await bridge.stop()
    asyncio.run(go())


def _finding_turn(tmp_path, monkeypatch, app, inv):
    """Tell a finding after a restart and return the turn ids its tools were given."""
    seen = []

    async def run(_cfg, prompt, session, new_session=None, env=None, read_dirs=(), mcp=None):
        seen.append(mcp["env"]["ASSISTANT_TURN_MESSAGES"])
        yield Event("done", "found it")
    monkeypatch.setattr(server, "run_turn", run)
    app.store.investigation_found(inv.id, "report", "passed", "")

    async def go():
        from assistant.server import Turn
        await app._tell(app.store.investigation(inv.id), Turn())
    asyncio.run(go())
    return [int(x) for x in seen[0].split(",") if x]


def test_a_finding_answers_exactly_the_messages_of_the_turn_that_asked_for_it(tmp_path, monkeypatch):
    from assistant.tools_mcp import Tools
    from assistant.youtrack import YouTrackError
    s = settings(tmp_path)
    app = server.App(s)
    app.s.config_path = tmp_path / "c.yaml"
    cid = app.store.create_conversation("x").id
    q1 = app.store.add_user_message(cid, "first question", "dana@x.com", [])
    q2 = app.store.add_user_message(cid, "look into why the import fails", "dana@x.com", [])
    app.store.add_message(cid, "assistant", "reply to the first", meta={"upto": q1.id, "answers": [q1.id]})
    Tools(s.youtrack, app.store, cid, engine_on=True, creators=s.ticket_creators, turn_ids=[q2.id]) \
        .investigate({"question": "Why does the course import from an archive fail?"})
    inv = app.store.investigations_in("running")[0]
    app.store.attach_investigations(cid, app.store.add_message(cid, "assistant", "I'll look into it.",
                                                               meta={"upto": q2.id}).id)
    app.store.add_user_message(cid, "thanks", "stranger@x.com", [])
    app2 = server.App(s)
    app2.s.config_path = tmp_path / "c.yaml"
    ids = _finding_turn(tmp_path, monkeypatch, app2, inv)
    assert ids == [q2.id]
    tools = Tools(s.youtrack, app2.store, cid, creators=s.ticket_creators, expert=s.expert, turn_ids=ids)
    assert json.loads(tools.propose({"kind": "bug", "summary": "Import", "description": "description",
                                     "open_questions": []}))["draft"]
    tools.call_expert({"reason": "Needs an expert on archive imports."})
    esc = app2.store.escalations_new()[0]
    assert esc["requester"] == "dana@x.com" and esc["asked"] == "look into why the import fails"


def test_a_finding_for_mixed_askers_keeps_both_and_no_ticket(tmp_path, monkeypatch):
    from assistant.tools_mcp import Tools
    from assistant.youtrack import YouTrackError
    s = settings(tmp_path)
    app = server.App(s)
    app.s.config_path = tmp_path / "c.yaml"
    cid = app.store.create_conversation("x").id
    a = app.store.add_user_message(cid, "delete my data from prod", "stranger@x.com", [])
    b = app.store.add_user_message(cid, "yes, take a look", "dana@x.com", [])
    Tools(s.youtrack, app.store, cid, engine_on=True, turn_ids=[a.id, b.id]).investigate(
        {"question": "What does it take to delete a user's data from prod?"})
    inv = app.store.investigations_in("running")[0]
    app.store.attach_investigations(cid, app.store.add_message(cid, "assistant", "I'll look into it.").id)
    ids = _finding_turn(tmp_path, monkeypatch, server.App(s), inv)
    assert ids == [a.id, b.id]
    tools = Tools(s.youtrack, app.store, cid, creators=s.ticket_creators, expert=s.expert, turn_ids=ids)
    try:
        tools.propose({"kind": "bug", "summary": "x", "description": "y", "open_questions": []})
        raise AssertionError("a ticket for a turn a non-creator asked in")
    except YouTrackError as e:
        assert "stranger@x.com" in str(e)
    tools.call_expert({"reason": "They ask to delete data from prod."})
    esc = app.store.escalations_new()[0]
    assert esc["requester"] == "stranger@x.com, dana@x.com"
    assert esc["asked"] == "delete my data from prod\nyes, take a look"


def test_called_into_a_long_thread_she_reads_all_its_pages_and_says_when_some_failed(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, prompts = make(tmp_path, monkeypatch)
        pages = {None: ([{"ts": "5.0", "user": "USER2", "text": "FIRST page"}], "p2"),
                 "p2": ([{"ts": "5.05", "user": "USER2", "text": "SECOND page"},
                         {"ts": "5.1", "user": "UDANA", "text": f"<@{BOT}> take a look"}], None)}

        async def replies(channel, ts, limit, cursor=None):
            msgs, nxt = pages[cursor]
            return {"messages": msgs, "has_more": nxt is not None, "response_metadata": {"next_cursor": nxt or ""}}
        web.conversations_replies = replies
        bridge.receive(mention("take a look", ts="5.1", thread="5.0"))
        await settle(app, bridge)
        assert "FIRST page" in prompts[0] and "SECOND page" in prompts[0]
        assert "could not be read" not in prompts[0]

        async def broken(channel, ts, limit, cursor=None):
            if cursor:
                raise RuntimeError("network")
            return {"messages": [{"ts": "6.0", "user": "USER2", "text": "the beginning"}], "has_more": True,
                    "response_metadata": {"next_cursor": "p2"}}
        web.conversations_replies = broken
        bridge.receive(mention("and here?", ts="6.5", thread="6.0"))
        await settle(app, bridge)
        assert "the beginning" in prompts[-1] and "part of the thread could not be read" in prompts[-1]
        await bridge.stop()
    asyncio.run(go())


def _thread_msg(text, ts, user="USER2"):
    return {"team_id": TEAM, "is_ext_shared_channel": False,
            "event": {"type": "message", "channel_type": "channel", "channel": "C1", "user": user, "text": text,
                      "ts": ts, "thread_ts": "5.0"}}


def quiet_run(monkeypatch, app, decide):
    """A Claude that, when `decide(prompt)` says so, calls stay_quiet the way the tools server does."""
    from assistant.tools_mcp import Tools
    seen = []

    async def run(_cfg, prompt, session, new_session=None, env=None, read_dirs=(), mcp=None):
        e = (mcp or {}).get("env", {})
        seen.append({"prompt": prompt, "quiet_ok": e.get("ASSISTANT_QUIET_OK") == "1", "key": e.get("ASSISTANT_TURN_KEY")})
        reaction = decide(prompt)
        if reaction is not None:
            from assistant.youtrack import YouTrackError
            t = Tools(app.s.youtrack, app.store, e["ASSISTANT_CONVERSATION"], turn_key=e["ASSISTANT_TURN_KEY"],
                      quiet_ok=e.get("ASSISTANT_QUIET_OK") == "1")
            yield Event("tool", "mcp__assistant__stay_quiet")
            try:
                t.call("stay_quiet", {"reaction": reaction or None, "why": "the conversation isn't for me"})
                yield Event("delta", "inner reasoning, not for people")
                yield Event("done", "")
                return
            except YouTrackError:
                pass
        yield Event("delta", "reply")
        yield Event("done", "reply")
    monkeypatch.setattr(server, "run_turn", run)
    return seen


def test_in_a_shared_thread_she_answers_when_spoken_to_and_may_keep_quiet_or_just_react_otherwise(tmp_path,
                                                                                                    monkeypatch):
    async def go():
        app, bridge, web, _ = make(tmp_path, monkeypatch)
        app.s.config_path = tmp_path / "c.yaml"
        seen = quiet_run(monkeypatch, app, lambda p: "pray" if "thanks" in p else ("" if "Serhii," in p else None))
        bridge.receive(mention("what about the release?", ts="5.1", thread="5.0"))
        await settle(app, bridge)
        assert seen[-1]["quiet_ok"] is False
        assert web.of("chat_postMessage")[-1]["text"] == "reply"
        eyes = len(web.of("reactions_add"))
        bridge.receive(_thread_msg("Serhii, will you look tomorrow?", "5.2"))
        await settle(app, bridge)
        assert seen[-1]["quiet_ok"] is True
        assert len(web.of("chat_postMessage")) == 1
        assert len(web.of("reactions_add")) == eyes
        bridge.receive(_thread_msg("thanks, it all works", "5.3"))
        await settle(app, bridge)
        assert len(web.of("chat_postMessage")) == 1
        assert web.of("reactions_add")[-1] == {"channel": "C1", "timestamp": "5.3", "name": "pray"}
        cid = app.store.slack_links()[0].conversation_id
        quiet = [m for m in app.store.messages(cid) if m.meta.get("silent")]
        assert len(quiet) == 2 and all(m.text == "" for m in quiet) and quiet[1].meta["reaction"] == "pray"
        n = len(seen)
        app2, bridge2, web2, _ = make(tmp_path, monkeypatch)
        quiet_run(monkeypatch, app2, lambda p: None)
        bridge2.resume()
        await settle(app2, bridge2)
        assert len(seen) == n and web2.of("chat_postMessage") == [] and web2.of("reactions_add") == []
        await bridge.stop()
        await bridge2.stop()
    asyncio.run(go())


def test_a_batch_with_a_message_to_her_cannot_end_quietly_even_if_the_last_one_is_chatter(tmp_path, monkeypatch):
    async def go():
        gate = asyncio.Event()
        app, bridge, web, prompts = make(tmp_path, monkeypatch, gate=gate)
        app.s.config_path = tmp_path / "c.yaml"
        bridge.receive(mention("first", ts="5.1", thread="5.0"))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if prompts:
                break
        bridge.receive(mention("and also: how many students are in the course?", ts="5.2", thread="5.0"))
        bridge.receive(_thread_msg("ok, I'm off to lunch", "5.3"))
        await asyncio.sleep(0.05)
        seen = quiet_run(monkeypatch, app, lambda p: "+1")
        gate.set()
        await settle(app, bridge)
        assert seen and seen[-1]["quiet_ok"] is False and "how many students" in seen[-1]["prompt"]
        cid = app.store.slack_links()[0].conversation_id
        msgs = app.store.messages(cid)
        assert not any(m.meta.get("silent") for m in msgs) and not any(m.role == "system" for m in msgs)
        assert msgs[-1].role == "assistant" and msgs[-1].text == "reply"
        assert web.of("chat_postMessage")[-1]["text"] == "reply"
        await bridge.stop()
    asyncio.run(go())


def test_stay_quiet_is_refused_where_she_must_answer_and_takes_only_known_reactions(tmp_path):
    from assistant.tools_mcp import Tools
    from assistant.youtrack import YouTrackError
    app = server.App(settings(tmp_path))
    cid = app.store.create_conversation("x").id
    for t, args in ((Tools(app.s.youtrack, app.store, cid, turn_key="1", quiet_ok=False), {}),
                    (Tools(app.s.youtrack, app.store, cid, turn_key="1", quiet_ok=True), {"reaction": "skull"})):
        try:
            t.call("stay_quiet", args)
            raise AssertionError("quiet where it must not be")
        except YouTrackError:
            pass
    assert app.store.quiet(cid, "1") is None


def test_a_quiet_reaction_that_failed_is_tried_again_and_the_thread_never_gets_text(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, _ = make(tmp_path, monkeypatch)
        app.s.config_path = tmp_path / "c.yaml"
        quiet_run(monkeypatch, app, lambda p: "heart" if "great" in p else None)
        bridge.receive(mention("hi", ts="5.1", thread="5.0"))
        await settle(app, bridge)
        real = web.reactions_add
        fails = {"n": 1}

        async def flaky(**kw):
            if kw.get("name") == "heart" and fails["n"]:
                fails["n"] -= 1
                raise RuntimeError("network")
            return await real(**kw)
        web.reactions_add = flaky
        bridge.receive(_thread_msg("great, thanks everyone", "5.4"))
        await settle(app, bridge)
        assert not any(r.get("name") == "heart" for r in web.of("reactions_add"))
        await bridge.post_pending()
        assert [r for r in web.of("reactions_add") if r.get("name") == "heart"] == \
            [{"channel": "C1", "timestamp": "5.4", "name": "heart"}]
        assert len(web.of("chat_postMessage")) == 1
        await bridge.stop()
    asyncio.run(go())


def test_a_quiet_turn_reads_as_silence_in_a_rebuilt_session(tmp_path, monkeypatch):
    app = server.App(settings(tmp_path))
    cid = app.store.link_slack(TEAM, "C1", "5.0", None, "dana@x.com", "t")
    q = app.store.add_user_message(cid, "thanks", "serhii@x.com", [], meta={"addressed": False})
    app.store.add_message(cid, "assistant", "", meta={"silent": True, "reaction": "pray", "upto": q.id})
    app.store.set_session(cid, "gone")
    prompts = []

    async def run(_cfg, prompt, session, new_session=None, env=None, read_dirs=(), mcp=None):
        prompts.append(prompt)
        if session:
            yield Event("error", "x", data={"num_turns": 0})
            return
        yield Event("done", "ok")
    monkeypatch.setattr(server, "run_turn", run)
    nxt = app.store.add_user_message(cid, f"<@{BOT}> what do you think?", "dana@x.com", [], meta={"addressed": True})

    async def go():
        from assistant.server import Turn
        await app._answer(cid, "dana@x.com", nxt, Turn())
    asyncio.run(go())
    assert "[the assistant stayed quiet, reacted with :pray:]" in prompts[-1]


class SlackRefusal(Exception):
    def __init__(self, error):
        super().__init__(error)
        self.response = {"error": error}


def test_a_reaction_slack_refuses_for_good_is_given_up_and_does_not_hold_up_her_next_answer(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, _ = make(tmp_path, monkeypatch)
        app.s.config_path = tmp_path / "c.yaml"
        quiet_run(monkeypatch, app, lambda p: "tada" if "released" in p else None)
        bridge.receive(mention("hi", ts="5.1", thread="5.0"))
        await settle(app, bridge)
        real = web.reactions_add
        tries = []

        async def refuse(**kw):
            if kw.get("name") == "tada":
                tries.append(kw)
                raise SlackRefusal("missing_scope")
            return await real(**kw)
        web.reactions_add = refuse
        bridge.receive(_thread_msg("released!", "5.6"))
        await settle(app, bridge)
        await bridge.post_pending()
        assert len(tries) == 1
        bridge.receive(mention("what next?", ts="5.7", thread="5.0"))
        await settle(app, bridge)
        assert [p["text"] for p in web.of("chat_postMessage")] == ["reply", "reply"]
        await bridge.stop()
    asyncio.run(go())


def test_what_she_writes_in_a_turn_that_may_end_quietly_is_not_shown_until_she_says_it(tmp_path, monkeypatch):
    async def go():
        app = server.App(settings(tmp_path))
        app.s.config_path = tmp_path / "c.yaml"
        cid = app.store.link_slack(TEAM, "C1", "5.0", None, "dana@x.com", "t")
        gate, seen_q = asyncio.Event(), asyncio.Queue()

        async def run(_cfg, prompt, session, new_session=None, env=None, read_dirs=(), mcp=None):
            yield Event("delta", "Unshown text of a quiet turn")
            await gate.wait()
            from assistant.tools_mcp import Tools
            Tools(app.s.youtrack, app.store, cid, turn_key=mcp["env"]["ASSISTANT_TURN_KEY"], quiet_ok=True) \
                .call("stay_quiet", {})
            yield Event("done", "")
        monkeypatch.setattr(server, "run_turn", run)
        m = app.store.add_user_message(cid, "Kyrylo, will you look?", "serhii@x.com", [], meta={"addressed": False})
        turn = server.Turn()
        turn.subscribers.add(seen_q)
        app.turns[cid] = turn
        task = asyncio.create_task(app._answer(cid, "serhii@x.com", m, turn))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if turn.text:
                break
        assert turn.text and turn.shown == ""
        gate.set()
        await task
        events = []
        while not seen_q.empty():
            events.append(seen_q.get_nowait())
        assert not any(e["type"] == "delta" for e in events)
        assert "Unshown" not in json.dumps(events, ensure_ascii=False)
        assert events[-1]["type"] == "done" and events[-1]["message"]["text"] == ""
    asyncio.run(go())


def test_a_quiet_turn_leaves_the_eyes_on_a_message_that_came_while_it_ran(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, _ = make(tmp_path, monkeypatch)
        app.s.config_path = tmp_path / "c.yaml"
        quiet_run(monkeypatch, app, lambda p: None)
        bridge.receive(mention("hi", ts="5.1", thread="5.0"))
        await settle(app, bridge)
        gate, gate2, second = asyncio.Event(), asyncio.Event(), asyncio.Event()
        from assistant.tools_mcp import Tools

        async def run(_cfg, prompt, session, new_session=None, env=None, read_dirs=(), mcp=None):
            e = mcp["env"]
            if e.get("ASSISTANT_QUIET_OK") == "1":
                await gate.wait()
                Tools(app.s.youtrack, app.store, e["ASSISTANT_CONVERSATION"], turn_key=e["ASSISTANT_TURN_KEY"],
                      quiet_ok=True).call("stay_quiet", {})
                yield Event("done", "")
                return
            second.set()
            await gate2.wait()
            yield Event("done", "reply to the second")
        monkeypatch.setattr(server, "run_turn", run)
        bridge.receive(_thread_msg("Serhii, will you look?", "5.2"))
        for _ in range(30):
            await asyncio.sleep(0.01)
        bridge.receive(mention("and how many modules?", ts="5.3", thread="5.0"))
        await asyncio.gather(*list(bridge.tasks))
        assert any(r["timestamp"] == "5.3" for r in web.of("reactions_add"))
        gate.set()
        await asyncio.wait_for(second.wait(), 2)
        await bridge.post_pending()
        assert "5.3" not in [r["timestamp"] for r in web.of("reactions_remove")]
        gate2.set()
        await settle(app, bridge)
        assert web.of("chat_postMessage")[-1]["text"] == "reply to the second"
        assert [r["timestamp"] for r in web.of("reactions_remove")].count("5.3") == 1
        await bridge.stop()
    asyncio.run(go())


def test_a_failed_turn_takes_the_eyes_off_what_it_answered_and_leaves_the_next_ones(tmp_path, monkeypatch):
    async def go():
        gate, gate2, second = asyncio.Event(), asyncio.Event(), asyncio.Event()
        app, bridge, web, _ = make(tmp_path, monkeypatch)
        calls = {"n": 0}
        prompts = []

        async def run(_cfg, prompt, session, new_session=None, env=None, read_dirs=(), mcp=None):
            calls["n"] += 1
            prompts.append(prompt)
            if calls["n"] == 1:
                await gate.wait()
                yield Event("error", "Limit reached, try later.")
                return
            second.set()
            await gate2.wait()
            yield Event("done", "second reply")
        monkeypatch.setattr(server, "run_turn", run)
        bridge.receive(dm("first", ts="1.0"))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if calls["n"]:
                break
        bridge.receive(dm("second", ts="1.1"))
        await asyncio.gather(*list(bridge.tasks))
        gate.set()
        await asyncio.wait_for(second.wait(), 2)
        cid = app.store.slack_links()[0].conversation_id
        error = [m for m in app.store.messages(cid) if m.role == "system"][0]
        assert error.meta["answers"] == [app.store.messages(cid)[0].id]
        await bridge.post_pending()
        assert "Limit reached" in web.of("chat_postMessage")[0]["text"]
        removed = [r["timestamp"] for r in web.of("reactions_remove")]
        assert "1.0" in removed and "1.1" not in removed
        gate2.set()
        await settle(app, bridge)
        assert "second" in prompts[-1]
        assert web.of("chat_postMessage")[-1]["text"] == "second reply"
        assert [r["timestamp"] for r in web.of("reactions_remove")].count("1.1") == 1
        await bridge.stop()
    asyncio.run(go())


def test_she_sees_the_writers_name_beside_the_email(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, prompts = make(tmp_path, monkeypatch)
        bridge.receive(mention("how do you like it here?", ts="5.1", thread="5.0", user="USER2"))
        await settle(app, bridge)
        assert "[Serhii (serhii@x.com)," in prompts[0]
        await bridge.stop()
    asyncio.run(go())


def test_her_name_speaks_to_her_only_when_she_is_called_by_it():
    from assistant.slack import called_by_name
    p, extra = "Olena Bondar", ("Lena", "Helen")
    for text in ("Olena, what about the ticket?", "Olena, take a look please", "olena: got a minute?",
                 "Lena, take a look", "Helen, what do you say?", "what do you think, Helen?", "Thanks, Olena!",
                 "Olena — a question", "Olena, take a look at `code`", "> a quote\nOlena, and you?",
                 "Olena, what does the error <@UOTHER> wrote about mean?", "Olena, take a look <@UOTHER>",
                 "&gt; <@UOTHER> wrote about it\nOlena, what do you think?", "«<@UOTHER> says ok» Olena, and you?"):
        assert called_by_name(text, p, extra), text
    for text in ("Let's invite Olena to the retro", "let Olena handle it",
                 "Feels like Taras is writing instead of Olena)))", "I'll pass it to Olena",
                 "<@UOTHER> Olena Koval was there too", "Olena's task", "Olenka, take a look", "So it was Petro who wrote it",
                 "He wrote: «Olena, take a look».", "> Olena, take a look", "&gt; Olena, take a look",
                 "<@UOTHER> Olena, take a look", "<@UOTHER> Olena, take a look", 'he says "Olena, take a look"',
                 "`Olena, take a look`", "```\nOlena, take a look\n```"):
        assert not called_by_name(text, p, extra), text
    assert not called_by_name("Olena, take a look", "", ())


def test_in_her_thread_called_by_name_without_a_tag_she_must_answer(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, _ = make(tmp_path, monkeypatch)
        app.s.name = "Olena Bondar"
        app.s.config_path = tmp_path / "c.yaml"
        seen = quiet_run(monkeypatch, app, lambda p: "")
        bridge.receive(mention("what about the release?", ts="5.1", thread="5.0"))
        await settle(app, bridge)
        eyes = len(web.of("reactions_add"))
        bridge.receive(_thread_msg("Olena, is it the same on prod?", "5.2"))
        await settle(app, bridge)
        assert seen[-1]["quiet_ok"] is False and "nobody tagged you" not in seen[-1]["prompt"]
        assert web.of("chat_postMessage")[-1]["text"] == "reply" and len(web.of("chat_postMessage")) == 2
        assert {"channel": "C1", "timestamp": "5.2", "name": "eyes"} in web.of("reactions_add")[eyes:]
        assert {"channel": "C1", "timestamp": "5.2", "name": "eyes"} in web.of("reactions_remove")
        for i, text in enumerate(("we'll invite Olena to the retro", "He wrote: «Olena, take a look».",
                                  "&gt; Olena, take a look", "<@UOTHER> Olena, take a look"), start=3):
            n = len(web.of("reactions_add"))
            bridge.receive(_thread_msg(text, f"5.{i}"))
            await settle(app, bridge)
            assert seen[-1]["quiet_ok"] is True and len(web.of("chat_postMessage")) == 2, text
            assert all(r["name"] != "eyes" for r in web.of("reactions_add")[n:]), text
        for i, text in enumerate(("Olena, take a look", "Olena, what does the error <@UOTHER> wrote about mean?",
                                  "&gt; <@UOTHER> wrote about it\nOlena, what do you think?"), start=0):
            ts = f"6.{i}"
            bridge.receive(_thread_msg(text, ts))
            await settle(app, bridge)
            assert seen[-1]["quiet_ok"] is False and len(web.of("chat_postMessage")) == 3 + i, text
            assert {"channel": "C1", "timestamp": ts, "name": "eyes"} in web.of("reactions_add"), text
        await bridge.stop()
    asyncio.run(go())


def test_a_reply_right_after_hers_is_flagged_as_likely_about_it(tmp_path, monkeypatch):
    """Under her "Pavlo, …", "So it was Petro who wrote it, not me": she stayed quiet, sure the thread was over."""
    async def go():
        app, bridge, web, _ = make(tmp_path, monkeypatch)
        app.s.config_path = tmp_path / "c.yaml"
        seen = quiet_run(monkeypatch, app, lambda p: None)
        bridge.receive(mention("what about the release?", ts="5.1", thread="5.0"))
        await settle(app, bridge)
        bridge.receive(_thread_msg("So it was Petro who wrote it, not me", "5.2"))
        await settle(app, bridge)
        assert seen[-1]["quiet_ok"] is True
        assert server.AFTER_HER in seen[-1]["prompt"] and "correct you" in seen[-1]["prompt"]
        assert "\"finished\"" in seen[-1]["prompt"]
        quiet_run(monkeypatch, app, lambda p: "")
        bridge.receive(_thread_msg("Kyrylo, what do you think?", "5.3"))
        await settle(app, bridge)
        seen2 = quiet_run(monkeypatch, app, lambda p: "")
        bridge.receive(_thread_msg("well, okay", "5.4"))
        await settle(app, bridge)
        assert server.AFTER_HER not in seen2[-1]["prompt"]
        await bridge.stop()
    asyncio.run(go())


def test_a_rebuilt_session_keeps_the_writers_names(tmp_path, monkeypatch):
    async def go():
        app, bridge, web, prompts = make(tmp_path, monkeypatch)
        bridge.receive(mention("how do you like it here?", ts="5.1", thread="5.0", user="USER2"))
        await settle(app, bridge)
        cid = app.store.slack_links()[0].conversation_id
        old = app.store.add_user_message(cid, "old, without a name", "dana@x.com", [])
        assert old.meta.get("name") is None
        app.store.set_session(cid, "lost")
        with app.store._lock:
            app.store._db.execute("UPDATE conversations SET rebuild=1 WHERE id=?", (cid,))
        bridge.receive(mention("and then?", ts="5.3", thread="5.0", user="USER2"))
        await settle(app, bridge)
        history = prompts[-1].split("\n---\n")[0]
        assert "the session was restored" in history and "[Serhii (serhii@x.com)]\n" in history and "how do you like it here?" in history
        assert "[dana@x.com]\nold, without a name" in history
        await bridge.stop()
    asyncio.run(go())


class MembersWeb(FakeWeb):
    """Two pages of channel members; a bot, a deleted account and someone of another domain among them."""
    def __init__(self, pages):
        super().__init__()
        self.pages, self.fail_members = pages, False

    async def conversations_members(self, channel, limit, cursor=None):
        self._log("conversations_members", {"channel": channel, "cursor": cursor})
        if self.fail_members:
            raise RuntimeError("ratelimited")
        i = int(cursor or 0)
        nxt = str(i + 1) if i + 1 < len(self.pages) else ""
        return {"members": self.pages[i], "response_metadata": {"next_cursor": nxt}}


MEMBERS = {
    "URG1": {"id": "URG1", "profile": {"email": "Olga@Acme.example", "real_name": "Olga"}},
    "URG2": {"id": "URG2", "profile": {"email": "dmytro@acme.example", "real_name": "Dmytro"}},
    "UCW": {"id": "UCW", "profile": {"email": "max@partner.example", "real_name": "Max"}},
    "UBOT2": {"id": "UBOT2", "is_bot": True, "profile": {"email": "bot@acme.example"}},
    "UGONE": {"id": "UGONE", "deleted": True, "profile": {"email": "gone@acme.example"}},
}


def test_channel_members_of_the_domain_may_ask_for_tickets_and_leavers_lose_it(tmp_path, monkeypatch):
    from assistant.tools_mcp import Tools
    from assistant.youtrack import YouTrackError
    monkeypatch.setitem(PEOPLE, "URG1", MEMBERS["URG1"])
    for k, v in MEMBERS.items():
        monkeypatch.setitem(PEOPLE, k, v)

    async def go():
        app, bridge, _, _ = make(tmp_path, monkeypatch)
        app.s.ticket_creators = ["dana@x.com"]
        app.s.slack.creator_channels, app.s.slack.creator_domain = ("CINT",), "acme.example"
        web = MembersWeb([["URG1", "UCW", "UBOT2"], ["URG2", "UGONE"]])
        bridge.web = web
        assert await bridge.refresh_creators() == {"olga@acme.example", "dmytro@acme.example"}
        assert app.s.creators(app.store) == ["dana@x.com", "dmytro@acme.example", "olga@acme.example"]
        assert "members of the project channel with an @acme.example email" in app.s.creators_label()
        cid = app.store.create_conversation("olga@acme.example", "t").id
        asked = app.store.add_user_message(cid, "file a ticket", "olga@acme.example", [])
        tools = Tools(app.s.youtrack, app.store, cid, creators=app.s.creators(app.store),
                      turn_ids=[asked.id], creators_label=app.s.creators_label(), leads=app.s.ticket_creators)
        args = {"kind": "bug", "summary": "Video", "description": "description", "open_questions": []}
        assert json.loads(tools.propose(args))["draft"]
        assert tools.leads == ["dana@x.com"]
        d = app.store.drafts_of(cid)[0]
        code, body = await app.make_ticket(d, "max@partner.example")
        assert code == 403 and "members of the project channel" in body["error"]
        web.fail_members = True
        try:
            await bridge.refresh_creators()
            raise AssertionError("a failed read passed")
        except RuntimeError:
            pass
        assert "olga@acme.example" in app.store.slack_creators()
        web.fail_members, web.pages = False, [["URG2"]]
        await bridge.refresh_creators()
        assert app.store.slack_creators() == {"dmytro@acme.example"}
        try:
            Tools(app.s.youtrack, app.store, cid, creators=app.s.creators(app.store), turn_ids=[asked.id],
                  creators_label=app.s.creators_label()).propose(args)
            raise AssertionError("proposed for someone who left the channel")
        except YouTrackError as e:
            assert "members of the project channel" in str(e)
        await bridge.stop()
    asyncio.run(go())


def test_channel_creators_configured_but_not_read_yet_is_nobody_not_anyone(tmp_path):
    s = settings(tmp_path)
    s.ticket_creators = []
    s.slack.creator_channels, s.slack.creator_domain = ("CINT",), "acme.example"
    app = server.App(s)
    assert app.s.creators(app.store) == [server.NOBODY]
    s.brain.prompt_file.write_text((Path(__file__).parent.parent / "assistant" / "prompt.md").read_text())
    text = server.render_prompt(s)
    assert "ask: anyone" not in text and "members of the project channel with an @acme.example email" in text
