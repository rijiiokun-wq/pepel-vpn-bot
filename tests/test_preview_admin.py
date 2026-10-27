import socket
from pathlib import Path
import subprocess
import sys

import pytest

from tools import preview_admin
from tools.preview_admin import create_preview_app, deny_outbound_network


@pytest.fixture
def preview(tmp_path, monkeypatch):
    from shop_bot.data_manager import database

    database_path = tmp_path / "preview.sqlite3"
    monkeypatch.setattr(database, "DB_FILE", database_path)
    monkeypatch.setenv("SHOP_DB_FILE", str(database_path))
    with deny_outbound_network():
        app = create_preview_app(database_path, "synthetic-preview-password")
        app.config["TESTING"] = True
        try:
            yield app, database_path
        finally:
            database.invalidate_settings_cache()


def test_preview_requires_chosen_password_and_renders_dashboard(preview):
    app, _ = preview
    with app.test_client() as client:
        response = client.post("/login", data={"username": "admin", "password": "admin"})
        assert response.status_code == 200
        with client.session_transaction() as session:
            assert not session.get("logged_in")
        response = client.post("/login", data={"username": "preview", "password": "synthetic-preview-password"})
        assert response.status_code == 302
        response = client.get("/dashboard")
        assert response.status_code == 200
        assert b"Local preview" in response.data
        assert "connect-src 'self'" in response.headers["Content-Security-Policy"]
        assert client.get("/settings").status_code == 200


def test_preview_refuses_mutations_and_integration_endpoints(preview):
    app, _ = preview
    with app.test_client() as client:
        client.post("/login", data={"username": "preview", "password": "synthetic-preview-password"})
        for route in ("/settings", "/start-shop-bot", "/internal/runtime/shop/start", "/add-host", "/webhook/yookassa"):
            assert client.post(route, data={"panel_login": "changed"}).status_code == 405
        assert client.get("/podpiska/synthetic-placeholder").status_code == 405
        assert client.get("/health/live").status_code == 200
    from shop_bot.data_manager import database

    assert database.get_setting("panel_login") == "preview"
    stored = database.get_setting("panel_password")
    assert stored.startswith("pbkdf2:")
    assert "synthetic-preview-password" not in stored


def test_preview_never_reuses_an_existing_database(tmp_path):
    existing = tmp_path / "existing.sqlite3"
    existing.write_bytes(b"untouched sentinel")
    with pytest.raises(ValueError, match="new disposable"):
        create_preview_app(existing, "synthetic-preview-password")
    assert existing.read_bytes() == b"untouched sentinel"


def test_preview_network_guard_blocks_connections_and_dns():
    original_socket = socket.socket
    original_dns = socket.getaddrinfo
    with deny_outbound_network():
        with socket.socket() as connection:
            with pytest.raises(RuntimeError, match="Outgoing network"):
                connection.connect(("192.0.2.1", 443))
        with pytest.raises(RuntimeError, match="DNS"):
            socket.getaddrinfo("example.invalid", 443)
    assert socket.socket is original_socket
    assert socket.getaddrinfo is original_dns


def test_preview_parent_discards_ambient_credentials_and_cleans_storage(tmp_path, monkeypatch):
    existing = tmp_path / "do-not-use.sqlite3"
    existing.write_bytes(b"existing data")
    monkeypatch.setenv("SHOP_DB_FILE", str(existing))
    monkeypatch.setenv("BOT_TOKEN", "synthetic-ambient-credential")
    monkeypatch.setenv("FLASK_SECRET_KEY", "synthetic-ambient-session-key")
    monkeypatch.setattr(sys, "argv", ["preview_admin.py"])
    created = []

    def fake_child(command, **kwargs):
        environment = kwargs["env"]
        assert "BOT_TOKEN" not in environment
        assert "FLASK_SECRET_KEY" not in environment
        temporary_db = Path(environment["SHOP_DB_FILE"])
        assert temporary_db != existing
        assert not temporary_db.exists()
        temporary_db.write_bytes(b"disposable preview data")
        created.append(temporary_db)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(preview_admin.subprocess, "run", fake_child)
    assert preview_admin.main() == 0
    assert created and not created[0].parent.exists()
    assert existing.read_bytes() == b"existing data"


def test_preview_refuses_to_serve_if_password_write_fails(tmp_path, monkeypatch):
    from shop_bot.data_manager import database

    database_path = tmp_path / "preview.sqlite3"
    monkeypatch.setattr(database, "DB_FILE", database_path)
    original_update = database.update_setting

    def failed_password_write(key, value):
        if key != "panel_password":
            original_update(key, value)

    monkeypatch.setattr(database, "update_setting", failed_password_write)
    with pytest.raises(RuntimeError, match="refusing to serve"):
        create_preview_app(database_path, "synthetic-preview-password")
    database.invalidate_settings_cache()
