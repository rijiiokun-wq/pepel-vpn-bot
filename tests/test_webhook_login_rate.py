from __future__ import annotations

import sys
from pathlib import Path

SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def test_login_attempts_are_pruned_and_capped(monkeypatch):
    from shop_bot.webhook_server import app as app_module

    now = 100.0

    def _time() -> float:
        return now

    monkeypatch.setattr(app_module.time, "time", _time)
    monkeypatch.setattr(app_module, "_LOGIN_WINDOW_SECONDS", 10)
    monkeypatch.setattr(app_module, "_LOGIN_ATTEMPTS_MAX_IPS", 3)
    app_module._LOGIN_ATTEMPTS.clear()
    app_module._LOGIN_ATTEMPTS["expired"] = [80.0]

    for ip, timestamp in (
        ("old", 100.0),
        ("keep-1", 101.0),
        ("keep-2", 102.0),
        ("new", 103.0),
    ):
        now = timestamp
        app_module._record_login_attempt(ip)

    assert "expired" not in app_module._LOGIN_ATTEMPTS
    assert "old" not in app_module._LOGIN_ATTEMPTS
    assert set(app_module._LOGIN_ATTEMPTS) == {"keep-1", "keep-2", "new"}

    now = 114.0
    assert app_module._check_login_rate("new") is True
    assert "new" not in app_module._LOGIN_ATTEMPTS


def test_successful_admin_login_does_not_consume_rate_limit(monkeypatch):
    from shop_bot.webhook_server import app as app_module

    digest, salt = app_module._hash_password("secret")
    password_hash = app_module._format_hashed_password(digest, salt)
    monkeypatch.setattr(
        app_module,
        "get_setting",
        lambda key: "flask-test-key" if key == "flask_secret_key" else None,
    )
    monkeypatch.setattr(
        app_module,
        "get_all_settings",
        lambda: {"panel_login": "Admin", "panel_password": password_hash},
    )
    app_module._LOGIN_ATTEMPTS.clear()
    flask_app = app_module.create_webhook_app(None)
    client = flask_app.test_client()

    for _ in range(app_module._LOGIN_MAX_ATTEMPTS + 1):
        response = client.post(
            "/login",
            data={"username": " admin ", "password": "secret"},
        )
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/dashboard")

    assert app_module._LOGIN_ATTEMPTS == {}
