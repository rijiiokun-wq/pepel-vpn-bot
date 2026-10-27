from shop_bot.webhook_server import app as app_module


def test_resolve_runtime_bot_token_prefers_db_setting(monkeypatch):
    monkeypatch.setattr(app_module, "BOT_SOURCE", "main")
    monkeypatch.setattr(app_module, "get_setting", lambda key: "db-main-token" if key == "telegram_bot_token" else None)
    monkeypatch.setenv("BOT_TOKEN", "env-fallback-token")

    assert app_module._resolve_runtime_bot_token() == "db-main-token"


def test_resolve_runtime_bot_token_falls_back_to_env(monkeypatch):
    monkeypatch.setattr(app_module, "BOT_SOURCE", "reserve")
    monkeypatch.setattr(app_module, "get_setting", lambda key: None)
    monkeypatch.setenv("BOT_TOKEN", "env-fallback-token")

    assert app_module._resolve_runtime_bot_token() == "env-fallback-token"


def test_resolve_runtime_bot_token_uses_selected_bot_source(monkeypatch):
    monkeypatch.setattr(app_module, "BOT_SOURCE", "main")
    monkeypatch.setattr(
        app_module,
        "get_setting",
        lambda key: {
            "telegram_bot_token": "db-main-token",
            "reserve_bot_token": "db-reserve-token",
        }.get(key),
    )
    monkeypatch.setenv("BOT_TOKEN", "env-fallback-token")

    assert app_module._resolve_runtime_bot_token("reserve") == "db-reserve-token"


def test_normalize_manual_bot_source_falls_back_to_runtime(monkeypatch):
    monkeypatch.setattr(app_module, "BOT_SOURCE", "reserve")

    assert app_module._normalize_manual_bot_source("weird") == "reserve"
