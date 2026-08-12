#!/usr/bin/env python3
"""Authorize one personal Gmail mailbox with the read-only installed-app scope."""

from __future__ import annotations

import json
import os
import secrets
import stat
import sys
import tempfile
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

ROOT = Path(__file__).resolve().parent.parent
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
DEFAULT_CLIENT = ROOT / "data/private/gmail_oauth_client.json"
DEFAULT_TOKEN = ROOT / "data/private/gmail_oauth_token.json"


class _Callback(BaseHTTPRequestHandler):
    code = ""
    returned_state = ""
    error = ""

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        query = parse_qs(urlparse(self.path).query)
        type(self).code = str((query.get("code") or [""])[0])
        type(self).returned_state = str((query.get("state") or [""])[0])
        type(self).error = str((query.get("error") or [""])[0])
        body = b"Authorization received. You can close this tab."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    if any(arg in {"-h", "--help"} for arg in sys.argv[1:]):
        print(__doc__)
        print("\nUsage: python tools/setup_gmail.py [client-json] [token-json]")
        return 0
    client_path = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_CLIENT
    token_path = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else DEFAULT_TOKEN
    client = _read_installed_client(client_path)
    state = secrets.token_urlsafe(24)
    server = HTTPServer(("127.0.0.1", 0), _Callback)
    server.timeout = 300
    redirect_uri = f"http://127.0.0.1:{server.server_port}/"
    authorization_endpoint = str(
        client.get("auth_uri") or "https://accounts.google.com/o/oauth2/auth"
    )
    url = (
        authorization_endpoint
        + "?"
        + urlencode(
            {
                "client_id": client["client_id"],
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": SCOPE,
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
            }
        )
    )
    print("Requesting exactly this scope:")
    print(f"  {SCOPE}")
    print("Opening the Google consent screen in your local browser...")
    if not webbrowser.open(url):
        print("Browser launch failed. Open this URL locally:")
        print(url)
    server.handle_request()
    server.server_close()
    if _Callback.error:
        raise SystemExit(f"Authorization failed: {_Callback.error[:80]}")
    if not _Callback.code or _Callback.returned_state != state:
        raise SystemExit("Authorization callback missing or state did not match")
    token_uri = str(client.get("token_uri") or "https://oauth2.googleapis.com/token")
    response = httpx.post(
        token_uri,
        data={
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "code": _Callback.code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=20.0,
    )
    if response.status_code >= 400:
        raise SystemExit(f"Token exchange failed with HTTP {response.status_code}")
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("refresh_token"):
        raise SystemExit(
            "Google did not return an offline refresh token; revoke consent and retry"
        )
    scope = str(payload.get("scope") or "")
    if scope and set(scope.split()) != {SCOPE}:
        raise SystemExit("Returned token does not contain exactly gmail.readonly")
    payload["scope"] = scope or SCOPE
    payload["expires_at"] = time.time() + max(1, int(payload.get("expires_in") or 3600))
    _atomic_private_json(token_path, payload)
    print(f"Token saved privately to: {token_path}")
    print("Authorize on this personal machine, then transfer only the token file")
    print("to the server over an encrypted channel and keep it mode 0600.")
    print("Next: configure GMAIL_MAILBOX_KEY plus GMAIL_LABEL_IDS/GMAIL_QUERY,")
    print("then run: python main.py --gmail-sync --dry-run")
    return 0


def _read_installed_client(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"OAuth client JSON not found: {path}") from exc
    client = payload.get("installed") if isinstance(payload, dict) else None
    if (
        not isinstance(client, dict)
        or not client.get("client_id")
        or not client.get("client_secret")
    ):
        raise SystemExit("OAuth JSON must contain an installed desktop-app client")
    return {
        str(key): str(value)
        for key, value in client.items()
        if not isinstance(value, list)
    }


def _atomic_private_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(stat.S_IRWXU)
    except OSError:
        pass
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


if __name__ == "__main__":
    raise SystemExit(main())
