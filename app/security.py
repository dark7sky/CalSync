"""Database-backed identity and opaque browser sessions for CalSync.

No JWT or password is stored in browser JavaScript/localStorage.
Passwords: Argon2id. Session token: random opaque cookie, SHA-256 digest in DB.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
import uuid

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
ROLES = ("viewer", "submitter", "admin")
MAX_LOGIN_FAILURES = 10
LOGIN_WINDOW_SECONDS = 900
SESSION_MAX_SECONDS = 43200


def initialize(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('viewer', 'submitter', 'admin')),
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
        token_hash TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        csrf TEXT NOT NULL,
        expires_at INTEGER NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)")
    conn.execute("""CREATE TABLE IF NOT EXISTS auth_attempts (
        key_hash TEXT PRIMARY KEY,
        failures INTEGER NOT NULL,
        window_started INTEGER NOT NULL
    )""")


def create_user(conn: sqlite3.Connection, username: str, password: str, role: str) -> str:
    username = username.strip()
    if not 3 <= len(username) <= 64 or not all(c.isascii() and (c.isalnum() or c in "._-") for c in username):
        raise ValueError("Username must be 3-64 ASCII letters/digits/._-")
    if role not in ROLES:
        raise ValueError("Unknown role")
    if len(password) < 14 or len(password) > 1024:
        raise ValueError("Use a password of at least 14 and at most 1024 characters")
    user_id = uuid.uuid4().hex
    conn.execute("INSERT INTO users (id,username,password_hash,role,created_at) VALUES (?,?,?,?,?)",
                 (user_id, username, PASSWORD_HASHER.hash(password), role, int(time.time())))
    return user_id


def verify_credentials(conn: sqlite3.Connection, username: str, password: str) -> dict | None:
    username = username.strip()
    key_hash = hashlib.sha256(("login:" + username.casefold()).encode("utf-8")).hexdigest()
    now = int(time.time())
    current = conn.execute("SELECT failures, window_started FROM auth_attempts WHERE key_hash=?",
                           (key_hash,)).fetchone()
    if current and now - current["window_started"] < LOGIN_WINDOW_SECONDS and current["failures"] >= MAX_LOGIN_FAILURES:
        # Avoid allowing brute force against a publicly accessible endpoint.
        raise ValueError("Too many failed attempts. Try again later.")
    row = conn.execute("SELECT id, username, password_hash, role, enabled FROM users WHERE username=?",
                       (username,)).fetchone()
    correct = False
    if row and row["enabled"]:
        try:
            correct = bool(PASSWORD_HASHER.verify(row["password_hash"], password))
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            correct = False
    if correct:
        conn.execute("DELETE FROM auth_attempts WHERE key_hash=?", (key_hash,))
        if PASSWORD_HASHER.check_needs_rehash(row["password_hash"]):
            conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                         (PASSWORD_HASHER.hash(password), row["id"]))
        return dict(row)
    if not current or now - current["window_started"] >= LOGIN_WINDOW_SECONDS:
        conn.execute("INSERT OR REPLACE INTO auth_attempts (key_hash,failures,window_started) VALUES (?,?,?)",
                     (key_hash, 1, now))
    else:
        conn.execute("UPDATE auth_attempts SET failures=failures+1 WHERE key_hash=?",
                     (key_hash,))
    return None


def create_session(conn: sqlite3.Connection, user_id: str, max_age: int = SESSION_MAX_SECONDS) -> str:
    token = secrets.token_urlsafe(48)
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    csrf = secrets.token_urlsafe(32)
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (int(time.time()),))
    conn.execute("INSERT INTO sessions(token_hash,user_id,csrf,expires_at) VALUES(?,?,?,?)",
                 (digest, user_id, csrf, int(time.time()) + max_age))
    return token


def get_session(conn: sqlite3.Connection, token: str | None) -> dict | None:
    if not token or len(token) > 256:
        return None
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    row = conn.execute("""SELECT u.id, u.username, u.role, u.enabled, s.csrf
        FROM sessions s JOIN users u ON s.user_id = u.id
        WHERE s.token_hash=? AND s.expires_at>?""", (digest, int(time.time()))).fetchone()
    if row is None or not row["enabled"]:
        return None
    return dict(row)


def revoke_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash=?",
                     (hashlib.sha256(token.encode("utf-8")).hexdigest(),))


def _database_file() -> str:
    return os.getenv("CALSYNC_DATABASE", "/data/calsync.db")


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage CalSync user accounts")
    parser.add_argument("command", choices=("add-user", "list-users", "disable-user", "reset-password"))
    parser.add_argument("--username")
    parser.add_argument("--role", choices=ROLES, default="submitter")
    args = parser.parse_args()
    path = _database_file()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    with conn:
        initialize(conn)
        if args.command == "list-users":
            for row in conn.execute("SELECT username, role, enabled FROM users ORDER BY username"):
                print(f"{row['username']}\t{row['role']}\t{'active' if row['enabled'] else 'disabled'}")
            return
        if not args.username:
            parser.error("--username is required")
        if args.command in ("add-user", "reset-password"):
            password = getpass.getpass("Password (min 14 characters): ")
            if password != getpass.getpass("Confirm password: "):
                raise SystemExit("Passwords do not match")
            if len(password) < 14 or len(password) > 1024:
                raise SystemExit("Password must be 14-1024 characters")
            if args.command == "add-user":
                create_user(conn, args.username, password, args.role)
            else:
                result = conn.execute(
                    "UPDATE users SET password_hash=? WHERE username=?",
                    (PASSWORD_HASHER.hash(password), args.username),
                )
                if not result.rowcount:
                    raise SystemExit("User not found")
                conn.execute("""DELETE FROM sessions WHERE user_id =
                    (SELECT id FROM users WHERE username=?)""", (args.username,))
        elif args.command == "disable-user":
            row = conn.execute("SELECT id,role,enabled FROM users WHERE username=?", (args.username,)).fetchone()
            if not row:
                raise SystemExit("User not found")
            if row["role"] == "admin" and row["enabled"]:
                count = conn.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND enabled=1").fetchone()[0]
                if count <= 1:
                    raise SystemExit("Cannot disable the last active admin")
            conn.execute("UPDATE users SET enabled=0 WHERE id=?", (row["id"],))
            conn.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
    print("Account change saved.")


if __name__ == "__main__":
    main()
