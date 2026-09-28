"""Editing an earlier user message: branch replacement, revision, and atomicity.

The behaviour under test is that an edit rewrites the conversation from the
target turn on — the replaced branch stops reaching the model and stops being
shown — while the session's own settings and everything already done to the
workspace stay exactly as they were.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient

from easycode.agent.loop import Agent
from easycode.config import Config
from easycode.tools import build_registry
from easycode.web.main import create_app
from easycode.web.store import SessionStore
from tests.conftest import FakeProvider
from tests.helpers_web import GateProvider, wait_until


def make_store(tmp_path: Path, provider) -> tuple[TestClient, SessionStore]:
    cfg_file = tmp_path / "easycode.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = tmp_path

    def factory(alias: str, **agent_kwargs):
        root = agent_kwargs.get("root") or tmp_path
        return Agent(provider=provider, registry=build_registry(8000), root=Path(root))

    store = SessionStore(cfg, tmp_path, factory)
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    return client, store


def events(body: str) -> list[dict]:
    out = []
    for line in body.splitlines():
        if line.startswith("data:"):
            out.append(json.loads(line[6:]))
    return out


def send(client: TestClient, message: str, session_id: str | None = None, **extra) -> list[dict]:
    payload: dict = {"message": message, **extra}
    if session_id:
        payload["session_id"] = session_id
    r = client.post("/api/chat", json=payload)
    assert r.status_code == 200, r.text
    return events(r.text)


def first_session_id(body: list[dict]) -> str:
    for ev in body:
        if ev.get("type") == "session":
            return ev["session_id"]
    raise AssertionError("no session event")


async def first_session_event(stream) -> str:
    """The session id from an SSE stream still in flight.

    The turn is parked inside the provider while it holds the session lock, so
    the response cannot be awaited whole — only its `session` frame is read.
    """
    async for line in stream.aiter_lines():
        if line.startswith("data:") and '"session"' in line:
            return json.loads(line[6:])["session_id"]
    raise AssertionError("no session event")


def user_texts(detail: dict) -> list[str]:
    return [m["content"] for m in detail["messages"] if m.get("role") == "user"]


def turn_id_for(detail: dict, text: str) -> str:
    """The id of the turn whose user message reads ``text``."""
    for message in detail["messages"]:
        if message.get("role") == "user" and message.get("content") == text:
            return message["turn_id"]
    raise AssertionError(f"no turn for {text!r}: {user_texts(detail)}")


def model_messages(store: SessionStore, sid: str) -> list[dict]:
    """The last payload the provider was asked to complete."""
    agent = store.get(sid).agent
    return agent.provider.calls[-1]


# ------------------------------------------------------------------ branch replacement


def test_edit_middle_turn_replaces_it_and_everything_after(tmp_path):
    """The whole tail from the edited turn on leaves the conversation."""
    provider = FakeProvider(
        script=[{"text": "r1"}, {"text": "r2"}, {"text": "r3"}, {"text": "r2-edited"}]
    )
    client, _store = make_store(tmp_path, provider)
    with client:
        sid = first_session_id(send(client, "first"))
        send(client, "second", sid)
        send(client, "third", sid)

        detail = client.get(f"/api/sessions/{sid}").json()
        assert user_texts(detail) == ["first", "second", "third"]
        target = turn_id_for(detail, "second")
        revision = detail["revision"]

        body = send(client, "second (fixed)", sid, edit_turn_id=target, expected_revision=revision)

        accepted = [ev for ev in body if ev.get("type") == "turn_accepted"]
        assert len(accepted) == 1
        assert accepted[0]["replaced_turn_id"] == target
        assert accepted[0]["revision"] == revision + 1
        # One step: the accepted state already carries the rewritten branch.
        assert [m["content"] for m in accepted[0]["messages"] if m["role"] == "user"] == [
            "first",
            "second (fixed)",
        ]

        detail = client.get(f"/api/sessions/{sid}").json()
        assert user_texts(detail) == ["first", "second (fixed)"]
        assert "third" not in json.dumps(detail["messages"])
        assert [t["status"] for t in detail["turns"]] == ["completed", "completed"]


def test_edited_turn_keeps_the_transcript_text_not_the_command_expansion(tmp_path):
    """What the user sees is what they typed; the model gets the expansion."""
    provider = FakeProvider(script=[{"text": "r1"}, {"text": "r2"}])
    client, store = make_store(tmp_path, provider)
    with client:
        sid = first_session_id(send(client, "original"))
        detail = client.get(f"/api/sessions/{sid}").json()
        target = turn_id_for(detail, "original")

        send(client, "typed again", sid, edit_turn_id=target, expected_revision=detail["revision"])

        detail = client.get(f"/api/sessions/{sid}").json()
        assert user_texts(detail) == ["typed again"]
        assert store.get(sid).turns[0]["raw_input"] == "typed again"
        assert store.get(sid).turns[0]["model_input"] == "typed again"


def test_edit_first_turn_leaves_nothing_of_the_old_branch(tmp_path):
    provider = FakeProvider(script=[{"text": "r1"}, {"text": "r2"}, {"text": "fresh"}])
    client, store = make_store(tmp_path, provider)
    with client:
        sid = first_session_id(send(client, "one"))
        send(client, "two", sid)
        detail = client.get(f"/api/sessions/{sid}").json()
        target = turn_id_for(detail, "one")

        send(client, "restart", sid, edit_turn_id=target, expected_revision=detail["revision"])

        detail = client.get(f"/api/sessions/{sid}").json()
        assert user_texts(detail) == ["restart"]
        # The request the model saw carries neither old prompt nor old reply —
        # compared per message, since a substring scan trips on the system
        # prompt. The edited turn's own reply is still streaming, so the payload
        # holds only what preceded it.
        payload = model_messages(store, sid)
        said = [str(m.get("content")) for m in payload if m.get("role") != "system"]
        # The provider records the request before it answers, so the payload is
        # the turn's input with nothing of the branch it replaced.
        assert said == ["restart"]
        assert [m["content"] for m in detail["messages"] if m["role"] == "assistant"] == ["fresh"]


def test_edit_last_turn_keeps_the_earlier_ones(tmp_path):
    provider = FakeProvider(script=[{"text": "first reply"}, {"text": "gone"}, {"text": "kept reply"}])
    client, _store = make_store(tmp_path, provider)
    with client:
        sid = first_session_id(send(client, "alpha"))
        send(client, "beta", sid)
        detail = client.get(f"/api/sessions/{sid}").json()
        target = turn_id_for(detail, "beta")

        send(client, "beta again", sid, edit_turn_id=target, expected_revision=detail["revision"])

        detail = client.get(f"/api/sessions/{sid}").json()
        assert user_texts(detail) == ["alpha", "beta again"]
        replies = [m["content"] for m in detail["messages"] if m["role"] == "assistant"]
        assert replies == ["first reply", "kept reply"]


# ------------------------------------------------------------------ compaction


def test_edit_after_compaction_excludes_the_replaced_branch(tmp_path):
    """A summary of the old branch must not smuggle it back into the request."""
    provider = FakeProvider(script=[{"text": "one"}, {"text": "two"}, {"text": "three"}])
    client, store = make_store(tmp_path, provider)
    with client:
        sid = first_session_id(send(client, "alpha"))
        send(client, "beta", sid)
        send(client, "gamma", sid)
        sess = store.get(sid)
        # Two compaction rounds, as the loop would run them mid-turn.
        assert sess.agent.history.condense_from("SUMMARY-A", 2) is True
        assert sess.agent.history.condense_from("SUMMARY-B", 2) is True
        assert sess.agent.history.summary == "SUMMARY-B"
        store.record_exchange(sess)

        detail = client.get(f"/api/sessions/{sid}").json()
        target = turn_id_for(detail, "gamma")
        send(client, "gamma (fixed)", sid, edit_turn_id=target, expected_revision=detail["revision"])

        payload = json.dumps(model_messages(store, sid))
        assert "gamma (fixed)" in payload
        assert "gamma" not in payload.replace("gamma (fixed)", "")
        # The old summary described turns that no longer exist; it is gone.
        assert "SUMMARY-A" not in payload and "SUMMARY-B" not in payload
        # The transcript still shows everything before the edited turn.
        detail = client.get(f"/api/sessions/{sid}").json()
        assert user_texts(detail) == ["alpha", "beta", "gamma (fixed)"]


def test_compaction_does_not_shorten_the_visible_history(tmp_path):
    provider = FakeProvider(script=[{"text": "a"}, {"text": "b"}, {"text": "c"}])
    client, store = make_store(tmp_path, provider)
    with client:
        sid = first_session_id(send(client, "alpha"))
        send(client, "beta", sid)
        send(client, "gamma", sid)
        sess = store.get(sid)
        assert sess.agent.history.condense_from("SUMMARY", 4) is True
        store.record_exchange(sess)
        # The context lost a turn; the conversation did not.
        assert len(sess.agent.history.messages) < len(sess.turns) + 1
        detail = client.get(f"/api/sessions/{sid}").json()
        assert user_texts(detail) == ["alpha", "beta", "gamma"]
        # …and a reload rebuilds it from the turns, not from the context.
        restored = SessionStore(store.cfg, tmp_path, store.agent_factory)
        restored.load_all()
        assert [t["raw_input"] for t in restored.get(sid).turns] == ["alpha", "beta", "gamma"]


# ------------------------------------------------------------------ tool pairing


def test_edit_keeps_tool_calls_paired_and_usable(tmp_path):
    """Replaying a retained turn must not orphan a tool result."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    provider = FakeProvider(
        script=[
            {"text": "", "tool_calls": [("c1", "glob", {"pattern": "*.py"})]},
            {"text": "found it"},
            {"text": "", "tool_calls": [("c2", "read_file", {"path": "a.py"})]},
            {"text": "second look"},
            {"text": "redone"},
        ]
    )
    client, store = make_store(tmp_path, provider)
    with client:
        sid = first_session_id(send(client, "look around"))
        send(client, "again please", sid)
        detail = client.get(f"/api/sessions/{sid}").json()
        # Turn boundaries: the glob turn, then the read turn.
        target = detail["turns"][1]["id"]
        send(client, "re-read it", sid, edit_turn_id=target, expected_revision=detail["revision"])

        payload = model_messages(store, sid)
        roles = [m["role"] for m in payload]
        # Every assistant tool_calls is followed by its results, and the replayed
        # first turn is still intact so the model can keep using the tools.
        pending: list[str] = []
        for message in payload:
            if message.get("role") == "assistant":
                pending = [tc["id"] for tc in (message.get("tool_calls") or [])]
            elif message.get("role") == "tool":
                assert message["tool_call_id"] in pending, roles
                pending.remove(message["tool_call_id"])
        assert pending == []
        assert any(m.get("name") == "glob" for m in payload if m["role"] == "tool")


# ------------------------------------------------------------------ side effects preserved


def test_edit_leaves_files_and_session_settings_alone(tmp_path):
    target_file = tmp_path / "note.txt"
    provider = FakeProvider(
        script=[
            {"text": "", "tool_calls": [("c1", "write_file", {"path": "note.txt", "content": "hi"})]},
            {"text": "written"},
            {"text": "redone"},
        ]
    )
    client, store = make_store(tmp_path, provider)
    with client:
        sid = first_session_id(send(client, "create the file"))
        sess = store.get(sid)
        sess.always_allow.append("write_file:*")
        sess.pinned = True
        sess.pinned_at = "2026-01-01T00:00:00+00:00"
        store.record_exchange(sess)
        assert target_file.read_text(encoding="utf-8") == "hi"

        send(client, "add another line", sid)
        detail = client.get(f"/api/sessions/{sid}").json()
        target = detail["turns"][1]["id"]
        send(client, "forget it", sid, edit_turn_id=target, expected_revision=detail["revision"])

        # The file is exactly as it was: an edit rewrites the conversation, not
        # the workspace, and nothing here rolls anything back.
        assert target_file.read_text(encoding="utf-8") == "hi"
        sess = store.get(sid)
        assert sess.always_allow == ["write_file:*"]
        assert sess.pinned is True
        assert sess.title == "create the file"


def test_edit_drops_records_of_the_replaced_branch(tmp_path):
    provider = FakeProvider(
        script=[
            {"text": "", "tool_calls": [("c1", "write_file", {"path": "kept.txt", "content": "a"})]},
            {"text": "one"},
            {"text": "", "tool_calls": [("c2", "write_file", {"path": "dropped.txt", "content": "b"})]},
            {"text": "two"},
            {"text": "again"},
        ]
    )
    client, _store = make_store(tmp_path, provider)
    with client:
        sid = first_session_id(send(client, "first"))
        send(client, "second", sid)
        detail = client.get(f"/api/sessions/{sid}").json()
        assert {a["args"].get("path") for a in detail["artifacts"]} == {"kept.txt", "dropped.txt"}
        target = detail["turns"][1]["id"]

        send(client, "second again", sid, edit_turn_id=target, expected_revision=detail["revision"])

        detail = client.get(f"/api/sessions/{sid}").json()
        assert {a["args"].get("path") for a in detail["artifacts"]} == {"kept.txt"}
        # The file the dropped turn wrote is still on disk — only the record went.
        assert (tmp_path / "dropped.txt").exists()


def test_edit_restores_the_task_list_from_before_the_turn(tmp_path):
    provider = FakeProvider(
        script=[
            {"text": "", "tool_calls": [("c1", "update_todos", {"todos": [{"text": "step one", "status": "in_progress"}]})]},
            {"text": "planned"},
            {"text": "again"},
        ]
    )
    client, store = make_store(tmp_path, provider)
    with client:
        sid = first_session_id(send(client, "plan it"))
        sess = store.get(sid)
        assert [t["text"] for t in sess.agent.todos] == ["step one"]
        detail = client.get(f"/api/sessions/{sid}").json()
        target = detail["turns"][0]["id"]

        send(client, "plan it differently", sid, edit_turn_id=target, expected_revision=detail["revision"])

        # Editing the turn that introduced the list puts the list back to what it
        # was before that turn ran.
        assert sess.agent.todos == []
        detail = client.get(f"/api/sessions/{sid}").json()
        assert detail["todos"] == []


def test_edit_carries_failures_and_approvals_with_their_turn(tmp_path):
    provider = FakeProvider(
        script=[{"text": "fine"}, {"text": "", "tool_calls": [("c1", "glob", {"pattern": "*"})]}, {"text": "ok"}]
    )
    client, store = make_store(tmp_path, provider)
    with client:
        sid = first_session_id(send(client, "one"))
        send(client, "two", sid)
        sess = store.get(sid)
        detail = client.get(f"/api/sessions/{sid}").json()
        second = detail["turns"][1]["id"]
        # A server error and an approval decision, both belonging to turn two.
        # A failure and a failed status go together: a finished turn has none.
        sess.turns[1]["status"] = "failed"
        sess.record_turn_failure("boom", "tool_iteration_limit", second)
        sess.approval_log.append(
            {"tool_call_id": "c1", "name": "glob", "args": {}, "decision": "approved", "turn_id": second}
        )
        store.record_exchange(sess)
        assert len(client.get(f"/api/sessions/{sid}").json()["turn_failures"]) == 1

        send(client, "two again", sid, edit_turn_id=second, expected_revision=detail["revision"])

        detail = client.get(f"/api/sessions/{sid}").json()
        assert detail["turn_failures"] == []
        assert detail["approvals"] == []


# ------------------------------------------------------------------ revision and errors


def test_stale_revision_is_refused_and_the_conversation_is_untouched(tmp_path):
    provider = FakeProvider(script=[{"text": "a"}, {"text": "b"}, {"text": "c"}])
    client, _store = make_store(tmp_path, provider)
    with client:
        sid = first_session_id(send(client, "one"))
        detail = client.get(f"/api/sessions/{sid}").json()
        target = turn_id_for(detail, "one")
        stale = detail["revision"]
        send(client, "two", sid)  # the conversation moves on

        r = client.post(
            "/api/chat",
            json={
                "message": "one again",
                "session_id": sid,
                "edit_turn_id": target,
                "expected_revision": stale,
            },
        )
        assert r.status_code == 409
        assert "刷新" in r.json()["detail"]

        detail = client.get(f"/api/sessions/{sid}").json()
        assert user_texts(detail) == ["one", "two"]


def test_missing_turn_and_missing_revision_are_client_errors(tmp_path):
    provider = FakeProvider(script=[{"text": "a"}])
    client, _store = make_store(tmp_path, provider)
    with client:
        sid = first_session_id(send(client, "one"))
        detail = client.get(f"/api/sessions/{sid}").json()

        r = client.post(
            "/api/chat",
            json={
                "message": "x",
                "session_id": sid,
                "edit_turn_id": "nope",
                "expected_revision": detail["revision"],
            },
        )
        assert r.status_code == 404

        r = client.post(
            "/api/chat",
            json={"message": "x", "session_id": sid, "edit_turn_id": detail["turns"][0]["id"]},
        )
        assert r.status_code == 422

        # Neither attempt changed anything.
        assert user_texts(client.get(f"/api/sessions/{sid}").json()) == ["one"]


def test_edit_without_a_session_is_refused(tmp_path):
    provider = FakeProvider(script=[{"text": "a"}])
    client, _store = make_store(tmp_path, provider)
    with client:
        r = client.post(
            "/api/chat",
            json={"message": "x", "edit_turn_id": "t1", "expected_revision": 0},
        )
        assert r.status_code == 422
        assert client.get("/api/sessions").json() == []


# ------------------------------------------------------------------ concurrency# ------------------------------------------------------------------ concurrency


def test_busy_session_rejects_an_edit_instead_of_queueing_it(tmp_path):
    """An edit that arrives while a turn runs is refused, never held for later.

    A queued edit would rewrite a conversation the running turn is still adding
    to, so the only safe answer is 409 — and it has to be the server's answer,
    not a client-side guess.
    """
    gate = asyncio.Event()
    provider = GateProvider(gate=gate, script=[{"text": "first"}, {"text": "second"}])
    client, store = make_store(tmp_path, provider)

    async def scenario() -> None:
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client.app), base_url="http://t"
        ) as c:
            # The turn parks in the provider while holding the session lock.
            running = asyncio.create_task(c.post("/api/chat", json={"message": "one"}))
            await wait_until(lambda: len(store.list()) == 1)
            sid = store.list()[0].id
            await wait_until(store.get(sid)._lock.locked)

            detail = (await c.get(f"/api/sessions/{sid}")).json()
            busy = await c.post(
                "/api/chat",
                json={
                    "message": "edited",
                    "session_id": sid,
                    "edit_turn_id": detail["turns"][0]["id"],
                    "expected_revision": detail["revision"],
                },
            )
            assert busy.status_code == 409
            assert "busy" in busy.json()["detail"]

            gate.set()
            assert (await running).status_code == 200
            # The refused edit left nothing behind.
            assert user_texts((await c.get(f"/api/sessions/{sid}")).json()) == ["one"]

    asyncio.run(scenario())


def test_second_edit_targets_the_branch_the_first_one_left(tmp_path):
    """Two edits in a row: each names the revision it read."""
    provider = FakeProvider(script=[{"text": "a"}, {"text": "b"}, {"text": "c"}, {"text": "d"}])
    client, _store = make_store(tmp_path, provider)
    with client:
        sid = first_session_id(send(client, "one"))
        send(client, "two", sid)

        detail = client.get(f"/api/sessions/{sid}").json()
        send(client, "two v2", sid, edit_turn_id=detail["turns"][1]["id"], expected_revision=detail["revision"])

        detail = client.get(f"/api/sessions/{sid}").json()
        assert user_texts(detail) == ["one", "two v2"]
        send(client, "two v3", sid, edit_turn_id=detail["turns"][1]["id"], expected_revision=detail["revision"])

        detail = client.get(f"/api/sessions/{sid}").json()
        assert user_texts(detail) == ["one", "two v3"]
        assert [t["status"] for t in detail["turns"]] == ["completed", "completed"]


def test_a_deleted_session_is_not_rewritten_by_its_own_edit(tmp_path):
    """The lock is what serialises an edit against a delete, and the store
    refuses to flush a session it no longer tracks — so a delete that lands
    first cannot be undone by the edit that was already in the request."""
    provider = FakeProvider(script=[{"text": "a"}, {"text": "b"}])
    client, store = make_store(tmp_path, provider)
    with client:
        sid = first_session_id(send(client, "one"))
        detail = client.get(f"/api/sessions/{sid}").json()

        assert client.delete(f"/api/sessions/{sid}").status_code == 200
        assert store.get(sid) is None

        # The id is gone, so an edit naming it is a 404 — never a silent
        # resurrection of the conversation the user just removed.
        late = client.post(
            "/api/chat",
            json={
                "message": "edited",
                "session_id": sid,
                "edit_turn_id": detail["turns"][0]["id"],
                "expected_revision": detail["revision"],
            },
        )
        assert late.status_code == 404

    store.load_all()
    assert store.get(sid) is None
    assert not store._path(sid).exists()


# ------------------------------------------------------------------ old sessions


def test_legacy_session_recovers_turns_with_stable_ids(tmp_path):
    """A file written before turns existed is readable and editable."""
    sessions = tmp_path / ".easycode" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "old1.json").write_text(
        json.dumps(
            {
                "id": "old1",
                "title": "旧会话",
                "created_at": "2026-01-01T00:00:00+00:00",
                "model_alias": "fake-a",
                "permission_mode": "ask",
                "messages": [
                    {"role": "user", "content": "old question"},
                    {"role": "assistant", "content": "old answer"},
                    {"role": "user", "content": "follow up"},
                    {"role": "assistant", "content": "second answer"},
                ],
                "user_times": ["2026-01-01T00:00:01+00:00", "2026-01-01T00:00:02+00:00"],
            }
        ),
        encoding="utf-8",
    )
    provider = FakeProvider(script=[{"text": "redone"}])
    client, store = make_store(tmp_path, provider)
    with client:
        detail = client.get("/api/sessions/old1").json()
        assert user_texts(detail) == ["old question", "follow up"]
        assert len(detail["turns"]) == 2
        ids = [t["id"] for t in detail["turns"]]

        # Reading twice must not renumber: a client holding an id would break.
        store2 = SessionStore(store.cfg, tmp_path, store.agent_factory)
        store2.load_all()
        assert [t["id"] for t in store2.get("old1").turns] == ids

        send(
            client,
            "fixed question",
            "old1",
            edit_turn_id=ids[0],
            expected_revision=detail["revision"],
        )
        detail = client.get("/api/sessions/old1").json()
        assert user_texts(detail) == ["fixed question"]
        assert detail["messages"][0]["turn_id"] != ids[0]


def test_restart_preserves_branch_ids_and_history(tmp_path):
    provider = FakeProvider(script=[{"text": "a"}, {"text": "b"}, {"text": "c"}])
    client, store = make_store(tmp_path, provider)
    with client:
        sid = first_session_id(send(client, "one"))
        send(client, "two", sid)
        detail = client.get(f"/api/sessions/{sid}").json()
        target = detail["turns"][1]["id"]
        send(client, "two again", sid, edit_turn_id=target, expected_revision=detail["revision"])
        detail = client.get(f"/api/sessions/{sid}").json()

    # A fresh store over the same directory: the ids and the branch survive.
    restored = SessionStore(store.cfg, tmp_path, store.agent_factory)
    restored.load_all()
    sess = restored.get(sid)
    assert sess is not None
    assert [t["raw_input"] for t in sess.turns] == ["one", "two again"]
    assert sess.turns[1]["id"] == detail["turns"][1]["id"]
    assert sess.revision == detail["revision"]


def test_interrupted_running_turn_is_repaired_on_load(tmp_path):
    """A session killed mid-turn comes back continuable, with tools paired."""
    sessions = tmp_path / ".easycode" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "cut.json").write_text(
        json.dumps(
            {
                "id": "cut",
                "title": "被中断",
                "created_at": "2026-01-01T00:00:00+00:00",
                "model_alias": "fake-a",
                "permission_mode": "ask",
                "messages": [{"role": "user", "content": "go"}],
                "turns": [
                    {
                        "id": "t1",
                        "created_at": "2026-01-01T00:00:01+00:00",
                        "raw_input": "go",
                        "model_input": "go",
                        "status": "running",
                        "messages": [
                            {"role": "user", "content": "go"},
                            {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "c1",
                                        "type": "function",
                                        "function": {"name": "glob", "arguments": "{}"},
                                    }
                                ],
                            },
                        ],
                    }
                ],
                "revision": 1,
            }
        ),
        encoding="utf-8",
    )
    provider = FakeProvider(script=[{"text": "carried on"}])
    client, store = make_store(tmp_path, provider)
    with client:
        sess = store.get("cut")
        assert sess.turns[0]["status"] == "cancelled"
        roles = [m["role"] for m in sess.agent.history.messages]
        assert roles == ["user", "assistant", "tool"]
        # …and the session can carry on.
        assert client.post("/api/chat", json={"message": "继续", "session_id": "cut"}).status_code == 200
        assert user_texts(client.get("/api/sessions/cut").json()) == ["go", "继续"]


# ------------------------------------------------------------------ atomicity


def test_failed_persist_leaves_the_old_branch_in_place(tmp_path, monkeypatch):
    """An edit that cannot be saved must not take effect in memory either."""
    provider = FakeProvider(script=[{"text": "a"}, {"text": "b"}, {"text": "c"}])
    client, store = make_store(tmp_path, provider)
    with client:
        sid = first_session_id(send(client, "one"))
        send(client, "two", sid)
        detail = client.get(f"/api/sessions/{sid}").json()
        target = detail["turns"][1]["id"]

        original = Path.replace

        def failing_replace(path, dest):
            if Path(dest) == store._path(sid):
                raise OSError("disk full")
            return original(path, dest)

        monkeypatch.setattr(Path, "replace", failing_replace)
        r = client.post(
            "/api/chat",
            json={
                "message": "two again",
                "session_id": sid,
                "edit_turn_id": target,
                "expected_revision": detail["revision"],
            },
        )
        monkeypatch.undo()
        assert r.status_code == 200  # the stream opened, then reported the failure
        assert "保存会话失败" in r.text

        # The edit never became the conversation: same branch, same revision as
        # before the request, and nothing to accept on a reload.
        assert "turn_accepted" not in r.text
        detail = client.get(f"/api/sessions/{sid}").json()
        assert user_texts(detail) == ["one", "two"]
        assert [t["status"] for t in detail["turns"]] == ["completed", "completed"]
        assert store.get(sid).revision == detail["revision"] == 2


# ------------------------------------------------------------------ terminal turns


def test_a_failed_turn_can_be_edited_and_the_error_goes_with_it(tmp_path):
    """A turn that ended on an error is still a turn the user can rewrite."""
    provider = FakeProvider(script=[{"error": "boom"}, {"text": "fixed"}])
    client, _store = make_store(tmp_path, provider)
    with client:
        sid = first_session_id(send(client, "one"))
        detail = client.get(f"/api/sessions/{sid}").json()
        assert detail["turns"][0]["status"] == "failed"
        target = detail["turns"][0]["id"]

        send(client, "one again", sid, edit_turn_id=target, expected_revision=detail["revision"])

        detail = client.get(f"/api/sessions/{sid}").json()
        assert user_texts(detail) == ["one again"]
        assert detail["turn_failures"] == []
        assert detail["turns"][0]["status"] == "completed"


def test_a_stopped_turn_can_be_edited(tmp_path):
    """Stopping a turn leaves it cancelled and unfinished — not uneditable."""
    cfg = Config.load(start=tmp_path)
    cfg.root = tmp_path
    (tmp_path / "easycode.config.json").write_text(
        json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8"
    )
    gate = asyncio.Event()
    gate.set()

    def factory(alias: str = "fake-a", **_):
        return Agent(
            provider=GateProvider(gate=gate, script=[{"text": "never arrives"}, {"text": "redone"}]),
            registry=build_registry(8000),
            root=tmp_path,
        )

    store = SessionStore(cfg, tmp_path, factory)

    async def scenario() -> None:
        import httpx

        app = create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            sess = store.create()
            gate.clear()
            running = asyncio.create_task(
                c.post("/api/chat", json={"message": "slow one", "session_id": sess.id})
            )
            await wait_until(sess._lock.locked)
            assert (await c.post(f"/api/sessions/{sess.id}/cancel")).json()["cancelled"] is True
            gate.set()
            assert (await running).status_code == 200

            detail = (await c.get(f"/api/sessions/{sess.id}")).json()
            assert detail["turns"][0]["status"] == "cancelled"
            assert user_texts(detail) == ["slow one"]

            assert (
                await c.post(
                    "/api/chat",
                    json={
                        "message": "slow one again",
                        "session_id": sess.id,
                        "edit_turn_id": detail["turns"][0]["id"],
                        "expected_revision": detail["revision"],
                    },
                )
            ).status_code == 200
            detail = (await c.get(f"/api/sessions/{sess.id}")).json()
            assert user_texts(detail) == ["slow one again"]
            assert detail["turns"][0]["status"] == "completed"

    asyncio.run(scenario())
