"""SQLite persistence for Hava: invoice jobs, questions asked in the group, and
small key/value state (the Telegram update offset).

Everything Hava needs to resume after a restart lives here. Questions can wait
hours for an answer, so nothing important is kept only in memory.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Job statuses, in pipeline order.
NEW = "new"                          # PDF received, supplier not yet known
AWAITING_SUPPLIER = "awaiting_supplier"  # asked the group which supplier it is
ONBOARDING = "onboarding"            # new supplier: gathering answers for the rulebook
WRITING_RULES = "writing_rules"      # answers in; writing the rulebook
EXTRACTING = "extracting"            # reading the invoice with the rulebook
AWAITING_DOUBTS = "awaiting_doubts"  # asked the group about the extracted invoice
CORRECTING = "correcting"            # applying the group's answers to the extraction
APPLYING = "applying"                # writing products + draft invoice to Moloni
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL = {DONE, FAILED, CANCELLED}
# Statuses a worker can safely (re)run after a restart.
RUNNABLE = {NEW, WRITING_RULES, EXTRACTING, CORRECTING}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    pdf_message_id INTEGER NOT NULL,
    filename TEXT NOT NULL,
    pdf_path TEXT NOT NULL,
    sender TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    supplier_slug TEXT,
    context_json TEXT NOT NULL DEFAULT '{}',
    extracted_json TEXT,
    document_id INTEGER,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    options_json TEXT NOT NULL DEFAULT '[]',
    payload_json TEXT NOT NULL DEFAULT '{}',
    chat_id INTEGER NOT NULL,
    tg_message_id INTEGER,
    status TEXT NOT NULL DEFAULT 'open',
    answer_option INTEGER,
    answer_text TEXT,
    answered_by TEXT,
    created_at REAL NOT NULL,
    answered_at REAL
);
CREATE INDEX IF NOT EXISTS questions_job ON questions(job_id, status);
CREATE INDEX IF NOT EXISTS questions_msg ON questions(chat_id, tg_message_id);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class Job:
    id: int
    chat_id: int
    pdf_message_id: int
    filename: str
    pdf_path: str
    sender: str
    status: str
    supplier_slug: str | None
    context: dict[str, Any]
    extracted_json: str | None
    document_id: int | None
    error: str | None

    def pdf_bytes(self) -> bytes:
        return Path(self.pdf_path).read_bytes()


@dataclass
class Question:
    id: int
    job_id: int
    kind: str
    text: str
    options: list[dict[str, Any]]
    payload: dict[str, Any]
    chat_id: int
    tg_message_id: int | None
    status: str
    answer_option: int | None
    answer_text: str | None
    answered_by: str | None

    @property
    def answer_label(self) -> str:
        """What was answered, as text: the button label or the typed reply."""
        if self.answer_option is not None and 0 <= self.answer_option < len(self.options):
            return str(self.options[self.answer_option].get("label", ""))
        return self.answer_text or ""

    @property
    def answer_value(self) -> Any:
        """Machine value of a button answer (None for a typed answer)."""
        if self.answer_option is not None and 0 <= self.answer_option < len(self.options):
            return self.options[self.answer_option].get("value")
        return None


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)

    # ── jobs ──────────────────────────────────────────────────────────────────

    def create_job(
        self, *, chat_id: int, pdf_message_id: int, filename: str, pdf_path: str, sender: str
    ) -> int:
        now = time.time()
        cur = self._db.execute(
            "INSERT INTO jobs (chat_id, pdf_message_id, filename, pdf_path, sender, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, pdf_message_id, filename, pdf_path, sender, NEW, now, now),
        )
        return int(cur.lastrowid)

    def set_pdf_path(self, job_id: int, pdf_path: str) -> None:
        self._db.execute("UPDATE jobs SET pdf_path = ? WHERE id = ?", (pdf_path, job_id))

    def get_job(self, job_id: int) -> Job | None:
        row = self._db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._job(row) if row else None

    def update_job(self, job_id: int, **fields: Any) -> None:
        if "context" in fields:
            fields["context_json"] = json.dumps(fields.pop("context"), ensure_ascii=False)
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._db.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))

    def jobs_with_status(self, statuses: set[str]) -> list[Job]:
        marks = ",".join("?" for _ in statuses)
        rows = self._db.execute(
            f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY id", tuple(statuses)
        ).fetchall()
        return [self._job(r) for r in rows]

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            chat_id=row["chat_id"],
            pdf_message_id=row["pdf_message_id"],
            filename=row["filename"],
            pdf_path=row["pdf_path"],
            sender=row["sender"],
            status=row["status"],
            supplier_slug=row["supplier_slug"],
            context=json.loads(row["context_json"] or "{}"),
            extracted_json=row["extracted_json"],
            document_id=row["document_id"],
            error=row["error"],
        )

    # ── questions ─────────────────────────────────────────────────────────────

    def add_question(
        self,
        *,
        job_id: int,
        chat_id: int,
        kind: str,
        text: str,
        options: list[dict[str, Any]],
        payload: dict[str, Any] | None = None,
    ) -> int:
        cur = self._db.execute(
            "INSERT INTO questions (job_id, kind, text, options_json, payload_json, chat_id, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                kind,
                text,
                json.dumps(options, ensure_ascii=False),
                json.dumps(payload or {}, ensure_ascii=False),
                chat_id,
                time.time(),
            ),
        )
        return int(cur.lastrowid)

    def set_question_message(self, question_id: int, tg_message_id: int) -> None:
        self._db.execute(
            "UPDATE questions SET tg_message_id = ? WHERE id = ?", (tg_message_id, question_id)
        )

    def get_question(self, question_id: int) -> Question | None:
        row = self._db.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
        return self._question(row) if row else None

    def question_for_message(self, chat_id: int, tg_message_id: int) -> Question | None:
        row = self._db.execute(
            "SELECT * FROM questions WHERE chat_id = ? AND tg_message_id = ?",
            (chat_id, tg_message_id),
        ).fetchone()
        return self._question(row) if row else None

    def questions(self, job_id: int, *, kind_prefix: str = "", status: str | None = None) -> list[Question]:
        sql = "SELECT * FROM questions WHERE job_id = ? AND kind LIKE ?"
        args: list[Any] = [job_id, f"{kind_prefix}%"]
        if status:
            sql += " AND status = ?"
            args.append(status)
        rows = self._db.execute(sql + " ORDER BY id", args).fetchall()
        return [self._question(r) for r in rows]

    def open_count(self, job_id: int) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) FROM questions WHERE job_id = ? AND status = 'open'", (job_id,)
        ).fetchone()
        return int(row[0])

    def answer(
        self, question_id: int, *, option: int | None = None, text: str | None = None, by: str = ""
    ) -> None:
        self._db.execute(
            "UPDATE questions SET status = 'answered', answer_option = ?, answer_text = ?, "
            "answered_by = ?, answered_at = ? WHERE id = ? AND status = 'open'",
            (option, text, by, time.time(), question_id),
        )

    def cancel_open_questions(self, job_id: int) -> list[Question]:
        open_qs = self.questions(job_id, status="open")
        self._db.execute(
            "UPDATE questions SET status = 'cancelled' WHERE job_id = ? AND status = 'open'",
            (job_id,),
        )
        return open_qs

    @staticmethod
    def _question(row: sqlite3.Row) -> Question:
        return Question(
            id=row["id"],
            job_id=row["job_id"],
            kind=row["kind"],
            text=row["text"],
            options=json.loads(row["options_json"] or "[]"),
            payload=json.loads(row["payload_json"] or "{}"),
            chat_id=row["chat_id"],
            tg_message_id=row["tg_message_id"],
            status=row["status"],
            answer_option=row["answer_option"],
            answer_text=row["answer_text"],
            answered_by=row["answered_by"],
        )

    # ── key/value ─────────────────────────────────────────────────────────────

    def kv_get(self, key: str, default: str = "") -> str:
        row = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
