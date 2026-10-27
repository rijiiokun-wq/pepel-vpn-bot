from pathlib import Path


def test_admin_templates_share_settings_style_shell():
    repo_root = Path(__file__).resolve().parents[1]
    base_template = (repo_root / "src" / "shop_bot" / "webhook_server" / "templates" / "base.html").read_text(
        encoding="utf-8"
    )
    dashboard_template = (repo_root / "src" / "shop_bot" / "webhook_server" / "templates" / "dashboard.html").read_text(
        encoding="utf-8"
    )
    users_template = (repo_root / "src" / "shop_bot" / "webhook_server" / "templates" / "users.html").read_text(
        encoding="utf-8"
    )
    login_template = (repo_root / "src" / "shop_bot" / "webhook_server" / "templates" / "login.html").read_text(
        encoding="utf-8"
    )
    debug_template = (
        repo_root / "src" / "shop_bot" / "webhook_server" / "templates" / "debug_settings.html"
    ).read_text(encoding="utf-8")
    css = (repo_root / "src" / "shop_bot" / "webhook_server" / "static" / "css" / "style.css").read_text(
        encoding="utf-8"
    )

    assert 'body class="admin-shell"' in base_template
    assert '<main class="admin-main">' in base_template

    assert 'class="admin-page-head"' in dashboard_template
    assert "admin-section-card" in dashboard_template
    assert 'class="table-scroll-shell"' in dashboard_template

    assert 'class="admin-page-head"' in users_template
    assert "admin-section-card users-page-panel" in users_template
    assert 'class="table-scroll-shell"' in users_template

    assert 'class="login-intro"' in login_template
    assert 'body class="admin-shell"' in debug_template
    assert 'class="admin-section-card debug-panel"' in debug_template

    required_css_snippets = [
        ".admin-main",
        "grid-template-columns: minmax(0, 1fr);",
        ".admin-main > *",
        ".admin-page-head",
        ".admin-section-card",
        ".table-scroll-shell",
        ".navigation .nav-link.active",
        ".login-intro",
        ".debug-grid",
        ".table-secondary-text",
        ".table-mono",
        ".host-check-row small,\n.host-check-row span",
        ".plan-list li > span",
        ".plan-list li small,\n.plan-list li code",
        "overflow-wrap: anywhere;",
    ]

    for snippet in required_css_snippets:
        assert snippet in css


def test_users_filters_toolbar_uses_structured_grid_and_summary():
    repo_root = Path(__file__).resolve().parents[1]
    users_template = (repo_root / "src" / "shop_bot" / "webhook_server" / "templates" / "users.html").read_text(
        encoding="utf-8"
    )
    css = (repo_root / "src" / "shop_bot" / "webhook_server" / "static" / "css" / "style.css").read_text(
        encoding="utf-8"
    )

    assert 'class="users-filter-grid"' in users_template
    assert 'class="users-toolbar-footer"' in users_template
    assert 'class="users-toolbar-summary"' in users_template
    assert 'class="users-toolbar-actions"' in users_template
    assert ".users-filter-grid" in css
    assert ".users-filter-label" in css
    assert ".users-toolbar-footer" in css
    assert ".users-toolbar-summary" in css
