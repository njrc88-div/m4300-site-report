"""Local username/password user store backing app/auth.py - replaces the
earlier Google OAuth gate with accounts this app manages itself: a
username, a bcrypt password hash, and a role ("admin" or "user").

SQLite, one file at USERS_DB_PATH (bind-mounted alongside the audit log -
see docker-compose.yml - so accounts survive `docker compose up --build`
recreating the container). Small enough a real database server would be
pure overhead for what this is: a handful of accounts for a small team.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import bcrypt

USERS_DB_PATH = Path(os.environ.get("USERS_DB_PATH", "/srv/data/users.db"))

ROLES = ("admin", "user")


@contextmanager
def _connect():
    USERS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(USERS_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
                created_at TEXT NOT NULL
            )
            """
        )


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def user_count() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def bootstrap_initial_admin(username: str, password: str) -> bool:
    """Creates the given account as an admin, but only if the users table
    is currently empty - won't silently reset an existing deployment's
    accounts just because INITIAL_ADMIN_* env vars are still set. Returns
    whether it actually created anything."""
    if user_count() > 0:
        return False
    create_user(username, password, "admin")
    return True


def create_user(username: str, password: str, role: str) -> None:
    username = username.strip()
    if not username:
        raise ValueError("Username can't be empty.")
    if role not in ROLES:
        raise ValueError(f"Role must be one of {ROLES}, got {role!r}.")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    with _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                (username, _hash_password(password), role, _now()),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"A user named {username!r} already exists.") from None


def verify_password(username: str, password: str) -> dict | None:
    """Returns {"username", "role"} on success, None on any failure (no
    such user, wrong password) - deliberately the same result either way
    so a login form can't be used to enumerate valid usernames."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT username, password_hash, role FROM users WHERE username = ?", (username.strip(),)
        ).fetchone()
    if row is None:
        return None
    if not bcrypt.checkpw(password.encode("utf-8"), row["password_hash"].encode("utf-8")):
        return None
    return {"username": row["username"], "role": row["role"]}


def list_users() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT username, role, created_at FROM users ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def set_role(username: str, role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"Role must be one of {ROLES}, got {role!r}.")
    with _connect() as conn:
        cur = conn.execute("UPDATE users SET role = ? WHERE username = ?", (role, username))
        if cur.rowcount == 0:
            raise ValueError(f"No user named {username!r}.")


def reset_password(username: str, new_password: str) -> None:
    if len(new_password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (_hash_password(new_password), username),
        )
        if cur.rowcount == 0:
            raise ValueError(f"No user named {username!r}.")


def delete_user(username: str) -> None:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM users WHERE username = ?", (username,))
        if cur.rowcount == 0:
            raise ValueError(f"No user named {username!r}.")


def admin_count() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]
