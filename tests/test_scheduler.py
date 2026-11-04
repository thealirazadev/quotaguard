"""Background loops must keep their blocking work off the event loop."""

import asyncio
import threading

from app import scheduler as scheduler_module
from app.services import rollup, webhooks


async def test_loops_run_blocking_jobs_in_worker_threads(monkeypatch):
    threads: dict[str, threading.Thread] = {}
    done = asyncio.Event()

    def fake_deliver() -> int:
        threads["webhooks"] = threading.current_thread()
        return 0

    def fake_rollup() -> dict[str, int]:
        threads["rollup"] = threading.current_thread()
        return {"scanned": 0, "upserted": 0, "restored": 0, "reconciled": 0}

    monkeypatch.setattr(webhooks, "deliver_webhooks", fake_deliver)
    monkeypatch.setattr(rollup, "run", fake_rollup)

    async def fake_sleep(_seconds):
        if len(threads) == 2:
            done.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(scheduler_module.asyncio, "sleep", fake_sleep)

    sched = scheduler_module.Scheduler()
    await sched.start()
    try:
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await sched.stop()

    assert threads["webhooks"] is not threading.main_thread()
    assert threads["rollup"] is not threading.main_thread()
