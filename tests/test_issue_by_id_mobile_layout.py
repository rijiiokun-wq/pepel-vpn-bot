from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _read_css() -> str:
    return (_repo_root() / "src" / "shop_bot" / "webhook_server" / "static" / "css" / "style.css").read_text(
        encoding="utf-8"
    )


def _read_migration_template() -> str:
    return (_repo_root() / "src" / "shop_bot" / "webhook_server" / "templates" / "migration.html").read_text(
        encoding="utf-8"
    )


def _read_issue_script() -> str:
    return (_repo_root() / "src" / "shop_bot" / "webhook_server" / "static" / "js" / "script.js").read_text(
        encoding="utf-8"
    )


def test_issue_by_id_mobile_layout_disables_sticky_panels():
    css = _read_css()

    sticky_rule_index = css.index(".migration-form-panel,\n.issue-panel {\n\tposition: sticky; top: 16px;")
    mobile_override = (
        "@media (max-width: 900px) {\n"
        "\t.migration-form-panel,\n"
        "\t.issue-panel {\n"
        "\t\tposition: static;\n"
        "\t\ttop: auto;\n"
        "\t}\n"
        "}"
    )
    mobile_override_index = css.index(mobile_override)

    assert mobile_override_index > sticky_rule_index


def test_admin_mobile_shell_prevents_header_overflow():
    css = _read_css()

    required_snippets = [
        "@media (max-width: 600px)",
        ".header-controls {\n\t\tflex-direction: column;",
        ".header-bots {\n\t\tdisplay: grid;",
        ".bot-pill {\n\t\twidth: 100%;",
        ".header-logout .button {\n\t\twidth: 100%;",
        "@media (max-width: 360px)",
        ".header-bots,\n\t.navigation {\n\t\tgrid-template-columns: 1fr;",
    ]

    for snippet in required_snippets:
        assert snippet in css


def test_issue_by_id_mobile_modal_renders_key_rows_as_labeled_cards():
    css = _read_css()
    script = _read_issue_script()

    required_css_snippets = [
        ".issue-keys-modal .migration-table thead {\n\t\tdisplay: none;",
        ".issue-keys-modal .migration-table td::before",
        "content: attr(data-label);",
        ".issue-modal-action {\n\t\twidth: 100%;",
    ]
    for snippet in required_css_snippets:
        assert snippet in css

    required_script_snippets = [
        "function _issueAppendModalCell(tr, label, value)",
        "_issueAppendModalCell(tr, 'ID'",
        "_issueAppendModalCell(tr, 'Хост'",
        "actionTd.dataset.label = 'Действия'",
        "_issueSetModalStatus('Email скопирован')",
    ]
    for snippet in required_script_snippets:
        assert snippet in script


def test_issue_host_selects_do_not_render_panel_urls_in_visible_options():
    template = _read_migration_template()

    assert "{{ host.host_name }} ({{ host.host_url }})" not in template
    assert 'title="{{ host.host_url }}"' in template
    assert "data-has-subscription-url=\"{{ 'true' if host.has_direct_subscription_url else 'false' }}\"" in template


def test_issue_tier_hint_documents_standard_and_super_white_behavior():
    template = _read_migration_template()
    script = _read_issue_script()

    assert 'id="issue-tier-hint"' in template
    assert "Супер: WHITELIST — 40 ГБ/мес." in template
    assert "Стандарт: WHITELIST — 5 ГБ/мес." in template
    assert "function _issueUpdateTierHint()" in script
    assert "Стандарт: выдача только по обычным inbound; WHITELIST не создаётся." in script
    assert "Супер: выдача по всем inbound; WHITELIST получает лимит 40 GiB." in script
    assert "Клиент получит один URL подписки выбранного хоста." in script
    assert "У выбранного хоста не настроен URL подписки; будет показана прямая ссылка подключения." in script


def test_issue_execute_reuses_request_id_after_browser_disconnect():
    script = _read_issue_script()

    assert "pepelbot.issue-by-id.pending-request" in script
    assert "window.sessionStorage.setItem" in script
    assert "requestId = _issueRequestIdForPayload(requestPayload)" in script
    assert "request_id: requestId" in script
    assert "X-Issue-Request-In-Progress" in script
