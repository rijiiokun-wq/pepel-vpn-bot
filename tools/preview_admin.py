#!/usr/bin/env python3
"""Preview the admin UI with disposable storage and no running bot.

The Python network guard is a development aid, not an OS sandbox: native code
and subprocesses are not isolated. Do not supply service credentials or data.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import getpass
import hashlib
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
READ_ONLY_PATHS = frozenset({"/", "/dashboard", "/users", "/settings", "/referrals", "/health/live"})


@contextmanager
def deny_outbound_network():
    """Block Python TCP/UDP connections and DNS; permit numeric server binding."""
    originals = {
        name: getattr(socket, name)
        for name in ("socket", "getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr")
    }

    class OfflineSocket(originals["socket"]):
        def connect(self, address):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                raise RuntimeError("Outgoing network access is disabled in the local preview")
            return super().connect(address)

        def connect_ex(self, address):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                raise RuntimeError("Outgoing network access is disabled in the local preview")
            return super().connect_ex(address)

        def sendto(self, *args, **kwargs):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                raise RuntimeError("Outgoing network access is disabled in the local preview")
            return super().sendto(*args, **kwargs)

    def no_dns(*args, **kwargs):
        raise RuntimeError("DNS access is disabled in the local preview")

    def numeric_bind(host, port, family=0, type=0, proto=0, flags=0):
        if host != "127.0.0.1":
            return no_dns()
        return originals["getaddrinfo"](host, port, family, type, proto, flags | socket.AI_NUMERICHOST)

    def numeric_reverse(host):
        if host == "127.0.0.1":
            return "localhost", [], ["127.0.0.1"]
        return no_dns()

    socket.socket = OfflineSocket
    socket.getaddrinfo = numeric_bind
    socket.gethostbyname = no_dns
    socket.gethostbyname_ex = no_dns
    socket.gethostbyaddr = numeric_reverse
    try:
        yield
    finally:
        for name, function in originals.items():
            setattr(socket, name, function)


class ReadOnlyPreview:
    """Reject unselected routes before any application request hooks execute."""

    def __init__(self, application):
        self.application = application

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        method = environ.get("REQUEST_METHOD", "GET").upper()
        readable = path in READ_ONLY_PATHS or path.startswith("/static/")
        allowed = (
            (readable and method in {"GET", "HEAD"})
            or (path == "/login" and method in {"GET", "HEAD", "POST"})
            or (path == "/logout" and method == "POST")
        )
        if not allowed:
            body = b"This local preview is read-only. This action or page is unavailable.\n"
            start_response("405 Method Not Allowed", [
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
            ])
            return [body]
        return self.application(environ, start_response)


def create_preview_app(database_path: Path, password: str):
    """Create the UI only against a new database selected before module import."""
    database_path = database_path.absolute()
    if database_path.exists() or database_path.is_symlink():
        raise ValueError("The preview requires a new disposable database")
    if len(password) < 12:
        raise ValueError("Use a preview password with at least 12 characters")
    from shop_bot.data_manager import database

    if Path(database.DB_FILE).absolute() != database_path:
        raise ValueError("The database module must use the disposable preview path")
    database.initialize_db()
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()
    password_hash = f"pbkdf2:{salt}:{digest}"
    session_secret = secrets.token_urlsafe(48)
    database.update_setting("panel_login", "preview")
    database.update_setting("panel_password", password_hash)
    database.update_setting("flask_secret_key", session_secret)
    settings = database.get_all_settings()
    if (
        settings.get("panel_login") != "preview"
        or settings.get("panel_password") != password_hash
        or settings.get("flask_secret_key") != session_secret
    ):
        raise RuntimeError("Preview credentials could not be initialized; refusing to serve")

    from shop_bot.webhook_server.app import create_webhook_app

    app = create_webhook_app(None)
    app.config.update(SESSION_COOKIE_SECURE=False, TESTING=False)
    app.wsgi_app = ReadOnlyPreview(app.wsgi_app)

    @app.after_request
    def preview_response(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        if response.mimetype == "text/html" and not response.direct_passthrough:
            banner = (
                '<aside style="padding:12px;background:#fff3cd;color:#222">'
                'Local preview — changes are disabled. '
                'Data is deleted when this process exits.</aside>'
            )
            response.set_data(re.sub(r"(<body\b[^>]*>)", lambda match: match[0] + banner,
                                     response.get_data(as_text=True), count=1))
        return response

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only admin preview using a fresh temporary database")
    parser.add_argument("--port", type=int, default=18084)
    parser.add_argument("--isolated-child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")

    if args.isolated_child:
        if os.environ.get("PEPEL_PREVIEW_CHILD") != "1":
            parser.error("Run the preview through its parent command")
        if not sys.stdin.isatty():
            parser.error("Use an interactive terminal for the preview password")
        password = getpass.getpass("Choose a temporary preview password (12+ characters): ")
        if password != getpass.getpass("Confirm preview password: "):
            parser.error("Passwords do not match")
        sys.path[:0] = [str(ROOT), str(ROOT / "src")]
        logging.basicConfig(level=logging.WARNING)
        with deny_outbound_network():
            app = create_preview_app(Path(os.environ["SHOP_DB_FILE"]), password)
            del password
            print(f"Local read-only preview: http://127.0.0.1:{args.port}/login (username: preview)")
            print("Ctrl+C stops the preview and deletes its temporary data.")
            app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False, load_dotenv=False)
        return 0

    with tempfile.TemporaryDirectory(prefix="pepel-preview-") as temporary:
        environment = {
            "PATH": os.environ.get("PATH", os.defpath),
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "BOT_SOURCE": "main",
            "SHOP_DB_FILE": str(Path(temporary) / "preview.sqlite3"),
            "FLASK_SESSION_COOKIE_SECURE": "false",
            "PEPEL_PREVIEW_CHILD": "1",
        }
        try:
            result = subprocess.run(
                [sys.executable, "-B", "-I", str(Path(__file__).resolve()), "--isolated-child", "--port", str(args.port)],
                cwd=temporary, env=environment, check=False,
            )
            return result.returncode
        except KeyboardInterrupt:
            return 130


if __name__ == "__main__":
    raise SystemExit(main())
