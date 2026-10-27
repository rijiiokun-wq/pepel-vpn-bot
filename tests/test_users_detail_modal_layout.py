from pathlib import Path


def test_user_detail_modal_hides_raw_key_value_and_keeps_copy_action():
    repo_root = Path(__file__).resolve().parents[1]
    template = (
        repo_root
        / "src"
        / "shop_bot"
        / "webhook_server"
        / "templates"
        / "users.html"
    ).read_text(encoding="utf-8")
    script = (
        repo_root
        / "src"
        / "shop_bot"
        / "webhook_server"
        / "static"
        / "js"
        / "script.js"
    ).read_text(encoding="utf-8")
    css = (
        repo_root
        / "src"
        / "shop_bot"
        / "webhook_server"
        / "static"
        / "css"
        / "style.css"
    ).read_text(encoding="utf-8")

    assert "<th>Ключ</th>" not in template
    assert '<th class="actions-cell">Ключ</th>' in template
    assert "copyButton.textContent = 'Скопировать'" in script
    assert "cell.colSpan = 7" in script
    assert "user-detail-key-preview" not in script
    assert "paymentCell.className = 'table-mono table-secondary-text'" in script
    assert "purchaseCell.className = 'user-detail-date-cell table-date table-mono'" in script
    assert ".user-detail-key-action-cell" in css
    assert "box-sizing: border-box;" in css


def test_user_detail_modal_key_email_has_copy_button_cell():
    repo_root = Path(__file__).resolve().parents[1]
    template = (
        repo_root
        / "src"
        / "shop_bot"
        / "webhook_server"
        / "templates"
        / "users.html"
    ).read_text(encoding="utf-8")
    script = (
        repo_root
        / "src"
        / "shop_bot"
        / "webhook_server"
        / "static"
        / "js"
        / "script.js"
    ).read_text(encoding="utf-8")

    assert '<th class="actions-cell">KEY_EMAIL</th>' in template
    assert "copyEmailButton.className = 'button button-small button-primary user-detail-copy'" in script
    assert "copyEmailButton.textContent = 'Скопировать'" in script
    assert "copyEmailButton.setAttribute('aria-label', `Скопировать KEY_EMAIL: ${keyEmailValue}`)" in script
