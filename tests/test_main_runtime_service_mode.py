import pytest

pytest.importorskip("aiogram")

from shop_bot.bot import handlers


def test_build_main_runtime_service_text_uses_configured_message():
    configured = "Покупка временно недоступна, ведутся технические работы. Попробуйте позже."

    rendered = handlers._build_main_runtime_service_text(configured)

    assert rendered.startswith("⚙️ Основной сервер временно недоступен.")
    assert configured in rendered


def test_build_main_runtime_service_text_uses_fallback_when_empty():
    rendered = handlers._build_main_runtime_service_text("")

    assert rendered == (
        "⚙️ Основной сервер временно недоступен.\n\n"
        "Уважаемые пользователи, в данный момент покупка ключей доступна только "
        "в резервном боте — @probelvpnbot"
    )


def test_main_runtime_status_is_healthy_requires_running_and_settings_ready():
    assert handlers._main_runtime_status_is_healthy(
        {"shop_bot_running": True, "all_settings_ok": True}
    )
    assert not handlers._main_runtime_status_is_healthy(
        {"shop_bot_running": False, "all_settings_ok": True}
    )
    assert not handlers._main_runtime_status_is_healthy(
        {"shop_bot_running": True, "all_settings_ok": False}
    )
    assert not handlers._main_runtime_status_is_healthy(None)
