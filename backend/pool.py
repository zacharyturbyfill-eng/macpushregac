"""Kho tài khoản NoteGPT (SQLite) cho dịch vụ notegpt-voice.

Một dòng = một tài khoản. Cookie giữ ở <cookie_dir>/<email đã làm sạch>.json (đúng chỗ lõi tool cũ tìm),
kho chỉ giữ metadata. Trạng thái:
  ready      còn quota, cookie còn hạn (hoặc có mật khẩu để đăng nhập lại)
  exhausted  quota = 0
  dead       đăng nhập hỏng / bị khoá
"""
from __future__ import annotations

import csv
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  email        TEXT PRIMARY KEY,
  password     TEXT NOT NULL,
  name         TEXT DEFAULT '',
  status       TEXT DEFAULT 'ready',
  quota_left   INTEGER DEFAULT 15,
  quota_text   TEXT DEFAULT '',
  cookie_file  TEXT DEFAULT '',
  created_at   REAL,
  last_used_at REAL,
  last_scan_at REAL,
  note         TEXT DEFAULT ''
);
"""


class AccountPool:
    def __init__(self, db_path: str | Path, cookie_dir: str | Path) -> None:
        self.db_path = Path(db_path)
        self.cookie_dir = Path(cookie_dir)
        self.cookie_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---------- đọc ----------
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) n, COALESCE(SUM(quota_left),0) q FROM accounts GROUP BY status"
            ).fetchall()
        out = {"ready": 0, "exhausted": 0, "dead": 0, "total": 0, "quota_total": 0}
        for r in rows:
            out[r["status"]] = r["n"]
            out["total"] += r["n"]
            if r["status"] == "ready":
                out["quota_total"] += r["q"]
        return out

    def ready_accounts(self, limit: int = 0, min_quota: int = 1) -> List[Dict[str, Any]]:
        """Trả về dict đúng khoá lõi tool cũ mong đợi: email / password / name / cookies (chuỗi JSON hoặc rỗng)."""
        sql = "SELECT * FROM accounts WHERE status='ready' AND quota_left>=? ORDER BY last_used_at IS NOT NULL, last_used_at, created_at"
        if limit > 0:
            sql += f" LIMIT {int(limit)}"
        with self._lock:
            rows = self._conn.execute(sql, (min_quota,)).fetchall()
        return [self._to_account(r) for r in rows]

    def get(self, email: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            r = self._conn.execute("SELECT * FROM accounts WHERE email=?", (email,)).fetchone()
        return self._to_account(r) if r else None

    def _to_account(self, r: sqlite3.Row) -> Dict[str, Any]:
        cookies = ""
        cf = r["cookie_file"] or ""
        if cf and Path(cf).exists():
            try:
                cookies = Path(cf).read_text(encoding="utf-8")
            except OSError:
                cookies = ""
        return {
            "email": r["email"], "password": r["password"], "name": r["name"] or "",
            "cookies": cookies, "quota_left": int(r["quota_left"] or 0), "status": r["status"],
        }

    # ---------- ghi ----------
    def upsert(self, email: str, password: str, *, name: str = "", quota_left: int = 15,
               quota_text: str = "", cookie_file: str = "", status: str = "ready", note: str = "") -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO accounts(email,password,name,status,quota_left,quota_text,cookie_file,created_at,note)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(email) DO UPDATE SET password=excluded.password, name=excluded.name,
                     status=excluded.status, quota_left=excluded.quota_left, quota_text=excluded.quota_text,
                     cookie_file=CASE WHEN excluded.cookie_file<>'' THEN excluded.cookie_file ELSE accounts.cookie_file END,
                     note=excluded.note""",
                (email, password, name, status, int(quota_left), quota_text, cookie_file, now, note),
            )
            self._conn.commit()

    def set_quota(self, email: str, quota_left: int, quota_text: str = "") -> None:
        status = "ready" if quota_left > 0 else "exhausted"
        with self._lock:
            self._conn.execute(
                "UPDATE accounts SET quota_left=?, quota_text=?, status=CASE WHEN status='dead' THEN 'dead' ELSE ? END, last_scan_at=? WHERE email=?",
                (int(quota_left), quota_text, status, time.time(), email),
            )
            self._conn.commit()

    def spend(self, email: str, chunks: int = 1) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE accounts SET quota_left=MAX(0, quota_left-?), last_used_at=?, status=CASE WHEN quota_left-?<=0 THEN 'exhausted' ELSE status END WHERE email=?",
                (int(chunks), time.time(), int(chunks), email),
            )
            self._conn.commit()

    def mark(self, email: str, status: str, note: str = "") -> None:
        with self._lock:
            self._conn.execute("UPDATE accounts SET status=?, note=? WHERE email=?", (status, note, email))
            self._conn.commit()

    # ---------- nhập ----------
    def import_tsv(self, path: str | Path, src_cookie_dir: str | Path | None = None) -> int:
        """Nhập file TSV của tool cũ (registered_accounts*.txt). Cookie: cột cookies hoặc file trong src_cookie_dir."""
        from notegpt_quota_tool.notegpt import safe_filename
        n = 0
        with Path(path).open("r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f, delimiter="\t"):
                email = (row.get("email") or "").strip()
                pwd = (row.get("password") or "").strip()
                if not email or not pwd:
                    continue
                cookie_file = self.cookie_dir / f"{safe_filename(email)}.json"
                if not cookie_file.exists():
                    text = (row.get("cookies") or "").strip()
                    if not text and src_cookie_dir:
                        src = Path(src_cookie_dir) / f"{safe_filename(email)}.json"
                        if src.exists():
                            text = src.read_text(encoding="utf-8")
                    if text.startswith("["):
                        cookie_file.write_text(text, encoding="utf-8")
                try:
                    quota = int(row.get("quota_left") or 15)
                except ValueError:
                    quota = 15
                status = "ready" if quota > 0 else "exhausted"
                self.upsert(email, pwd, name=row.get("name") or "", quota_left=quota,
                            quota_text=row.get("quota_text") or "",
                            cookie_file=str(cookie_file) if cookie_file.exists() else "", status=status)
                n += 1
        return n
