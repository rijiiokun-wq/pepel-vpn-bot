from pathlib import Path


def _read_users_template() -> str:
    template_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "shop_bot"
        / "webhook_server"
        / "templates"
        / "users.html"
    )
    return template_path.read_text(encoding="utf-8")


def test_users_template_contains_horizontal_scroll_hints_for_detail_tables():
    template = _read_users_template()

    assert template.count('class="user-detail-table-scroll" data-table-scroll') == 2
    assert template.count('class="user-detail-keys-wrapper" data-scroll-wrapper') == 2
    assert template.count('class="user-detail-scroll-hint" data-scroll-hint hidden') == 2
    assert "Потяните таблицу влево/вправо мышкой или колесом" in template


def test_users_template_has_live_signature_sync_hooks():
    template = _read_users_template()

    assert "data-live-signature-endpoint" in template
    assert "data-live-signature" in template
    assert "data-live-sync-status" in template
    assert "users_live_signature_api" in template
