"""Direct CalSync server transport for the Windows client.

The corporate proxy requires browser-like User-Agent plus NTLM SSO on a reused
connection; Python has no reliable in-process NTLM proxy support, so HTTP runs
in transport.ps1 (PowerShell/.NET, proven working) and returns one JSON line.

Passwords travel via the subprocess environment (not the command line) and are
never written to disk. No third-party dependencies.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

if getattr(sys, "frozen", False):
    SCRIPT = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "transport.ps1"
else:
    SCRIPT = Path(__file__).with_name("transport.ps1")


class TransportError(RuntimeError):
    def __init__(self, stage: str, http: int, message: str):
        super().__init__(f"[{stage}] HTTP {http}: {message}")
        self.stage = stage
        self.http = http


def _invoke(mode: str, origin: str, username: str | None = None,
            password: str | None = None, batch: dict | None = None) -> dict:
    origin = origin.rstrip("/")
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            str(SCRIPT), "-Origin", origin, "-Mode", mode]
    env = dict(os.environ)
    temp_path = None
    if mode != "health":
        if not username or not password:
            raise TransportError("credentials", 0, "username/password required")
        args += ["-Username", username]
        env["CALSYNC_SEND_PASSWORD"] = password
    if batch is not None:
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(batch, handle, ensure_ascii=False)
        handle.close()
        temp_path = handle.name
        args += ["-BatchFile", temp_path]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=180, env=env)
    finally:
        if temp_path:
            os.unlink(temp_path)
    line = next((ln for ln in reversed(proc.stdout.splitlines())
                 if ln.strip().startswith("{")), "")
    if not line:
        detail = (proc.stderr or proc.stdout or "no output").strip()[-500:]
        raise TransportError("transport", proc.returncode, detail)
    result = json.loads(line)
    if not result.get("ok"):
        raise TransportError(result.get("stage", "?"), result.get("http", 0),
                             result.get("error") or "unknown failure")
    return result


def health(origin: str) -> dict:
    """Connectivity check through the corporate proxy; no credentials needed."""
    return _invoke("health", origin)


def import_batch(origin: str, username: str, password: str, batch: dict) -> dict:
    """Submit {'batchId':..., 'events':[...]} or {'batchId':..., 'deletions':[...]}."""
    return _invoke("import", origin, username, password, batch)["data"]


def receipts(origin: str, username: str, password: str) -> dict:
    """Fetch the server-side receipt/status export for queue reconciliation."""
    return _invoke("receipts", origin, username, password)["data"]
