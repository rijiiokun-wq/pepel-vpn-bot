from __future__ import annotations

import pytest
import scripts.bootstrap_runtime_settings as bootstrap

from shop_bot.data_manager import database


@pytest.fixture(autouse=True)
def _clean_bootstrap_env(monkeypatch: pytest.MonkeyPatch) -> None:
    env_names = set()
    for _, names in bootstrap.ENV_SETTING_MAP:
        env_names.update(names)
    for _, _, names in bootstrap.USERNAME_BOOTSTRAP_SOURCES:
        env_names.update(names)

    for env_name in env_names:
        monkeypatch.delenv(env_name, raising=False)

    return


@pytest.fixture
def temp_runtime_db(monkeypatch: pytest.MonkeyPatch, tmp_path):
    db_file = tmp_path / "shop.db"
    monkeypatch.setattr(database, "DB_FILE", db_file)
    monkeypatch.setattr(database, "_WAL_INITIALIZED", False)
    database.invalidate_settings_cache()
    monkeypatch.setattr(bootstrap, "database", database)
    return db_file


def test_bootstrap_runtime_settings_populates_missing_required_values(
    monkeypatch: pytest.MonkeyPatch,
    temp_runtime_db,
):
    monkeypatch.setenv("BOT_TOKEN", "main-token")
    monkeypatch.setenv("RESERVE_BOT_TOKEN", "reserve-token")
    monkeypatch.setenv("ADMIN_TELEGRAM_ID", "123456")
    monkeypatch.setenv("YOOKASSA_SHOP_ID", "shop-id")
    monkeypatch.setenv("YOOKASSA_SECRET_KEY", "secret")
    monkeypatch.setenv("YOOKASSA_PAYMENTS_ENABLED", "true")
    monkeypatch.setenv("PAYPEAR_SHOP_ID", "paypear-shop")
    monkeypatch.setenv("PAYPEAR_SECRET_KEY", "paypear-secret")

    def resolve_username(token: str, timeout_seconds: float) -> str | None:
        del timeout_seconds
        return {
            "main-token": "vpnpepelbot",
            "reserve-token": "probelvpnbot",
        }.get(token)

    report = bootstrap.bootstrap_runtime_settings(
        require_startup_settings=True,
        username_resolver=resolve_username,
    )

    assert temp_runtime_db.exists()
    assert report.missing_required == ()
    assert database.get_setting("telegram_bot_token") == "main-token"
    assert database.get_setting("telegram_bot_username") == "vpnpepelbot"
    assert database.get_setting("reserve_bot_token") == "reserve-token"
    assert database.get_setting("reserve_bot_username") == "probelvpnbot"
    assert database.get_setting("admin_telegram_id") == "123456"
    assert database.get_setting("yookassa_payments_enabled") == "true"
    assert database.get_setting("paypear_secret_key") == "paypear-secret"


def test_bootstrap_runtime_settings_keeps_existing_db_values_without_overwrite(
    monkeypatch: pytest.MonkeyPatch,
    temp_runtime_db,
):
    database.initialize_db()
    database.update_setting("admin_telegram_id", "111")
    monkeypatch.setenv("ADMIN_TELEGRAM_ID", "222")

    report = bootstrap.bootstrap_runtime_settings()

    assert database.get_setting("admin_telegram_id") == "111"
    assert "admin_telegram_id" in report.kept_existing
    assert "admin_telegram_id" not in report.updated


def test_bootstrap_runtime_settings_applies_panel_credentials_from_env(
    monkeypatch: pytest.MonkeyPatch,
    temp_runtime_db,
):
    password_hash = "pbkdf2:salt:digest"
    database.initialize_db()
    database.update_setting("panel_login", "old-login")
    database.update_setting("panel_password", "pbkdf2:old:hash")
    monkeypatch.setenv("PANEL_LOGIN", "Admin")
    monkeypatch.setenv("PANEL_PASSWORD_HASH", password_hash)

    report = bootstrap.bootstrap_runtime_settings()

    assert database.get_setting("panel_login") == "Admin"
    assert database.get_setting("panel_password") == password_hash
    assert "panel_login" in report.updated
    assert "panel_password" in report.updated


def test_bootstrap_runtime_settings_rejects_plain_panel_password_env(
    monkeypatch: pytest.MonkeyPatch,
    temp_runtime_db,
):
    monkeypatch.setenv("PANEL_PASSWORD_HASH", "plain-password")

    with pytest.raises(RuntimeError, match="Invalid PBKDF2 hash"):
        bootstrap.bootstrap_runtime_settings()


def test_bootstrap_runtime_settings_fails_when_required_values_are_missing(temp_runtime_db):
    with pytest.raises(RuntimeError, match="Missing required runtime settings"):
        bootstrap.bootstrap_runtime_settings(require_startup_settings=True)
