"""Session locking: who may write a session, and when.

Two rules that must not be re-derived at each call site:

- ``idle_sessions`` refuses when a target holds its chat lock, so a bulk mutation
  never kills work in flight, and holds every target for its duration.
- ``run_mutation`` finishes a state-writing worker before cancellation can
  release the locks it holds.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from easycode.web.session import Session


class SessionBusyError(RuntimeError):
    """A session-scoped operation targeted a session with an active turn."""


@asynccontextmanager
async def idle_sessions(sessions: Iterable[Session]) -> AsyncIterator[None]:
    """Serialize a session-scoped mutation against running chat turns.

    Refuses (``SessionBusyError``) when any target session holds its chat lock;
    otherwise holds every target lock for the duration so no turn can start
    mid-mutation. Must run on the event loop; targets are locked in a stable
    order so two bulk operations cannot deadlock.
    """
    ordered = sorted(sessions, key=lambda s: s.id)
    for sess in ordered:
        if sess._lock.locked():
            raise SessionBusyError(f"session busy: {sess.id}")
    async with AsyncExitStack() as stack:
        for sess in ordered:
            await stack.enter_async_context(sess._lock)
        yield


async def run_mutation(func: Callable, *args, **kwargs):
    """Finish a state-writing worker before cancellation can release its locks."""
    worker = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    cancelled = False
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled = True
    result = worker.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


def project_key(root: str | None) -> str:
    """Canonical map key for a project root (default project → "")."""
    return root or ""
