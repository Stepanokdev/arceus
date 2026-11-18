import asyncio
import json
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from assistant import brain, server
from assistant.brain import BrainConfig, Event, command, parse_line


def cfg(tmp: Path) -> server.Settings:
    prompt = tmp / "prompt.md"
    prompt.write_text("Assistant {project}")
    b = BrainConfig(claude_bin="claude", workdir=tmp, prompt_file=prompt, settings_file=tmp / "s.json")
    return server.Settings(data_dir=tmp / "data", brain=b, allowed_users=[], project="Northwind")


def test_command_is_read_only():
    c = command(BrainConfig("claude", Path("/m"), Path("/p"), Path("/s")), None, "sid")
    tools = c[c.index("--tools") + 1].split(",")
    assert tools == ["Read", "Grep", "Glob"]
    assert c[c.index("--permission-mode") + 1] == "dontAsk"
    assert c[c.index("--setting-sources") + 1] == "project"
    assert "--session-id" in c and "--resume" not in c
    assert "--strict-mcp-config" in c and c[c.index("--mcp-config") + 1] == '{"mcpServers": {}}'
    assert "--resume" in command(BrainConfig("claude", Path("/m"), Path("/p"), Path("/s")), "old", None)


def test_parse_stream_lines():
    assert parse_line(json.dumps({"type": "system", "subtype": "init", "session_id": "x"}))[0].data == {"session_id": "x"}
    d = parse_line(json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                                                                  "delta": {"type": "text_delta", "text": "Hel"}}}))
    assert d[0].kind == "delta" and d[0].text == "Hel"
    ok = parse_line(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "Done"}))
    assert ok[0].kind == "done" and ok[0].text == "Done"
    bad = parse_line(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True}))
    assert bad[0].kind == "error"
    assert parse_line("not json") == []


def fake_turn(events):
    async def run(cfg, prompt, session, new_session=None, env=None, **kw):
        for e in events:
            await asyncio.sleep(0.01)
            yield e
    return run


def wait_idle(client, cid, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        c = client.get(f"/assistant/api/conversations/{cid}", headers=H).json()
        if not c["busy"]:
            return c
        time.sleep(0.02)
    raise AssertionError("still busy")


H = {"X-Assistant-User": "manager@example.com"}


def test_no_header_no_access(tmp_path):
    with TestClient(server.build(cfg(tmp_path))) as client:
        assert client.get("/assistant/api/conversations").status_code == 401
        assert client.get("/assistant/").status_code == 403
        assert client.get("/assistant/", headers=H).status_code == 200


def test_allowed_users_list(tmp_path):
    s = cfg(tmp_path)
    s.allowed_users = ["boss@example.com"]
    with TestClient(server.build(s)) as client:
        assert client.get("/assistant/api/me", headers=H).status_code == 401
        assert client.get("/assistant/api/me", headers={"X-Assistant-User": "Boss@Example.com"}).status_code == 200


def test_conversation_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "run_turn", fake_turn([
        Event("session", data={"session_id": "s-1"}), Event("delta", "Hel"), Event("delta", "lo"),
        Event("done", "Hello")]))
    with TestClient(server.build(cfg(tmp_path))) as client:
        cid = client.post("/assistant/api/conversations", headers=H).json()["id"]
        r = client.post(f"/assistant/api/conversations/{cid}/messages", headers=H, json={"text": "What about the ticket?"})
        assert r.status_code == 201
        conv = wait_idle(client, cid)
        assert [m["role"] for m in conv["messages"]] == ["user", "assistant"]
        assert conv["messages"][1]["text"] == "Hello"
        assert conv["title"] == "What about the ticket?"
        app = client.app.state.app
        assert app.store.conversation(cid).claude_session == "s-1"
        assert (tmp_path / "data" / "system-prompt.md").read_text() == "Assistant Northwind"


def test_the_persona_name_reaches_the_prompt_and_the_page(tmp_path):
    s = cfg(tmp_path)
    s.brain.prompt_file.write_text("You are {name} on {project}.")
    s.name, s.intro = "Olena Bondar", "Write what the client is asking for."
    with TestClient(server.build(s)) as client:
        me = client.get("/assistant/api/me", headers=H).json()
        assert me["name"] == "Olena Bondar" and me["intro"] == "Write what the client is asking for."
    assert (tmp_path / "data" / "system-prompt.md").read_text() == "You are Olena Bondar on Northwind."


def test_the_shipped_prompt_has_the_character_and_keeps_the_rules():
    deploy = Path(__file__).parent.parent / "deploy"
    assert (deploy.parent / "assistant" / "prompt.md").read_text().startswith("You are {name}.")
    prompt = (deploy.parent / "assistant" / "prompt.md").read_text().replace("{persona}", (Path(__file__).parent / "fixtures" / "persona-she.md").read_text())
    assert "You are a woman" in prompt and "quick to laugh" in prompt
    assert "Character never outranks accuracy" in prompt and "The ticket is read by the development AI agent" in prompt
    for rule in ("not programmers", "one to four sentences", "Don't offer a menu of options", "Don't hand the work back to the person",
                 "Technical internals stay out of the chat"):
        assert rule in prompt
    assert "lighthouse" in prompt and "too punctual" in prompt and "You are a living person" not in prompt
    assert "honestly say it's a legend and you're an AI assistant" in prompt and "don't guess the name" in prompt


def test_text_before_a_tool_call_stays_in_the_saved_reply(tmp_path, monkeypatch):
    """Claude's `result` holds only the text after its last tool call. The reply people watched being written,
    the analysis before the draft included, is what is saved, with the parts as separate paragraphs."""
    monkeypatch.setattr(server, "run_turn", fake_turn([
        Event("delta", "Finding: the icon has a white background."), Event("tool", "mcp__assistant__propose_ticket"),
        Event("delta", "The draft is shown below."), Event("done", "The draft is shown below.")]))
    with TestClient(server.build(cfg(tmp_path))) as client:
        cid = client.post("/assistant/api/conversations", headers=H).json()["id"]
        client.post(f"/assistant/api/conversations/{cid}/messages", headers=H, json={"text": "icon"})
        conv = wait_idle(client, cid)
        assert conv["messages"][1]["text"] == "Finding: the icon has a white background.\n\nThe draft is shown below."


def test_second_turn_resumes_session(tmp_path, monkeypatch):
    seen = []

    async def run(cfg, prompt, session, new_session=None, env=None, **kw):
        seen.append((session, new_session))
        yield Event("session", data={"session_id": new_session or session})
        yield Event("done", "ok")
    monkeypatch.setattr(server, "run_turn", run)
    with TestClient(server.build(cfg(tmp_path))) as client:
        cid = client.post("/assistant/api/conversations", headers=H).json()["id"]
        client.post(f"/assistant/api/conversations/{cid}/messages", headers=H, json={"text": "first"})
        wait_idle(client, cid)
        client.post(f"/assistant/api/conversations/{cid}/messages", headers=H, json={"text": "second"})
        wait_idle(client, cid)
    assert seen[0][0] is None and seen[0][1]
    assert seen[1] == (seen[0][1], None)


def test_error_is_stored_and_busy_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "run_turn", fake_turn([Event("delta", "part"), Event("error", "Claude crashed")]))
    with TestClient(server.build(cfg(tmp_path))) as client:
        cid = client.post("/assistant/api/conversations", headers=H).json()["id"]
        client.post(f"/assistant/api/conversations/{cid}/messages", headers=H, json={"text": "one"})
        r2 = client.post(f"/assistant/api/conversations/{cid}/messages", headers=H, json={"text": "two"})
        assert r2.status_code == 409
        conv = wait_idle(client, cid)
        assert conv["messages"][-1]["role"] == "system" and conv["messages"][-1]["text"] == "Claude crashed"


def test_events_stream(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "run_turn", fake_turn([Event("delta", "a"), Event("delta", "b"), Event("done", "ab")]))
    with TestClient(server.build(cfg(tmp_path))) as client:
        cid = client.post("/assistant/api/conversations", headers=H).json()["id"]
        client.post(f"/assistant/api/conversations/{cid}/messages", headers=H, json={"text": "x"})
        with client.stream("GET", f"/assistant/api/conversations/{cid}/events", headers=H) as r:
            types = [json.loads(line[6:])["type"] for line in r.iter_lines() if line.startswith("data: ")]
        assert types[0] in ("snapshot", "idle")
        assert types[-1] in ("done", "idle")
        listed = client.get("/assistant/api/conversations", headers=H).json()["conversations"]
        assert listed[0]["busy"] is False


def test_lost_session_is_rebuilt_from_history(tmp_path, monkeypatch):
    calls = []

    async def run(cfg, prompt, session, new_session=None, env=None, **kw):
        calls.append((prompt, session, new_session))
        if session == "gone":
            yield Event("error", "x", data={"num_turns": 0})
            return
        yield Event("session", data={"session_id": new_session})
        yield Event("done", "Restored")
    monkeypatch.setattr(server, "run_turn", run)
    with TestClient(server.build(cfg(tmp_path))) as client:
        app = client.app.state.app
        cid = client.post("/assistant/api/conversations", headers=H).json()["id"]
        app.store.add_message(cid, "user", "old question", author="m@x")
        app.store.add_message(cid, "assistant", "old answer")
        app.store.set_session(cid, "gone")
        client.post(f"/assistant/api/conversations/{cid}/messages", headers=H, json={"text": "new"})
        conv = wait_idle(client, cid)
    assert conv["messages"][-1]["text"] == "Restored"
    prompt, session, fresh = calls[1]
    assert session is None and fresh and "old question" in prompt and "old answer" in prompt
    assert prompt.rstrip().endswith("new")
    assert app.store.conversation(cid).claude_session == fresh


def test_retry_after_error(tmp_path, monkeypatch):
    results = iter([[Event("error", "failed")], [Event("done", "worked")]])

    async def run(cfg, prompt, session, new_session=None, env=None, **kw):
        for e in next(results):
            yield e
    monkeypatch.setattr(server, "run_turn", run)
    with TestClient(server.build(cfg(tmp_path))) as client:
        cid = client.post("/assistant/api/conversations", headers=H).json()["id"]
        assert client.post(f"/assistant/api/conversations/{cid}/retry", headers=H).status_code == 400
        client.post(f"/assistant/api/conversations/{cid}/messages", headers=H, json={"text": "question"})
        wait_idle(client, cid)
        assert client.post(f"/assistant/api/conversations/{cid}/retry", headers=H).status_code == 202
        conv = wait_idle(client, cid)
    assert [m["role"] for m in conv["messages"]] == ["user", "system", "assistant"]


def test_error_words_are_plain():
    from assistant.brain import error_words
    assert "too many steps" in error_words({"subtype": "error_max_turns"})
    assert "limit" in error_words({"subtype": "error_during_execution", "result": "Claude usage limit reached"})
    assert "_" not in error_words({"subtype": "error_during_execution"})


def keyed(tmp: Path, key: str = "sk-ant-team") -> dict:
    """The team's key in assistant.env and Claude's own home: what a BrainConfig in api_key mode needs."""
    env = tmp / "assistant.env"
    env.write_text(f"ANTHROPIC_API_KEY={key}\n")
    env.chmod(0o600)
    return {"secrets": env, "claude_home": tmp / "claude-home"}


def test_brain_reports_missing_binary(tmp_path):
    c = BrainConfig(claude_bin=str(tmp_path / "nope"), workdir=tmp_path, prompt_file=tmp_path / "p",
                    settings_file=tmp_path / "s", **keyed(tmp_path))

    async def go():
        return [e async for e in brain.run_turn(c, "hi", None, "s")]
    evs = asyncio.run(go())
    assert len(evs) == 1 and evs[0].kind == "error"


def test_brain_reports_silent_exit(tmp_path):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\ncat >/dev/null\necho boom >&2\nexit 3\n")
    fake.chmod(0o755)
    c = BrainConfig(claude_bin=str(fake), workdir=tmp_path, prompt_file=tmp_path / "p", settings_file=tmp_path / "s",
                    **keyed(tmp_path))

    async def go():
        return [e async for e in brain.run_turn(c, "hi", None, "s")]
    evs = asyncio.run(go())
    assert evs[-1].kind == "error" and "boom" in evs[-1].text


def test_finished_turn_does_not_free_the_next_one(tmp_path, monkeypatch):
    """A reply that says "done" while its Claude is still exiting must not erase the busy mark of the next reply."""
    release = {}

    async def run(cfg, prompt, session, new_session=None, env=None, **kw):
        yield Event("done", f"answer to {prompt.splitlines()[-1]}")
        gate = asyncio.Event()
        release[prompt.splitlines()[-1]] = gate
        await gate.wait()
    monkeypatch.setattr(server, "run_turn", run)
    with TestClient(server.build(cfg(tmp_path))) as client:
        app = client.app.state.app
        cid = client.post("/assistant/api/conversations", headers=H).json()["id"]
        client.post(f"/assistant/api/conversations/{cid}/messages", headers=H, json={"text": "first"})
        wait_idle(client, cid)
        client.post(f"/assistant/api/conversations/{cid}/messages", headers=H, json={"text": "second"})
        end = time.time() + 2
        while "second" not in release and time.time() < end:
            time.sleep(0.01)
        second = app.turns.get(cid)
        app.turns[cid] = second or server.Turn()
        held = app.turns[cid]
        client.portal.call(release["first"].set)
        time.sleep(0.05)
        assert app.turns.get(cid) is held
        client.portal.call(release["second"].set)


def test_stream_that_subscribes_after_the_end_still_gets_it(tmp_path):
    with TestClient(server.build(cfg(tmp_path))) as client:
        app = client.app.state.app
        cid = client.post("/assistant/api/conversations", headers=H).json()["id"]
        turn = server.Turn()
        app.turns[cid] = turn
        turn.text = "done"
        turn.publish({"type": "done", "message": {"id": 1, "text": "done"}})
        with client.stream("GET", f"/assistant/api/conversations/{cid}/events", headers=H) as r:
            types = [json.loads(line[6:])["type"] for line in r.iter_lines() if line.startswith("data: ")]
        assert types == ["snapshot", "done"]


def test_reply_cut_by_restart_can_be_retried(tmp_path, monkeypatch):
    with TestClient(server.build(cfg(tmp_path))) as client:
        app = client.app.state.app
        cid = client.post("/assistant/api/conversations", headers=H).json()["id"]
        app.store.add_message(cid, "user", "question without an answer", author="m@x")
    monkeypatch.setattr(server, "run_turn", fake_turn([Event("done", "answered")]))
    with TestClient(server.build(cfg(tmp_path))) as client:
        conv = client.get(f"/assistant/api/conversations/{cid}", headers=H).json()
        assert conv["messages"][-1]["role"] == "system" and "restarted" in conv["messages"][-1]["text"]
        assert client.post(f"/assistant/api/conversations/{cid}/retry", headers=H).status_code == 202
        conv = wait_idle(client, cid)
    assert conv["messages"][-1]["text"] == "answered"


def test_cancelled_turn_leaves_an_error_not_silence(tmp_path, monkeypatch):
    async def run(cfg, prompt, session, new_session=None, env=None, **kw):
        yield Event("delta", "started")
        raise asyncio.CancelledError
    monkeypatch.setattr(server, "run_turn", run)
    with TestClient(server.build(cfg(tmp_path))) as client:
        cid = client.post("/assistant/api/conversations", headers=H).json()["id"]
        client.post(f"/assistant/api/conversations/{cid}/messages", headers=H, json={"text": "x"})
        conv = wait_idle(client, cid)
    assert conv["messages"][-1]["role"] == "system" and "interrupted" in conv["messages"][-1]["text"]


def test_timeout_covers_a_claude_that_never_reads(tmp_path):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\nsleep 30\n")
    fake.chmod(0o755)
    c = BrainConfig(claude_bin=str(fake), workdir=tmp_path, prompt_file=tmp_path / "p", settings_file=tmp_path / "s",
                    timeout_s=1, **keyed(tmp_path))

    async def go():
        return [e async for e in brain.run_turn(c, "x" * (4 * 1024 * 1024), None, "s")]
    started = time.time()
    evs = asyncio.run(go())
    assert time.time() - started < 10
    assert evs[-1].kind == "error" and "didn't answer" in evs[-1].text
