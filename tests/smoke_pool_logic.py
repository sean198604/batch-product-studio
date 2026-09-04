"""Pure-logic test of the parallel pool (no asyncio tasks, no DB).

Exercises ``_accept_task``, ``_pick_next_item``, ``_cleanup_order`` and
``queue_info_for`` directly so we verify the data-structure invariants
without the noise of concurrent scheduling.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="bps_pool_logic_"))
os.environ["SQLITE_PATH"] = str(TMP / "app.db")
os.environ["STORAGE_DIR"] = str(TMP / "storage")
os.environ["DATA_DIR"] = str(TMP / "data")
os.environ["MAX_CONCURRENT_TASKS"] = "4"

from app import worker  # noqa: E402
from app.config import settings  # noqa: E402

worker._max_concurrent = 4
worker._wakeup = type("E", (), {"set": lambda self: None, "clear": lambda self: None})()


def assert_eq(label, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {label}: got={got!r} want={want!r}")
    if not ok:
        raise AssertionError(f"{label}: got {got!r}, want {want!r}")


def reset():
    worker._active_task_ids.clear()
    worker._waiting_task_ids.clear()
    worker._item_queues.clear()


def main():
    reset()

    # ── Case 1: 4 active + 3 waiting ────────────────────────────────
    print("\n── Case 1: enqueue order is preserved (FIFO)")
    worker._accept_task("t0", ["i0"])
    worker._accept_task("t1", ["i1"])
    worker._accept_task("t2", ["i2"])
    worker._accept_task("t3", ["i3"])
    worker._accept_task("t4", ["i4"])
    worker._accept_task("t5", ["i5"])
    worker._accept_task("t6", ["i6"])

    assert_eq("active = first 4", list(worker._active_task_ids), ["t0", "t1", "t2", "t3"])
    assert_eq("waiting = next 3", list(worker._waiting_task_ids), ["t4", "t5", "t6"])

    # ── Case 2: pick returns FIFO active task's item ─────────────────
    print("\n── Case 2: pick_next_item round-robins active set in FIFO order")
    p1 = worker._pick_next_item(); assert_eq("pick 1", p1, ("t0", "i0"))
    p2 = worker._pick_next_item(); assert_eq("pick 2", p2, ("t1", "i1"))
    # task t0 is now empty; cleanup removes it from active.
    worker._cleanup_order("t0")
    p3 = worker._pick_next_item(); assert_eq("pick 3", p3, ("t2", "i2"))
    p4 = worker._pick_next_item(); assert_eq("pick 4", p4, ("t3", "i3"))
    worker._cleanup_order("t1")
    p5 = worker._pick_next_item(); assert_eq("pick 5", p5, ("t4", "i4"))
    # active now contains t2/t3/t4 (t0/t1 cleaned) + t5 promoted from waiting
    assert_eq("active after promote", list(worker._active_task_ids), ["t2", "t3", "t4", "t5"])

    # ── Case 3: queue_info_for ahead counts are sane ────────────────
    print("\n── Case 3: tasks_ahead respects active+waiting FIFO")
    # active is [t2, t3, t4, t5], waiting is [t6]
    assert_eq("ahead t2", worker.queue_info_for("t2")[0], 0)
    assert_eq("ahead t5", worker.queue_info_for("t5")[0], 3)
    assert_eq("ahead t6", worker.queue_info_for("t6")[0], 4)
    assert_eq("ahead unknown", worker.queue_info_for("nope")[0], 0)

    # ── Case 4: drain with 5 tasks (4 active + 1 waiting) ───────────────
    print("\n── Case 4: 5 tasks (4 active + 1 waiting) drain cleanly")
    reset()
    for tid in ["t0", "t1", "t2", "t3", "t4"]:
        worker._accept_task(tid, [f"{tid}i"])

    assert_eq("active", list(worker._active_task_ids), ["t0", "t1", "t2", "t3"])
    assert_eq("waiting", list(worker._waiting_task_ids), ["t4"])

    # Round 1: pick t0, cleanup → promotes t4 from waiting.
    p = worker._pick_next_item(); assert_eq("pick#1", p, ("t0", "t0i"))
    worker._cleanup_order("t0"); snap = worker.pool_snapshot()
    assert_eq("after #1 active", snap["active_task_ids"], ["t1", "t2", "t3", "t4"])
    assert_eq("after #1 waiting", snap["waiting_task_ids"], [])

    # Round 2: pick t1, cleanup (no waiting).
    p = worker._pick_next_item(); assert_eq("pick#2", p, ("t1", "t1i"))
    worker._cleanup_order("t1"); snap = worker.pool_snapshot()
    assert_eq("after #2 active", snap["active_task_ids"], ["t2", "t3", "t4"])

    # Round 3: pick t2, cleanup.
    p = worker._pick_next_item(); assert_eq("pick#3", p, ("t2", "t2i"))
    worker._cleanup_order("t2"); snap = worker.pool_snapshot()
    assert_eq("after #3 active", snap["active_task_ids"], ["t3", "t4"])

    # Round 4: pick t3, cleanup.
    p = worker._pick_next_item(); assert_eq("pick#4", p, ("t3", "t3i"))
    worker._cleanup_order("t3"); snap = worker.pool_snapshot()
    assert_eq("after #4 active", snap["active_task_ids"], ["t4"])

    # Round 5: pick t4, cleanup — queue should drain to empty.
    p = worker._pick_next_item(); assert_eq("pick#5", p, ("t4", "t4i"))
    worker._cleanup_order("t4"); snap = worker.pool_snapshot()
    assert_eq("after #5 active", snap["active_task_ids"], [])
    assert_eq("after #5 waiting", snap["waiting_task_ids"], [])
    assert_eq("active empty", list(worker._active_task_ids), [])
    assert_eq("waiting empty", list(worker._waiting_task_ids), [])
    assert_eq("item_queues empty", dict(worker._item_queues), {})

    # ── Case 5: re-enqueue after drain behaves correctly ─────────────
    print("\n── Case 5: re-enqueue fills slots in submission order")
    reset()
    worker._accept_task("A", ["a1"])
    worker._accept_task("B", ["b1"])
    worker._accept_task("C", ["c1"])
    assert_eq("active", list(worker._active_task_ids), ["A", "B", "C"])
    worker._pick_next_item(); worker._cleanup_order("A")
    assert_eq("after A cleanup", list(worker._active_task_ids), ["B", "C"])
    worker._accept_task("D", ["d1"])  # fits into the freed slot
    assert_eq("D promoted immediately", list(worker._active_task_ids), ["B", "C", "D"])

    # ── Case 6: idempotent accept (same task_id twice) ──────────────
    print("\n── Case 6: accepting the same task twice is idempotent")
    reset()
    worker._accept_task("X", ["x1", "x2"])
    worker._accept_task("X", ["x3"])  # extra items
    assert_eq("X in active exactly once", list(worker._active_task_ids).count("X"), 1)
    assert_eq("X item queue union", list(worker._item_queues["X"]), ["x1", "x2", "x3"])

    # ── Case 7: max_concurrent is at least 1 ────────────────────────
    print("\n── Case 7: settings.max_concurrent_tasks defaults sane")
    assert_eq("settings.max_concurrent_tasks", settings.max_concurrent_tasks, 4)
    assert_eq("settings.registration_open_user_threshold",
              settings.registration_open_user_threshold, 20)

    print("\n  All parallel-pool pure-logic checks PASSED ✅")


if __name__ == "__main__":
    main()
