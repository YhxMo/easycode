"""Session permissions tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easycode.config import Config
from easycode.web.main import create_app


def test_session_permission_persistence(tmp_path):
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    primary.mkdir()
    cfg_file = tmp_path / "myagent.config.json"
    cfg_file.write_text(json.dumps({"models": {"fake-a": "fake/a"}}), encoding="utf-8")
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):
        return Agent(
            provider=FakeProvider(script=[{"text": "ok"}]),
            registry=build_registry(8000),
            root=Path(kw.get("root") or primary),
        )

    store1 = SessionStore(cfg, primary, factory)
    s = store1.create(permission_mode="auto-review")
    assert s.permission_mode == "auto-review"
    assert s.agent.permission_mode == "auto-review"
    assert s.summary["permission_mode"] == "auto-review"
    assert s.summary["sandbox_mode"] == "workspace-write"
    assert s.summary["approval_policy"] == "on-request"
    assert s.summary["approvals_reviewer"] == "auto-review"

    store2 = SessionStore(cfg, primary, factory)
    store2.load_all()
    restored = store2.get(s.id)
    assert restored is not None
    assert restored.permission_mode == "auto-review"
    assert restored.agent.permission_mode == "auto-review"

    # 模式与确认标记由会话统一写入：直接改 Agent 会绕过确认标记。
    restored.set_permission_mode("allow-all")
    assert restored.summary["permission_mode"] == "allow-all"
    assert restored.full_access_confirmed is True
    store2.record_exchange(restored)
    saved = json.loads(store2._path(s.id).read_text(encoding="utf-8"))
    assert saved["permission_mode"] == "allow-all"
    assert saved["full_access_confirmed"] is True

    # 切回其他模式会清掉确认状态：再次进入完全访问需要重新确认。
    restored.set_permission_mode("ask")
    store2.record_exchange(restored)
    saved = json.loads(store2._path(s.id).read_text(encoding="utf-8"))
    assert saved["permission_mode"] == "ask"
    assert "full_access_confirmed" not in saved


def test_legacy_allow_all_session_restores_as_asking(tmp_path):
    """A session file that records full access without a consent is a file from
    before the preset was gated: it comes back asking, and the downgrade is
    written back so the user is asked exactly once."""
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    primary.mkdir()
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):
        return Agent(
            provider=FakeProvider(script=[{"text": "ok"}]),
            registry=build_registry(8000),
            root=primary,
        )

    store = SessionStore(cfg, primary, factory)
    legacy = store.dir / "legacy123.json"
    legacy.write_text(
        json.dumps(
            {
                "id": "legacy123",
                "title": "旧会话",
                "created_at": "2026-01-01T00:00:00+00:00",
                "model_alias": "fake-a",
                "permission_mode": "allow-all",
                "messages": [],
            }
        ),
        encoding="utf-8",
    )

    store.load_all()
    sess = store.get("legacy123")
    assert sess is not None
    assert sess.permission_mode == "ask"
    assert sess.agent.permission_mode == "ask"
    assert sess.full_access_confirmed is False
    assert sess.summary["sandbox_mode"] == "workspace-write"
    # 迁移落盘：旧文件不会在每次刷新时再降级一次。
    saved = json.loads(legacy.read_text(encoding="utf-8"))
    assert saved["permission_mode"] == "ask"
    assert "full_access_confirmed" not in saved

    # 已确认的完全访问会话按原样恢复。
    confirmed = store.dir / "confirmed123.json"
    confirmed.write_text(
        json.dumps(
            {
                "id": "confirmed123",
                "title": "全访问会话",
                "created_at": "2026-01-02T00:00:00+00:00",
                "model_alias": "fake-a",
                "permission_mode": "allow-all",
                "full_access_confirmed": True,
                "messages": [],
            }
        ),
        encoding="utf-8",
    )
    store2 = SessionStore(cfg, primary, factory)
    store2.load_all()
    kept = store2.get("confirmed123")
    assert kept is not None
    assert kept.permission_mode == "allow-all"
    assert kept.full_access_confirmed is True
    assert kept.summary["sandbox_mode"] == "danger-full-access"


def test_session_permission_endpoint(tmp_path):
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    primary.mkdir()
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):
        return Agent(
            provider=FakeProvider(script=[{"text": "ok"}]),
            registry=build_registry(8000),
            root=primary,
        )

    store = SessionStore(cfg, primary, factory)
    s = store.create()
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    with client:
        # 未确认的完全访问被拒绝，且不改变原有模式。
        unconfirmed = client.post(f"/api/sessions/{s.id}/permission", json={"mode": "allow-all"})
        assert unconfirmed.status_code == 422
        assert "confirm_full_access" in unconfirmed.json()["detail"]
        assert store.get(s.id).permission_mode == "ask"

        r = client.post(
            f"/api/sessions/{s.id}/permission",
            json={"mode": "allow-all", "confirm_full_access": True},
        )
        assert r.status_code == 200
        assert r.json()["permission_mode"] == "allow-all"
        assert store.get(s.id).permission_mode == "allow-all"
        assert store.get(s.id).agent.permission_mode == "allow-all"

        # 切回其他模式后，再进完全访问仍需确认。
        back = client.post(f"/api/sessions/{s.id}/permission", json={"mode": "ask"})
        assert back.status_code == 200
        assert store.get(s.id).full_access_confirmed is False
        again = client.post(f"/api/sessions/{s.id}/permission", json={"mode": "allow-all"})
        assert again.status_code == 422

        bad = client.post(f"/api/sessions/{s.id}/permission", json={"mode": "bogus"})
        assert bad.status_code == 422
        missing = client.post("/api/sessions/nope/permission", json={"mode": "ask"})
        assert missing.status_code == 404


def test_chat_with_permission_mode_on_existing_session(tmp_path):
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.session import SessionStore
    from tests.conftest import FakeProvider

    primary = tmp_path / "p"
    primary.mkdir()
    cfg = Config.load(start=tmp_path)
    cfg.root = primary

    def factory(alias: str, **kw):
        return Agent(
            provider=FakeProvider(script=[{"text": "ok"}]),
            registry=build_registry(8000),
            root=primary,
        )

    store = SessionStore(cfg, primary, factory)
    s = store.create()
    client = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    with client:
        r = client.post(
            "/api/chat",
            json={"message": "hi", "session_id": s.id, "permission_mode": "auto-review"},
        )
        assert r.status_code == 200
        # 已有会话上未确认的完全访问同样被拒绝。
        denied = client.post(
            "/api/chat",
            json={"message": "hi", "session_id": s.id, "permission_mode": "allow-all"},
        )
        assert denied.status_code == 422
        assert store.get(s.id).permission_mode == "auto-review"
        # 新建会话时也一样。
        denied_new = client.post(
            "/api/chat", json={"message": "new", "permission_mode": "allow-all"}
        )
        assert denied_new.status_code == 422
        assert len(store.list()) == 1
    assert store.get(s.id).permission_mode == "auto-review"
    assert store.get(s.id).agent.permission_mode == "auto-review"

    # 新会话带 permission_mode（完全访问需带上确认）
    client2 = TestClient(create_app(cfg=cfg, session_store=store, static_dir=tmp_path / "no-dist"))
    with client2:
        r2 = client2.post(
            "/api/chat",
            json={"message": "new", "permission_mode": "allow-all", "confirm_full_access": True},
        )
        assert r2.status_code == 200
    news = [x for x in store.list() if x.id != s.id]
    assert news and news[0].permission_mode == "allow-all"
    assert news[0].full_access_confirmed is True


@pytest.mark.asyncio
async def test_web_always_allow_matches_session_scope_no_prompt(tmp_path):
    """A pre-registered session always-allow scope skips the approval prompt."""
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.bridge import ApprovalBroker, stream_chat_with_approval
    from easycode.web.session import Session
    from tests.conftest import FakeProvider

    outside = tmp_path.parent / "always-x.txt"
    script = [
        {"tool_calls": [("c1", "write_file", {"path": str(outside), "content": "x"})], "text": ""},
        {"text": "ok"},
    ]
    agent = Agent(
        provider=FakeProvider(script=list(script)), registry=build_registry(8000), root=tmp_path
    )
    broker = ApprovalBroker()
    agent.model_alias = "fake-a"  # the agent owns the alias; Session only reads it
    sess = Session(id="s1", title="t", created_at="now", agent=agent)
    parent = str(outside.parent).rstrip("/")
    sess.always_allow.append(f"write_file:{parent}/*")

    approvals = []

    async for kind, payload in stream_chat_with_approval(agent, "go", broker, session=sess):
        if kind == "approval":
            approvals.append(payload)

    assert approvals == []
    assert outside.read_text(encoding="utf-8") == "x"


@pytest.mark.asyncio
async def test_web_approval_log_records_decision(tmp_path):
    """Resolved approvals are recorded on the session (approved / denied / expired)."""
    from easycode.agent.loop import Agent
    from easycode.tools import build_registry
    from easycode.web.bridge import ApprovalBroker, stream_chat_with_approval
    from easycode.web.session import Session
    from tests.conftest import FakeProvider

    outside = tmp_path.parent / "log-y.txt"
    script = [
        {"tool_calls": [("c1", "write_file", {"path": str(outside), "content": "y"})], "text": ""},
        {"text": "ok"},
    ]
    agent = Agent(
        provider=FakeProvider(script=list(script)), registry=build_registry(8000), root=tmp_path
    )
    broker = ApprovalBroker()

    class AutoBroker(ApprovalBroker):
        def add(self, approval_id: str):
            fut = super().add(approval_id)

            async def resolve():
                import asyncio

                await asyncio.sleep(0.05)
                fut.set_result((True, True))

            import asyncio

            asyncio.ensure_future(resolve())
            return fut

    broker = AutoBroker()
    agent.model_alias = "fake-a"  # the agent owns the alias; Session only reads it
    sess = Session(id="s1", title="t", created_at="now", agent=agent)
    async for _kind, _payload in stream_chat_with_approval(agent, "go", broker, session=sess):
        pass

    assert sess.approval_log
    log = sess.approval_log[0]
    assert log["name"] == "write_file"
    assert log["decision"] == "approved"
    assert log["always"] is True
    assert log["tool_call_id"] == "c1"
    assert log["scope"].endswith("/*")
