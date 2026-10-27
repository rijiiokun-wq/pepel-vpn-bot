from pathlib import Path


def test_dashboard_template_has_dedicated_source_filter_row_class():
    template_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "shop_bot"
        / "webhook_server"
        / "templates"
        / "dashboard.html"
    )
    css_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "shop_bot"
        / "webhook_server"
        / "static"
        / "css"
        / "style.css"
    )

    template = template_path.read_text(encoding="utf-8")
    css = css_path.read_text(encoding="utf-8")

    assert "dashboard-source-summary" in template
    assert ".dashboard-type-summary + .dashboard-type-summary" in css
    assert ".dashboard-source-summary .dashboard-type-summary-item" in css


def test_dashboard_kpi_cards_pin_values_to_card_bottom():
    css_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "shop_bot"
        / "webhook_server"
        / "static"
        / "css"
        / "style.css"
    )

    css = css_path.read_text(encoding="utf-8")

    assert ".dashboard-kpi-card" in css
    assert "display: flex;" in css
    assert "flex-direction: column;" in css
    assert "min-height: 116px;" in css
    assert "margin-top: auto;" in css
    assert "padding-top: 10px;" in css


def test_dashboard_transactions_pagination_preserves_open_section_context():
    repo_root = Path(__file__).resolve().parents[1]
    template = (
        repo_root
        / "src"
        / "shop_bot"
        / "webhook_server"
        / "templates"
        / "dashboard.html"
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

    assert 'id="dashboard-transactions-section"' in template
    assert 'id="dashboard-transactions-details"' in template
    assert "#dashboard-transactions-section" in template
    assert "initializeDashboardTransactionsPagination()" in script
    assert "window.location.hash !== '#dashboard-transactions-section'" in script
    assert "details.open = true" in script


def test_dashboard_template_has_live_signature_sync_hooks():
    repo_root = Path(__file__).resolve().parents[1]
    template = (
        repo_root
        / "src"
        / "shop_bot"
        / "webhook_server"
        / "templates"
        / "dashboard.html"
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

    assert "data-live-signature-endpoint" in template
    assert "data-live-signature" in template
    assert "data-live-sync-status" in template
    assert "initializeLiveDataSync()" in script
    assert "dashboard_live_signature_api" in template
