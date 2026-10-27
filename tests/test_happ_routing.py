from __future__ import annotations

import base64
import json
from copy import deepcopy

import pytest
from flask import Flask, Response

from shop_bot import happ_routing

SUBSCRIPTION_BODY = b"b2ZmbGluZS1zdWJzY3JpcHRpb24=\n"
SUBSCRIPTION_ID = "offline-subscription"
EXISTING_ROUTING = "happ://routing/existing-profile"


@pytest.fixture
def profile():
    return {
        "Name": "Офлайн профиль",
        "DirectSites": ["domain:example.test"],
        "ProxySites": ["domain:proxy.example.test"],
        "DirectIp": [],
        "Tags": {"source": "offline"},
    }


def _override(profile, **extra):
    return json.dumps({"enabled": True, "profile": profile, **extra}, ensure_ascii=False)


def _decode_profile(header):
    prefix = "happ://routing/onadd/"
    assert header.startswith(prefix)
    return json.loads(base64.b64decode(header.removeprefix(prefix), validate=True))


def _subscription_response(read_setting, *, status=200, mimetype="text/plain", managed=True):
    app = Flask(__name__)

    def deliver_subscription(sub_id):
        return Response(
            SUBSCRIPTION_BODY,
            status=status,
            mimetype=mimetype,
            headers={"routing": EXISTING_ROUTING, "Cache-Control": "public, max-age=60"},
        )

    app.add_url_rule(
        "/subscription/<sub_id>",
        endpoint="managed_subscription_route" if managed else "unrelated_route",
        view_func=deliver_subscription,
    )
    happ_routing.register_subscription_routing(app, read_setting)
    return app.test_client().get("/subscription/" + SUBSCRIPTION_ID)


def test_profile_header_round_trips_unicode_and_preserves_profile(profile):
    original = deepcopy(profile)

    header = happ_routing.routing_header(_override(profile))

    assert header is not None
    assert len(header.encode("ascii")) <= 4096
    assert _decode_profile(header) == original
    assert profile == original


def test_invalid_or_oversized_override_preserves_existing_subscription_routing(profile):
    malformed_profiles = [
        "not JSON",
        json.dumps({"enabled": 1, "profile": profile}),
        _override({**profile, "DirectSites": "example.test"}),
        _override({**profile, "DirectSites": ["x" * 10000]}),
    ]
    for raw in malformed_profiles:
        settings = {
            happ_routing.setting_key(SUBSCRIPTION_ID): raw,
            happ_routing.DEFAULT_SETTING: _override(profile),
        }

        response = _subscription_response(settings.get)

        assert response.data == SUBSCRIPTION_BODY
        assert response.headers["routing"] == EXISTING_ROUTING
        assert response.headers["Cache-Control"] == "public, max-age=60"


def test_explicit_disabled_override_does_not_fall_back_to_shared_default(profile):
    settings = {
        happ_routing.setting_key(SUBSCRIPTION_ID): json.dumps({"enabled": False}),
        happ_routing.DEFAULT_SETTING: _override(profile),
    }

    response = _subscription_response(settings.get)

    assert response.data == SUBSCRIPTION_BODY
    assert response.headers["routing"] == EXISTING_ROUTING


def test_shared_default_adds_domain_routing_without_paid_app_management(profile):
    settings = {
        happ_routing.DEFAULT_SETTING: _override(
            profile, provider_id="offline-provider", android_bypass_packages=["org.example.app"]
        )
    }

    response = _subscription_response(settings.get)

    assert response.data == SUBSCRIPTION_BODY
    assert _decode_profile(response.headers["routing"]) == profile
    assert "providerid" not in response.headers
    assert "per-app-proxy-mode" not in response.headers
    assert "per-app-proxy-list" not in response.headers
    assert "private" in response.headers["Cache-Control"]
    assert "no-store" in response.headers["Cache-Control"]


def test_private_override_selects_its_profile_and_deduplicates_valid_android_apps(profile):
    settings = {
        happ_routing.DEFAULT_SETTING: _override({**profile, "Name": "Shared profile"}),
        happ_routing.setting_key(SUBSCRIPTION_ID): _override(
            profile,
            provider_id="offline-provider",
            android_bypass_packages=["org.example.first", "org.example.second", "org.example.first"],
        ),
    }

    response = _subscription_response(settings.get)

    assert response.data == SUBSCRIPTION_BODY
    assert _decode_profile(response.headers["routing"]) == profile
    assert response.headers["providerid"] == "offline-provider"
    assert response.headers["per-app-proxy-mode"] == "bypass"
    assert response.headers["per-app-proxy-list"] == "org.example.first,org.example.second"


def test_routing_hook_ignores_other_endpoints_error_responses_and_non_subscription_types():
    def unexpected_setting_read(_name):
        pytest.fail("Non-subscription responses must not read private routing settings")

    for options in [{"managed": False}, {"status": 503}, {"mimetype": "application/json"}]:
        response = _subscription_response(unexpected_setting_read, **options)

        assert response.data == SUBSCRIPTION_BODY
        assert response.headers["routing"] == EXISTING_ROUTING
        assert response.headers["Cache-Control"] == "public, max-age=60"


def test_settings_failure_keeps_subscription_and_private_details_out_of_logs(caplog):
    def unavailable_setting(_name):
        raise RuntimeError("offline-private-diagnostic-sentinel")

    response = _subscription_response(unavailable_setting)

    assert response.data == SUBSCRIPTION_BODY
    assert response.headers["routing"] == EXISTING_ROUTING
    assert "offline-private-diagnostic-sentinel" not in caplog.text
    assert SUBSCRIPTION_ID not in caplog.text
