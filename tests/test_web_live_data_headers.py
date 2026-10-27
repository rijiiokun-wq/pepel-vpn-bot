from pathlib import Path


def test_web_app_sets_no_store_headers_for_dynamic_get_routes():
    app_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "shop_bot"
        / "webhook_server"
        / "app.py"
    )
    app_source = app_path.read_text(encoding="utf-8")

    assert "@flask_app.after_request" in app_source
    assert 'response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"' in app_source
    assert 'response.headers["Pragma"] = "no-cache"' in app_source
    assert 'response.headers["Expires"] = "0"' in app_source

