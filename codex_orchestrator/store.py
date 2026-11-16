"""Durable local task and interaction records. Never stores endpoint credentials."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from .bounds import json_size, preview_record


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS tasks(
          id TEXT PRIMARY KEY, request_key TEXT, env TEXT NOT NULL, actor TEXT NOT NULL,
          platform TEXT NOT NULL, umo TEXT NOT NULL, cid TEXT NOT NULL, workspace TEXT NOT NULL,
          thread_id TEXT, turn_id TEXT, status TEXT NOT NULL, input TEXT NOT NULL,
          result TEXT, error TEXT, created REAL NOT NULL, updated REAL NOT NULL,
          mode TEXT NOT NULL DEFAULT 'new', parent_thread_id TEXT,
          thread_start_phase TEXT NOT NULL DEFAULT 'not_started',
          turn_start_phase TEXT NOT NULL DEFAULT 'not_started');
        CREATE UNIQUE INDEX IF NOT EXISTS task_request_dedupe
          ON tasks(env,actor,request_key) WHERE request_key IS NOT NULL;
        CREATE TABLE IF NOT EXISTS interactions(
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL, generation INTEGER NOT NULL,
          method TEXT NOT NULL, params TEXT NOT NULL, state TEXT NOT NULL,
          response TEXT, request_id TEXT,
          FOREIGN KEY(task_id) REFERENCES tasks(id));
        CREATE TABLE IF NOT EXISTS outbox(
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL, payload TEXT NOT NULL,
          state TEXT NOT NULL, created REAL NOT NULL, FOREIGN KEY(task_id) REFERENCES tasks(id));
        CREATE TABLE IF NOT EXISTS events(
          seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, payload TEXT NOT NULL,
          created REAL NOT NULL, FOREIGN KEY(task_id) REFERENCES tasks(id));
        """)
        task_columns = {row[1] for row in self.db.execute("PRAGMA table_info(tasks)")}
        for name, definition in {
            "mode": "TEXT NOT NULL DEFAULT 'new'",
            "parent_thread_id": "TEXT",
            "thread_start_phase": "TEXT NOT NULL DEFAULT 'not_started'",
            "turn_start_phase": "TEXT NOT NULL DEFAULT 'not_started'",
        }.items():
            if name not in task_columns:
                self.db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(interactions)")}
        if "response" not in columns:
            self.db.execute("ALTER TABLE interactions ADD COLUMN response TEXT")
        if "request_id" not in columns:
            self.db.execute("ALTER TABLE interactions ADD COLUMN request_id TEXT")

    def create_task(
        self,
        *,
        env: str,
        actor: str,
        platform: str,
        umo: str,
        cid: str,
        workspace: str,
        text: str,
        request_key: str | None,
        mode: str = "new",
        parent_thread_id: str | None = None,
    ) -> dict[str, Any]:
        if request_key:
            old = self.db.execute(
                "SELECT * FROM tasks WHERE request_key=? AND env=? AND actor=?",
                (request_key, env, actor),
            ).fetchone()
            if old:
                return dict(old)
        now, task_id = time.time(), str(uuid.uuid4())
        self.db.execute(
            """INSERT INTO tasks
            (id,request_key,env,actor,platform,umo,cid,workspace,thread_id,turn_id,status,input,result,error,created,updated,mode,parent_thread_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                task_id,
                request_key,
                env,
                actor,
                platform,
                umo,
                cid,
                workspace,
                None,
                None,
                "submitted",
                text,
                None,
                None,
                now,
                now,
                mode,
                parent_thread_id,
            ),
        )
        return self.task(task_id)

    def task(self, task_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def tasks_for(self, env: str, actor: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM tasks WHERE env=? AND actor=? ORDER BY created DESC", (env, actor)
            )
        ]

    def update(self, task_id: str, **values: Any) -> None:
        values["updated"] = time.time()
        keys = list(values)
        self.db.execute(
            "UPDATE tasks SET " + ",".join(f"{k}=?" for k in keys) + " WHERE id=?",
            [
                json.dumps(v) if k == "result" and not isinstance(v, str) else v
                for k, v in values.items()
            ]
            + [task_id],
        )

    def event(self, task_id: str, event: Any) -> None:
        self.db.execute(
            "INSERT INTO events(task_id,payload,created) VALUES(?,?,?)",
            (task_id, json.dumps(event), time.time()),
        )

    def events_page(
        self,
        task_id: str,
        *,
        cursor: int = 0,
        limit: int = 20,
        max_event_bytes: int = 4096,
        max_page_bytes: int = 65536,
    ) -> dict[str, Any]:
        """Return a bounded page. The caller must authorize the task before querying."""
        rows = self.db.execute(
            "SELECT seq,payload FROM events WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?",
            (task_id, cursor, limit + 1),
        ).fetchall()
        items: list[dict[str, Any]] = []
        page: dict[str, Any] = {"items": items, "nextCursor": cursor, "hasMore": False}
        has_more = len(rows) > limit
        for row in rows[:limit]:
            raw = row["payload"]
            entry: dict[str, Any] = {
                "cursor": int(row["seq"]),
                "event": json.loads(raw),
                "truncated": False,
            }
            if json_size(entry) > max_event_bytes:
                entry = preview_record(
                    raw,
                    {
                        "cursor": int(row["seq"]),
                        "truncated": True,
                        "originalBytes": len(raw.encode("utf-8")),
                    },
                    max_event_bytes,
                )
            candidate = {"items": [*items, entry], "nextCursor": entry["cursor"], "hasMore": False}
            if json_size(candidate) > max_page_bytes:
                if not items:
                    raise ValueError("page byte budget is too small for one bounded event")
                has_more = True
                break
            items.append(entry)
            page["nextCursor"] = entry["cursor"]
        page["hasMore"] = has_more
        return page

    def add_outbox(self, task_id: str, payload: Any) -> None:
        self.db.execute(
            "INSERT INTO outbox VALUES(?,?,?,?,?)",
            (str(uuid.uuid4()), task_id, json.dumps(payload), "pending", time.time()),
        )

    def finish(self, task_id: str, status: str, result: Any, error: Any = None) -> None:
        payload = json.dumps(result, default=str)
        error_text = (
            json.dumps(error, ensure_ascii=False, default=str) if error is not None else None
        )
        now = time.time()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                "UPDATE tasks SET status=?,result=?,error=?,updated=? WHERE id=?",
                (status, payload, error_text, now, task_id),
            )
            self.db.execute(
                "INSERT INTO outbox(id,task_id,payload,state,created) VALUES(?,?,?,?,?)",
                (str(uuid.uuid4()), task_id, payload, "pending", now),
            )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self.db.close()
