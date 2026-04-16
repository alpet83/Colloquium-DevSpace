from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Any

from .config import SecurityPolicy
from .constants import LINE_EDITED
from .errors import EditorError, bad_request


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=2000")
    return conn


class Storage:
    def __init__(self, data_dir: Path, policy: SecurityPolicy):
        self.data_dir = data_dir
        self.policy = policy
        self.registry_path = self.data_dir / "registry.sqlite"
        self.sessions_dir = self.data_dir / "sessions"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._init_registry()

    @staticmethod
    def canonical_path(raw: str) -> Path:
        return Path(raw).expanduser().resolve()

    def ensure_allowed_path(self, path: Path) -> None:
        candidate = path.resolve()
        for root in self.policy.allowed_roots:
            rp = Path(root).resolve()
            if candidate == rp or rp in candidate.parents:
                return
        raise EditorError(
            "security",
            "path_not_allowed",
            f"Path is outside allowed roots: {candidate}",
            hint="Use a path inside allowed_roots.",
            details={"path": str(candidate), "allowed_roots": self.policy.allowed_roots},
        )

    @staticmethod
    def session_id_for(path: Path) -> str:
        # session_id is a compact deterministic identifier for canonical file path.
        # MD5 is used here for token efficiency (shorter id) and not for cryptographic security.
        return hashlib.md5(str(path).encode("utf-8")).hexdigest()

    def _init_registry(self) -> None:
        with _connect(self.registry_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_info (
                    schema_version INTEGER NOT NULL,
                    applied_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions_registry (
                    session_id TEXT PRIMARY KEY,
                    source_path_hash TEXT NOT NULL UNIQUE,
                    canonical_path TEXT NOT NULL,
                    display_path TEXT NOT NULL,
                    session_db_path TEXT NOT NULL UNIQUE,
                    current_revision_number INTEGER NOT NULL CHECK (current_revision_number >= 0),
                    created_at INTEGER NOT NULL,
                    last_write_at INTEGER NOT NULL,
                    last_opened_at INTEGER NOT NULL,
                    profile_id TEXT,
                    status TEXT NOT NULL DEFAULT 'active'
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_registry_last_write_at ON sessions_registry(last_write_at);
                CREATE INDEX IF NOT EXISTS idx_sessions_registry_status ON sessions_registry(status);
                """
            )
            row = conn.execute("SELECT COUNT(*) AS c FROM schema_info").fetchone()
            if row and int(row["c"]) == 0:
                now = int(time.time())
                conn.execute("INSERT INTO schema_info(schema_version, applied_at) VALUES(1, ?)", (now,))

    def cleanup_stale_sessions(self, *, stale_after_days: int = 30) -> dict[str, int]:
        threshold = int(time.time()) - max(1, stale_after_days) * 24 * 60 * 60
        with _connect(self.registry_path) as conn:
            rows = conn.execute(
                """
                SELECT session_id, session_db_path
                FROM sessions_registry
                WHERE last_write_at < ?
                """,
                (threshold,),
            ).fetchall()
            removed = 0
            missing_db = 0
            for row in rows:
                db_path = Path(str(row["session_db_path"]))
                try:
                    db_path.unlink(missing_ok=True)
                    Path(str(db_path) + "-wal").unlink(missing_ok=True)
                    Path(str(db_path) + "-shm").unlink(missing_ok=True)
                    removed += 1
                except Exception:
                    missing_db += 1
                conn.execute("DELETE FROM sessions_registry WHERE session_id=?", (str(row["session_id"]),))
        return {
            "checked": len(rows),
            "removed": removed,
            "missing_db": missing_db,
        }

    def _init_session_db(self, db_path: Path, content: str) -> None:
        lines = content.splitlines()
        if content.endswith("\n"):
            lines.append("")
        with _connect(db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS text_lines (
                    idx INTEGER PRIMARY KEY,
                    text TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS revision_history (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    line_num INTEGER NOT NULL CHECK (line_num >= 1),
                    deleted_idx INTEGER NOT NULL,
                    added_idx INTEGER NOT NULL,
                    flags INTEGER NOT NULL DEFAULT 0,
                    CHECK (deleted_idx >= 0 OR added_idx >= 0)
                );
                CREATE TABLE IF NOT EXISTS current_revision (
                    line_num INTEGER PRIMARY KEY CHECK (line_num >= 1),
                    line_idx INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS previous_revision (
                    line_num INTEGER PRIMARY KEY CHECK (line_num >= 1),
                    line_idx INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS revision_meta (
                    revision INTEGER PRIMARY KEY,
                    parent_revision INTEGER,
                    created_at INTEGER NOT NULL,
                    op TEXT NOT NULL,
                    source TEXT,
                    changed_lines INTEGER NOT NULL CHECK (changed_lines >= 0),
                    bytes_delta INTEGER NOT NULL,
                    checksum_after TEXT,
                    response_mode_used TEXT,
                    response_render_meta TEXT,
                    profile_id TEXT
                );
                CREATE TABLE IF NOT EXISTS revision_snapshots (
                    revision INTEGER PRIMARY KEY,
                    body TEXT NOT NULL
                );
                """
            )
            conn.execute("INSERT OR IGNORE INTO text_lines(idx, text) VALUES(0, '')")
            self._replace_snapshot(conn, "current_revision", lines)
            self._replace_snapshot(conn, "previous_revision", lines)
            now = int(time.time())
            conn.execute(
                """
                INSERT OR REPLACE INTO revision_meta(
                    revision, parent_revision, created_at, op, source, changed_lines, bytes_delta
                ) VALUES(1, NULL, ?, 'open', 'mcp', 0, 0)
                """,
                (now,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO revision_snapshots(revision, body) VALUES(1, ?)",
                ("\n".join(lines),),
            )
            self._set_meta(conn, "current_revision_number", "1")
            self._set_meta(conn, "cursor_line", "1")
            self._set_meta(conn, "cursor_col", "1")
            self._set_meta(conn, "source_mtime_ns", "0")
            self._set_meta(conn, "source_size_bytes", str(len(content.encode("utf-8"))))
            self._set_meta(conn, "source_hash", hashlib.sha256(content.encode("utf-8")).hexdigest())
            self._set_meta(conn, "source_checked_at", str(now))

    @staticmethod
    def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute("INSERT OR REPLACE INTO session_meta(key, value) VALUES(?,?)", (key, value))

    def _line_to_idx(self, conn: sqlite3.Connection, text: str) -> int:
        row = conn.execute("SELECT idx FROM text_lines WHERE text=? LIMIT 1", (text,)).fetchone()
        if row:
            return int(row["idx"])
        cur = conn.execute("INSERT INTO text_lines(text) VALUES(?)", (text,))
        return int(cur.lastrowid)

    def _replace_snapshot(self, conn: sqlite3.Connection, table: str, lines: list[str]) -> None:
        conn.execute(f"DELETE FROM {table}")
        for i, line in enumerate(lines, start=1):
            idx = self._line_to_idx(conn, line)
            conn.execute(f"INSERT INTO {table}(line_num, line_idx) VALUES(?,?)", (i, idx))

    def open_session(self, path_raw: str, *, display_path: str, profile_id: str | None = None) -> dict[str, Any]:
        path = self.canonical_path(path_raw)
        self.ensure_allowed_path(path)
        if not path.exists():
            raise bad_request("path_not_found", f"File does not exist: {path}")
        size = path.stat().st_size
        if size > self.policy.max_file_size_bytes:
            raise EditorError("security", "payload_too_large", "File too large", details={"size": size})
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise EditorError("security", "file_not_text", f"Non-text file: {path}") from exc

        sid = self.session_id_for(path)
        source_hash = sid
        db_path = self.sessions_dir / f"{sid}.sqlite"
        now = int(time.time())
        with _connect(self.registry_path) as conn:
            row = conn.execute(
                "SELECT * FROM sessions_registry WHERE session_id=?",
                (sid,),
            ).fetchone()
            if row is None:
                self._init_session_db(db_path, content)
                markers = self.source_markers(path)
                with _connect(db_path) as sconn:
                    self.update_source_markers(sconn, markers)
                conn.execute(
                    """
                    INSERT INTO sessions_registry(
                        session_id, source_path_hash, canonical_path, display_path, session_db_path,
                        current_revision_number, created_at, last_write_at, last_opened_at, profile_id, status
                    ) VALUES(?,?,?,?,?,?,?,?,?,?, 'active')
                    """,
                    (
                        sid,
                        source_hash,
                        str(path),
                        display_path,
                        str(db_path),
                        1,
                        now,
                        now,
                        now,
                        profile_id,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE sessions_registry SET last_opened_at=?, display_path=? WHERE session_id=?",
                    (now, display_path, sid),
                )
        return self.get_session_info(sid)

    def get_session_info(self, session_id: str) -> dict[str, Any]:
        with _connect(self.registry_path) as conn:
            row = conn.execute("SELECT * FROM sessions_registry WHERE session_id=?", (session_id,)).fetchone()
            if row is None:
                raise bad_request("session_not_found", f"Unknown session_id: {session_id}")
            return dict(row)

    def session_conn(self, session_id: str) -> sqlite3.Connection:
        info = self.get_session_info(session_id)
        return _connect(Path(str(info["session_db_path"])))

    @staticmethod
    def read_snapshot(conn: sqlite3.Connection, table: str) -> list[str]:
        rows = conn.execute(
            f"""
            SELECT r.line_num, t.text
            FROM {table} r
            JOIN text_lines t ON t.idx = r.line_idx
            ORDER BY r.line_num
            """
        ).fetchall()
        return [str(r["text"]) for r in rows]

    @staticmethod
    def current_revision(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT value FROM session_meta WHERE key='current_revision_number'").fetchone()
        return int(row["value"]) if row else 1

    def write_revision(
        self,
        session_id: str,
        op: str,
        old_lines: list[str],
        new_lines: list[str],
        *,
        response_mode: str,
        revision_flags: int = 0,
    ) -> tuple[int, int]:
        info = self.get_session_info(session_id)
        parent = int(info["current_revision_number"])
        current = parent + 1
        with self.session_conn(session_id) as conn:
            self._replace_snapshot(conn, "previous_revision", old_lines)
            self._replace_snapshot(conn, "current_revision", new_lines)
            max_len = max(len(old_lines), len(new_lines))
            for i in range(max_len):
                old = old_lines[i] if i < len(old_lines) else None
                new = new_lines[i] if i < len(new_lines) else None
                if old == new:
                    continue
                deleted_idx = -1 if old is None else self._line_to_idx(conn, old)
                added_idx = -1 if new is None else self._line_to_idx(conn, new)
                conn.execute(
                    """
                    INSERT INTO revision_history(revision, line_num, deleted_idx, added_idx, flags)
                    VALUES(?,?,?,?,?)
                    """,
                    (current, i + 1, deleted_idx, added_idx, LINE_EDITED | revision_flags),
                )
            now = int(time.time())
            conn.execute(
                """
                INSERT OR REPLACE INTO revision_meta(
                    revision, parent_revision, created_at, op, source, changed_lines, bytes_delta, response_mode_used
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    current,
                    parent,
                    now,
                    op,
                    "mcp",
                    abs(len(new_lines) - len(old_lines)),
                    len("\n".join(new_lines)) - len("\n".join(old_lines)),
                    response_mode,
                ),
            )
            conn.execute(
                "INSERT OR REPLACE INTO revision_snapshots(revision, body) VALUES(?, ?)",
                (current, "\n".join(new_lines)),
            )
            self._set_meta(conn, "current_revision_number", str(current))
        with _connect(self.registry_path) as reg:
            reg.execute(
                "UPDATE sessions_registry SET current_revision_number=?, last_write_at=? WHERE session_id=?",
                (current, int(time.time()), session_id),
            )
        return current, parent

    @staticmethod
    def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
        row = conn.execute("SELECT value FROM session_meta WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        return str(row["value"])

    @staticmethod
    def source_markers(path: Path) -> dict[str, str]:
        raw = path.read_bytes()
        stat = path.stat()
        return {
            "source_mtime_ns": str(stat.st_mtime_ns),
            "source_size_bytes": str(stat.st_size),
            "source_hash": hashlib.sha256(raw).hexdigest(),
            "source_checked_at": str(int(time.time())),
        }

    @staticmethod
    def update_source_markers(conn: sqlite3.Connection, markers: dict[str, str]) -> None:
        for key, value in markers.items():
            conn.execute("INSERT OR REPLACE INTO session_meta(key, value) VALUES(?,?)", (key, value))

    @staticmethod
    def snapshot_lines(conn: sqlite3.Connection, revision: int) -> list[str] | None:
        row = conn.execute("SELECT body FROM revision_snapshots WHERE revision=?", (revision,)).fetchone()
        if row is None:
            return None
        body = str(row["body"])
        return body.split("\n")

