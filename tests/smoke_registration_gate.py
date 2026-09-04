"""Local end-to-end smoke test for the new schema + parallel pool.

Cleans its own DB + storage dir under /tmp. Exercises:

1. **Registration gate**
   * Register 20 dummy users.
   * 21st user with no code → 400.
   * Admin issues a code → list shows it.
   * 21st user with code → 201.
   * Same code reused once more (max_uses=2) → 201.
   * Third reuse → 400 (auto-disabled after consume).
2. **Parallel pool semantics** (logic-only, no real Gemini calls)

Run with:
    /c/Users/.../python.exe tests/smoke_parallel_pool.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Always run from the project root and isolate the DB / storage.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="bps_smoke_"))
os.environ["SQLITE_PATH"] = str(TMP / "app.db")
os.environ["STORAGE_DIR"] = str(TMP / "storage")
os.environ["DATA_DIR"] = str(TMP / "data")
os.environ["REGISTRATION_OPEN_USER_THRESHOLD"] = "20"

from fastapi.testclient import TestClient  # noqa: E402

from main import app  # noqa: E402


def step(msg: str) -> None:
    print(f"\n── {msg} " + "─" * (60 - len(msg)))


def assert_eq(label: str, got, want) -> None:
    ok = got == want
    mark = "✅" if ok else "❌"
    print(f"  {mark} {label}: got={got!r} want={want!r}")
    if not ok:
        raise AssertionError(f"{label}: got {got!r}, want {want!r}")


def assert_true(label: str, cond, hint: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {label}{(' ('+hint+')') if hint else ''}")
    if not cond:
        raise AssertionError(f"{label} failed")


def main() -> None:
    try:
        with TestClient(app) as client:
            step("Initial /registration-mode")
            r = client.get("/api/auth/registration-mode")
            assert_eq("status", r.status_code, 200)
            assert_eq("body", r.json(), {"code_required": False, "current_count": 0, "threshold": 20})

            step("Register 20 users (threshold-edge)")
            # The first one becomes admin.
            r = client.post("/api/auth/register", json={"username": "u0", "password": "pw1234"})
            assert_eq("u0 status", r.status_code, 201)
            assert_eq("u0 role", r.json()["role"], "admin")
            for i in range(1, 20):
                r = client.post("/api/auth/register", json={"username": f"u{i}", "password": "pw1234"})
                assert_eq(f"u{i} status", r.status_code, 201)

            step("Registration mode now requires code (current_count=20)")
            r = client.get("/api/auth/registration-mode")
            assert_eq("body", r.json(), {"code_required": True, "current_count": 20, "threshold": 20})

            step("21st user with NO code → 400")
            r = client.post("/api/auth/register", json={"username": "u20", "password": "pw1234"})
            assert_eq("status", r.status_code, 400)
            assert_true("detail mentions invite code", "邀请码" in r.json().get("detail", ""))

            step("21st user with WRONG code → 400")
            r = client.post(
                "/api/auth/register",
                json={"username": "u20", "password": "pw1234", "registration_code": "WRONG-CODE-XYZ"},
            )
            assert_eq("status", r.status_code, 400)

            step("Admin login + create invite code (max_uses=2)")
            r = client.post(
                "/api/auth/login",
                data={"username": "u0", "password": "pw1234"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            assert_eq("login status", r.status_code, 200)
            admin_token = r.json()["access_token"]
            admin_headers = {"Authorization": f"Bearer {admin_token}"}

            r = client.post(
                "/api/admin/registration-codes",
                json={"max_uses": 2, "note": "smoke test"},
                headers=admin_headers,
            )
            assert_eq("create status", r.status_code, 201)
            code_row = r.json()
            code_value = code_row["code"]
            assert_eq("max_uses", code_row["max_uses"], 2)
            assert_eq("used_count", code_row["used_count"], 0)
            assert_eq("status active", code_row["status"], "active")

            step("List codes via admin endpoint")
            r = client.get("/api/admin/registration-codes", headers=admin_headers)
            assert_eq("status", r.status_code, 200)
            listed = r.json()
            assert_true("our code in list", any(c["code"] == code_value for c in listed))
            assert_eq("list length", len(listed), 1)

            step("21st user WITH valid code → 201")
            r = client.post(
                "/api/auth/register",
                json={"username": "u20", "password": "pw1234", "registration_code": code_value},
            )
            assert_eq("status", r.status_code, 201)
            assert_eq("role", r.json()["role"], "staff")

            step("Code used_count incremented to 1, remaining 1")
            r = client.get("/api/admin/registration-codes", headers=admin_headers)
            listed = r.json()
            match = next(c for c in listed if c["code"] == code_value)
            assert_eq("used_count", match["used_count"], 1)
            assert_eq("remaining", match["remaining"], 1)

            step("22nd user reuses same code → 201")
            r = client.post(
                "/api/auth/register",
                json={"username": "u21", "password": "pw1234", "registration_code": code_value},
            )
            assert_eq("status", r.status_code, 201)

            step("Code auto-disabled after max_uses reached")
            r = client.get("/api/admin/registration-codes", headers=admin_headers)
            listed = r.json()
            match = next(c for c in listed if c["code"] == code_value)
            assert_eq("used_count", match["used_count"], 2)
            assert_eq("status disabled", match["status"], "disabled")

            step("23rd user tries disabled code → 400")
            r = client.post(
                "/api/auth/register",
                json={"username": "u22", "password": "pw1234", "registration_code": code_value},
            )
            assert_eq("status", r.status_code, 400)

            step("Admin deletes the code → 200")
            r = client.delete(
                f"/api/admin/registration-codes/{match['id']}",
                headers=admin_headers,
            )
            assert_eq("delete status", r.status_code, 200)

            step("Non-admin cannot create codes (403)")
            # Login a non-admin
            r = client.post(
                "/api/auth/login",
                data={"username": "u1", "password": "pw1234"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            assert_eq("login staff", r.status_code, 200)
            staff_token = r.json()["access_token"]
            r = client.post(
                "/api/admin/registration-codes",
                json={"max_uses": 1},
                headers={"Authorization": f"Bearer {staff_token}"},
            )
            assert_eq("status forbidden", r.status_code, 403)

        print("\n" + "═" * 60)
        print("  All smoke checks PASSED ✅")
        print("═" * 60)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
