"""Offline checks for the imported mobile and finance boundaries."""

import json

import pytest
from flask import Flask

from shop_bot import mobile_app
from shop_bot.data_manager import finance_live
from shop_bot.modules import xui_api
from shop_bot.webhook_server import subscriptions_finance


@pytest.mark.parametrize(
    ("key", "quota"),
    [
        ({"subscription_tier": "standard"}, 5 * 1024**3),
        ({"subscription_tier": "super"}, 40 * 1024**3),
        ({"subscription_tier": "super", "key_email": "fixture-trial-super@telegram.bot"}, 2 * 1024**3),
    ],
)
def test_imported_tiers_keep_distinct_whitelist_quotas(key, quota):
    assert xui_api.whitelist_traffic_limit_bytes_for_key(key) == quota


def test_mobile_approval_cannot_be_taken_over_or_replayed(tmp_path):
    store = mobile_app.AuthStore(tmp_path / "mobile.sqlite3", clock=lambda: 1000)
    request = store.start("Test device", "127.0.0.1")
    challenge = request["telegram_url"].split("app_", 1)[1]
    claimed = store.claim(challenge, 101, "Fixture user")
    with pytest.raises(mobile_app.MobileError):
        store.claim(challenge, 102, "Other user")
    with pytest.raises(mobile_app.MobileError):
        store.confirm(claimed["request_id"], 102)
    assert store.poll(request["request_id"], request["request_secret"]) is None
    store.confirm(claimed["request_id"], 101)
    session = store.poll(request["request_id"], request["request_secret"])
    assert store.authenticate(session["access_token"])["user_id"] == 101
    with pytest.raises(mobile_app.MobileError):
        store.poll(request["request_id"], request["request_secret"])
    with store.connection() as db:
        persisted = " ".join(str(tuple(row)) for table in ("sessions", "auth_requests")
                             for row in db.execute(f"SELECT * FROM {table}"))
    assert session["access_token"] not in persisted
    assert request["request_secret"] not in persisted
    assert challenge not in persisted
    store.logout(session["access_token"])
    with pytest.raises(mobile_app.MobileError):
        store.authenticate(session["access_token"])


def test_mobile_subscription_alias_requires_its_owner(tmp_path):
    store = mobile_app.AuthStore(tmp_path / "aliases.sqlite3")
    alias = store.alias_for(101, 201)
    assert store.key_for_alias(101, alias) == 201
    assert store.key_for_alias(102, alias) is None


@pytest.mark.parametrize("configured_id", ["", "fixture-shop"])
def test_finance_capture_rejects_unconfigured_or_different_shop(monkeypatch, configured_id):
    monkeypatch.setattr(finance_live, "SHOP_ID", configured_id)

    def unexpected_database_access(*args, **kwargs):
        pytest.fail("Untrusted shop must not access either database")

    monkeypatch.setattr(finance_live, "connect", unexpected_database_access)
    finance_live.capture_webhook({}, "unused-customers.db", "unused-history.json", shop_id="other-shop")


@pytest.mark.parametrize("configured_id", ["", "different-shop"])
def test_finance_mirror_missing_or_mismatched_shop_fails_closed(tmp_path, monkeypatch, configured_id):
    monkeypatch.setattr(subscriptions_finance, "SHOP_ID", configured_id)
    history = tmp_path / "history.json"
    (tmp_path / "subscriptions-webhook-secret.json").write_text(json.dumps({
        "shop_id": "fixture-shop", "additional_key": "synthetic-webhook-key",
    }))
    app = Flask(__name__)
    subscriptions_finance.register_subscriptions_finance(app, history_path=lambda: str(history))
    response = app.test_client().post(subscriptions_finance.ENDPOINT, json={"status": "success"})
    assert response.status_code == 503
    assert sorted(p.name for p in tmp_path.iterdir()) == ["subscriptions-webhook-secret.json"]
