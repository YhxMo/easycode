"""Shared helpers for Web-level tests: app wiring, a gated provider and an async poller."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from easycode.agent.loop import Agent
from easycode.config import Config
from easycode.models.base import Provider, StreamEvent, ToolCall
from easycode.tools import build_registry
from easycode.web.main import create_app
from easycode.web.store import SessionStore
from tests.conftest import FakeProvider

FAKE_MODELS = {"models": {"fake-a": "fake/a"}}


def load_config(where: Path, root: Path | None = None, data: dict | None = FAKE_MODELS) -> Config:
    """Loads the config in ``where`` (written from ``data`` first unless it is
    None) with ``root`` — ``where`` by default — as the default project."""
    if data is not None:
        (where / "easycode.config.json").write_text(json.dumps(data), encoding="utf-8")
    cfg = Config.load(start=where)
    cfg.root = root or where
    return cfg


def agent_factory(
    root: Path,
    *,
    provider: Callable[[], Provider] | None = None,
    script: list[dict] | None = None,
    created: list[Agent] | None = None,
    **agent_kw: Any,
) -> Callable[..., Agent]:
    """A ``SessionStore`` agent factory over a scripted provider.

    Like the production factory it builds each agent in the ``root`` and
    ``secondary_roots`` the store asks for (resolved), falling back to ``root``:
    a factory that dropped them would let multi-root assertions pass for the
    wrong reason. ``provider`` builds each agent's provider (default: a
    ``FakeProvider`` over a copy of ``script``); ``created`` collects the agents.
    """

    def factory(alias: str = "fake-a", **kw: Any) -> Agent:
        agent = Agent(
            provider=provider() if provider else FakeProvider(script=list(script or [])),
            registry=build_registry(8000),
            root=Path(kw["root"]).resolve() if kw.get("root") else root,
            secondary_roots=[Path(p).resolve() for p in kw.get("secondary_roots") or []],
            **agent_kw,
        )
        if created is not None:
            created.append(agent)
        return agent

    return factory


def web_app(
    cfg: Config, factory: Callable[..., Agent], root: Path | None = None, **app_kw: Any
) -> tuple[TestClient, SessionStore]:
    """The app over a fresh store rooted at ``root`` (default ``cfg.root``)."""
    store = SessionStore(cfg, root or cfg.root, factory)
    app = create_app(cfg=cfg, session_store=store, static_dir=store.root / "no-dist", **app_kw)
    return TestClient(app), store


def web_client(root: Path, data: dict | None = FAKE_MODELS, **app_kw: Any) -> TestClient:
    """The app over project ``root`` configured from ``data``, with idle fake agents."""
    return web_app(load_config(root, data=data), agent_factory(root), **app_kw)[0]


class GateProvider(Provider):
    """One provider shared by an agent's turn.

    ``stream`` call #1 waits on ``gate`` before yielding (so the session lock is
    held deterministically while the test issues concurrent requests), then serves
    the scripted items in order. Later calls skip the gate.
    """

    def __init__(self, gate: asyncio.Event, script: list[dict], model: str = "fake/model") -> None:
        super().__init__(model)
        self.gate = gate
        self.script = list(script)
        self.calls = 0

    async def stream(self, messages, tools=None) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        if self.gate is not None and self.calls == 1:
            await self.gate.wait()
        if not self.script:
            yield StreamEvent(kind="text", content="ok")
            yield StreamEvent(kind="done")
            return
        item = self.script.pop(0)
        if item.get("text"):
            for token in item["text"]:
                yield StreamEvent(kind="text", content=token)
        if item.get("tool_calls"):
            calls = [ToolCall(id=tc[0], name=tc[1], arguments=tc[2]) for tc in item["tool_calls"]]
            yield StreamEvent(kind="tool_calls", tool_calls=calls)
        yield StreamEvent(kind="done")


async def wait_until(pred, timeout: float = 2.0) -> None:
    """Yields to the running loop until ``pred`` is true (or timeout)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(0.01)
