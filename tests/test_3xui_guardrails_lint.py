from __future__ import annotations

import pytest

from scripts import lint_3xui_guardrails


def _synthetic_subscription_url() -> str:
    return f"https://example.invalid/sub/{'a' * 24}"


def _synthetic_panel_url() -> str:
    # RFC 5737 documentation address; this is not a reachable service.
    host = "192.0.2.10"
    return f"http://{host}:8443/{'synthetic' * 3}/"


def test_guardrails_lint_rejects_real_managed_subscription_url(tmp_path):
    leaked_url = _synthetic_subscription_url()
    (tmp_path / "example.md").write_text(f"Subscription: {leaked_url}\n", encoding="utf-8")
    errors = lint_3xui_guardrails.scan_repo(tmp_path)
    assert any("FULL_SUBSCRIPTION_URL" in error for error in errors)


def test_guardrails_lint_allows_managed_subscription_placeholder(tmp_path):
    (tmp_path / "example.md").write_text(
        "https://example.invalid/sub/<sub_id>\n", encoding="utf-8"
    )
    assert lint_3xui_guardrails.scan_repo(tmp_path) == []


def test_guardrails_lint_rejects_hidden_panel_url(tmp_path):
    hidden_url = _synthetic_panel_url()
    (tmp_path / "example.md").write_text(f"Panel: {hidden_url}\n", encoding="utf-8")
    errors = lint_3xui_guardrails.scan_repo(tmp_path)
    assert any("HIDDEN_PANEL_URL" in error for error in errors)


def test_guardrails_lint_rejects_ci_text_leaks(monkeypatch):
    leaked_url = _synthetic_subscription_url()
    monkeypatch.setenv("CI_MERGE_REQUEST_DESCRIPTION", f"Subscription: {leaked_url}")
    errors = lint_3xui_guardrails.scan_ci_context()
    assert any("FULL_SUBSCRIPTION_URL" in error for error in errors)
    assert all(leaked_url not in error for error in errors)


def test_guardrails_lint_rejects_full_vless_in_text_context(tmp_path):
    (tmp_path / "example.md").write_text(
        "vless://synthetic-credential@example.invalid:443?security=tls#synthetic\n",
        encoding="utf-8",
    )
    errors = lint_3xui_guardrails.scan_repo(tmp_path)
    assert any("FULL_VLESS_URI" in error for error in errors)


@pytest.mark.parametrize(
    ("value", "rule"),
    [
        (_synthetic_subscription_url(), "FULL_SUBSCRIPTION_URL"),
        (_synthetic_panel_url(), "HIDDEN_PANEL_URL"),
        ("vless://synthetic-credential@example.invalid:443", "FULL_VLESS_URI"),
        ("00000000-0000-4000-8000-000000000001", "REAL_UUID"),
    ],
)
def test_guardrails_cli_never_echoes_matched_values(tmp_path, monkeypatch, capsys, value, rule):
    for name in lint_3xui_guardrails.CI_TEXT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "example.md").write_text(value, encoding="utf-8")
    assert lint_3xui_guardrails.main([str(tmp_path)]) == 1
    output = capsys.readouterr()
    assert f"example.md:1 [{rule}]" in output.err
    assert value not in output.err
    assert value not in output.out
