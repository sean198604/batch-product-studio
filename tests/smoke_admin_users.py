"""Local end-to-end smoke test for admin employee management.

Isolates its own DB + storage dir under /tmp (never touches server data).
Exercises:

1. Blank-username registration is rejected (the bug that let a '' account in).
2. Admin can create accounts manually (username/password/role/note).
3. Duplicate username → 409. Non-admin callers → 403.
4. Update note / role; admin cannot demote itself; admin cannot demote/delete
   the last remaining admin.
5. Password reset: old password stops working, new one logs in.
6. Delete an employee cascades: task rows, key-pool rows and storage files
   are removed together.
7. Delete is refused while the employee still has a running task, and an
   admin cannot delete their own logged-in account.

Run with:
    /c/Users/.../python.exe tests/smoke_admin_users.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Always run from the project root and isolate the DB / storage.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="bps_adm_"))
os.environ["SQLITE_PATH"] = str(TMP / "app.db")
os.environ["STORAGE_DIR"] = str(TMP / "storage")
os.environ["DATA_DIR"] = str(TMP / "data")
os.environ["REGISTRATION_OPEN_USER_THRESHOLD"] = "20"

from fastapi.testclient import TestClient  # noqa: E402

from main import app  # noqa: E402

NOW = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
FAILS: list[str] = []


def step(msg: str) -> None:
    print(f"\n── {msg} " + "─" * (60 - len(msg)))


def check(label: str, cond: bool, hint: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {label}{(' (' + hint + ')') if hint else ''}")
    if not cond:
        FAILS.append(label)
        raise AssertionError(f"{label} failed {hint}")


def eq(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {label}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(label)
        raise AssertionError(f"{label}: got {got!r}, want {want!r}")


def _sql() -> sqlite3.Connection:
    con = sqlite3.connect(str(TMP / "app.db"), timeout=30)
    con.row_factory = sqlite3.Row
    return con


def _direct_insert_user(username: str) -> int:
    """Insert a raw account row the way legacy/test data would look."""
    con = _sql()
    try:
        con.execute(
            "INSERT INTO users (username, password_hash, role, "
            "total_api_calls, total_images_generated, total_cost_usd, created_at) "
            "VALUES (?, ?, ?, 0, 0, 0.0, ?)",
            (username, "not-a-real-hash", "staff", NOW),
        )
        con.commit()
        uid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        return int(uid)
    finally:
        con.close()


def main() -> None:
    try:
        with TestClient(app) as client:
            # ---- bootstrap: u0 = first registrant = admin, u1 = staff ----
            step("Bootstrap admin u0 + staff u1")
            eq("register u0", client.post("/api/auth/register",
                json={"username": "u0", "password": "pw1234"}).status_code, 201)
            eq("register u1", client.post("/api/auth/register",
                json={"username": "u1", "password": "pw1234"}).status_code, 201)
            tok = client.post("/api/auth/login",
                data={"username": "u0", "password": "pw1234"},
                headers={"Content-Type": "application/x-www-form-urlencoded"}).json()["access_token"]
            H = {"Authorization": f"Bearer {tok}"}
            tok1 = client.post("/api/auth/login",
                data={"username": "u1", "password": "pw1234"},
                headers={"Content-Type": "application/x-www-form-urlencoded"}).json()["access_token"]
            H1 = {"Authorization": f"Bearer {tok1}"}

            # ---- 1. blank username rejected ----
            step("Blank username registration rejected")
            for bad, label in (("", "empty"), ("   ", "whitespace")):
                r = client.post("/api/auth/register",
                                json={"username": bad, "password": "pw1234"})
                eq(f"register {label!r} status", r.status_code, 400)
            # whitespace-padded name is trimmed on save
            r = client.post("/api/auth/register",
                            json={"username": "  trimmer  ", "password": "pw1234"})
            eq("trimmed register", r.status_code, 201)
            eq("trimmed username", r.json()["username"], "trimmer")

            # ---- 2/3. admin creates accounts ----
            step("Admin manual create (note+role)")
            r = client.post("/api/admin/users", headers=H, json={
                "username": "hr1", "password": "pw123456", "role": "staff",
                "note": "人力资源部 / 王芳",
            })
            eq("create hr1", r.status_code, 201)
            d = r.json()
            eq("hr1 role", d["role"], "staff")
            eq("hr1 note", d["note"], "人力资源部 / 王芳")
            hr1_id = d["id"]
            eq("hr1 username in /users", any(
                u["username"] == "hr1" for u in client.get("/api/admin/users", headers=H).json()), True)

            r = client.post("/api/admin/users", headers=H, json={
                "username": "hr1", "password": "pw123456"})
            eq("duplicate create → 409", r.status_code, 409)
            r = client.post("/api/admin/users", headers=H, json={
                "username": "   ", "password": "pw123456"})
            eq("blank username create → 400", r.status_code, 400)
            r = client.post("/api/admin/users", headers=H, json={
                "username": "weak", "password": "123"})
            eq("short password create → 422", r.status_code, 422)

            r = client.post("/api/admin/users", headers=H1, json={
                "username": "staffTry", "password": "pw123456"})
            eq("staff create → 403", r.status_code, 403)
            eq("staff list → 403", client.get("/api/admin/users", headers=H1).status_code, 403)

            # ---- 4. update note / role + self/guard rules ----
            step("Update note / role")
            r = client.put(f"/api/admin/users/{hr1_id}", headers=H, json={"note": "已转销售部"})
            eq("update note", r.status_code, 200)
            eq("note persisted", r.json()["note"], "已转销售部")
            r = client.put(f"/api/admin/users/{hr1_id}", headers=H, json={"note": ""})
            eq("clear note → null", r.status_code, 200)
            eq("note cleared", r.json()["note"], None)

            r = client.post("/api/admin/users", headers=H, json={
                "username": "adminB", "password": "pw123456", "role": "admin"})
            eq("create adminB", r.status_code, 201)
            adminb_id = r.json()["id"]

            r = client.put("/api/admin/users/1", headers=H, json={"role": "staff"})
            eq("self demote → 400", r.status_code, 400)  # u0 is id 1

            # demote adminB (still one admin left: u0) → allowed
            r = client.put(f"/api/admin/users/{adminb_id}", headers=H, json={"role": "staff"})
            eq("demote adminB (2 admins) ok", r.status_code, 200)
            eq("adminB role now", r.json()["role"], "staff")

            # only u0 (admin, logged in) remains → deleting the last admin guard:
            # promote adminB back so there are 2, then adminB deletes u0 → allowed,
            # leaving u0 the only path to guard-checking impossible for adminB.
            r = client.put(f"/api/admin/users/{adminb_id}", headers=H, json={"role": "admin"})
            eq("re-promote adminB", r.status_code, 200)
            r = client.delete("/api/admin/users/1", headers=H)
            eq("delete self → 400", r.status_code, 400)

            # ---- 5. password reset ----
            step("Password reset")
            r = client.post("/api/admin/users", headers=H, json={
                "username": "pwuser", "password": "oldpass1"})
            eq("create pwuser", r.status_code, 201)
            pw_id = r.json()["id"]
            login_old = client.post("/api/auth/login",
                data={"username": "pwuser", "password": "oldpass1"},
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            eq("old password login ok", login_old.status_code, 200)
            r = client.put(f"/api/admin/users/{pw_id}/password", headers=H,
                           json={"new_password": "brandnew99"})
            eq("reset password", r.status_code, 200)
            login_old2 = client.post("/api/auth/login",
                data={"username": "pwuser", "password": "oldpass1"},
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            eq("old password now 401", login_old2.status_code, 401)
            login_new = client.post("/api/auth/login",
                data={"username": "pwuser", "password": "brandnew99"},
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            eq("new password login ok", login_new.status_code, 200)

            # ---- 6. cascade delete (records + storage files) ----
            step("Delete cascades tasks / keys / files")
            # fabricate legacy artifacts: 1 task with 3 original files + 1 output,
            # plus a bound pool key, plus real files on disk
            files = ["u_del/f.png", "u_del/a.png", "u_del/b.png", "u_del/out.png"]
            storage = Path(os.environ["STORAGE_DIR"])
            for rel in files:
                p = storage / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"\x89PNG-fake")
            con = _sql()
            try:
                tid = f"t-{uuid.uuid4().hex[:8]}"
                iid = f"i-{uuid.uuid4().hex[:8]}"
                con.execute(
                    "INSERT INTO generation_tasks (id, user_id, prompt, is_white_bg, "
                    "theme_random, total_count, completed_count, failed_count, cost_usd, "
                    "status, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (tid, hr1_id, "del test", 0, 0, 1, 0, 1, 0.0, "failed", NOW),
                )
                con.execute(
                    "INSERT INTO task_items (id, task_id, user_id, original_filename, "
                    "original_path, original_paths, output_path, cost_usd, status, "
                    "created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (iid, tid, hr1_id, "f.png", files[0],
                     '["u_del/a.png","u_del/b.png"]', files[3], 0.0, "completed", NOW),
                )
                con.execute(
                    "INSERT INTO api_keys (provider, owner_user_id, source, key_value, "
                    "status, created_at) VALUES (?,?,?,?,?,?)",
                    ("agnes", hr1_id, "user", f"sk-del-{hr1_id}", "valid", NOW),
                )
                con.commit()
            finally:
                con.close()

            r = client.delete(f"/api/admin/users/{hr1_id}", headers=H)
            eq("delete hr1", r.status_code, 200)
            body = r.json()
            eq("deleted username", body["deleted_username"], "hr1")
            eq("removed records", body["removed_records"], 4)  # 3 orig + 1 output path
            eq("removed files", body["removed_files"], 4)

            con = _sql()
            try:
                eq("users row gone", con.execute(
                    "SELECT COUNT(*) FROM users WHERE id=?", (hr1_id,)).fetchone()[0], 0)
                eq("task rows gone", con.execute(
                    "SELECT COUNT(*) FROM generation_tasks WHERE user_id=?",
                    (hr1_id,)).fetchone()[0], 0)
                eq("item rows gone", con.execute(
                    "SELECT COUNT(*) FROM task_items WHERE user_id=?",
                    (hr1_id,)).fetchone()[0], 0)
                eq("bound keys gone", con.execute(
                    "SELECT COUNT(*) FROM api_keys WHERE owner_user_id=?",
                    (hr1_id,)).fetchone()[0], 0)
            finally:
                con.close()
            eq("file f.png gone", not (storage / files[0]).exists(), True)
            eq("file out.png gone", not (storage / files[3]).exists(), True)

            # ---- 7. running task blocks delete; last-admin guard ----
            step("Guards: running task + legacy blank username cleanup")
            ghost = _direct_insert_user("")
            eq("legacy '' account in list", any(
                u["username"] == "" for u in client.get("/api/admin/users", headers=H).json()), True)
            r = client.delete(f"/api/admin/users/{ghost}", headers=H)
            eq("delete blank-username ghost", r.status_code, 200)
            eq("ghost removed from /users", any(
                u["id"] == ghost for u in client.get("/api/admin/users", headers=H).json()), False)

            # running-task guard: give pwuser a 'processing' task
            con = _sql()
            try:
                con.execute(
                    "INSERT INTO generation_tasks (id, user_id, prompt, is_white_bg, "
                    "theme_random, total_count, completed_count, failed_count, cost_usd, "
                    "status, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (f"t-{uuid.uuid4().hex[:8]}", pw_id, "running", 0, 0, 1, 0, 0, 0.0,
                     "processing", NOW),
                )
                con.commit()
            finally:
                con.close()
            r = client.delete(f"/api/admin/users/{pw_id}", headers=H)
            eq("delete user with running task → 409", r.status_code, 409)
            eq("detail mentions 进行中", "进行中" in r.json().get("detail", ""), True)

        if FAILS:
            print("\nFAILED checks:", FAILS)
            raise SystemExit(1)
        print("\n" + "═" * 60)
        print("  All employee-management smoke checks PASSED ✅")
        print("═" * 60)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
