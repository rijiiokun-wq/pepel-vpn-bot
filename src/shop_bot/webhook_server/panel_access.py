"""Server-validated section access for additional web-panel accounts.

Owner sessions retain the existing authorization path. Scoped cookies contain
only an account reference and revocation version; permissions always come from
the current server-side setting, never from a cookie or request parameter.
"""

import hmac
import json
import re

from flask import abort, g, redirect, request, session, url_for
from werkzeug.security import check_password_hash

ACCOUNTS_SETTING = 'panel_scoped_accounts'
_SECTIONS = frozenset({'dashboard', 'finance', 'referrals'})
_USERNAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', re.ASCII)
_AUTH_VERSION = re.compile(r'[A-Za-z0-9_.:-]{1,128}', re.ASCII)
_SCRYPT_HASH = re.compile(r'scrypt:32768:8:1\$[A-Za-z0-9]{8,64}\$[a-f0-9]{128}', re.ASCII)
_READ = frozenset({'GET', 'HEAD'})
_COMMON = {
    'index': _READ,
    'login_page': frozenset({'GET', 'HEAD', 'POST'}),
    'logout_page': frozenset({'POST'}),
    'static': _READ,
}
_SECTION_ENDPOINTS = {
    'dashboard': {
        'dashboard_page': _READ,
        'dashboard_live_signature_api': _READ,
        'dashboard_export_csv': _READ,
    },
    'finance': {
        'finance_page': _READ,
        'pepel_balance_fragment': _READ,
    },
    'referrals': {
        'referrals_page': _READ,
        'referral_notification_status_route': _READ,
        'referral_settings_route': frozenset({'POST'}),
        'referral_withdrawal_route': frozenset({'POST'}),
        'referral_notification_retry_route': frozenset({'POST'}),
    },
}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate account configuration field')
        result[key] = value
    return result


def _parse_accounts(raw):
    if not isinstance(raw, str) or not raw or len(raw) > 65536:
        return None
    try:
        data = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, TypeError, RecursionError):
        return None
    if (not isinstance(data, dict) or set(data) != {'version', 'accounts'}
            or type(data['version']) is not int or data['version'] != 1
            or not isinstance(data['accounts'], list) or len(data['accounts']) > 100):
        return None
    accounts = {}
    fields = {'username', 'password_hash', 'auth_version', 'sections', 'enabled'}
    for item in data['accounts']:
        if not isinstance(item, dict) or set(item) != fields:
            return None
        username, auth_version = item['username'], item['auth_version']
        password_hash, sections = item['password_hash'], item['sections']
        if (not isinstance(username, str) or not _USERNAME.fullmatch(username)
                or not isinstance(auth_version, str) or not _AUTH_VERSION.fullmatch(auth_version)
                or not isinstance(password_hash, str) or not _SCRYPT_HASH.fullmatch(password_hash)
                or type(item['enabled']) is not bool or not isinstance(sections, list)
                or len(sections) > len(_SECTIONS)
                or any(not isinstance(section, str) or section not in _SECTIONS for section in sections)):
            return None
        if len(set(sections)) != len(sections) or username.casefold() in accounts:
            return None
        accounts[username.casefold()] = item
    return accounts


def _load_accounts(get_setting):
    try:
        return _parse_accounts(get_setting(ACCOUNTS_SETTING))
    except Exception:  # noqa: BLE001 - an unavailable ACL must never grant access.
        return None


def _safe_account(account):
    return {'username': account['username'], 'auth_version': account['auth_version'],
            'sections': list(account['sections'])}


def authenticate_scoped_account(username, password, get_setting):
    """Check bounded credentials against a strict, enabled scrypt account."""
    if (not isinstance(username, str) or len(username) > 128
            or not isinstance(password, str) or not 1 <= len(password) <= 1024):
        return None
    username = username.strip()
    if not _USERNAME.fullmatch(username):
        return None
    accounts = _load_accounts(get_setting)
    account = accounts.get(username.casefold()) if accounts is not None else None
    if account is None or not account['enabled']:
        return None
    try:
        authenticated = check_password_hash(account['password_hash'], password)
    except (ValueError, TypeError, UnicodeError):
        return None
    return _safe_account(account) if authenticated else None


def start_scoped_session(account):
    """Store no permissions or credentials in the session; caller clears it first."""
    if (not isinstance(account, dict) or set(account) != {'username', 'auth_version', 'sections'}
            or not isinstance(account['username'], str) or not _USERNAME.fullmatch(account['username'])
            or not isinstance(account['auth_version'], str) or not _AUTH_VERSION.fullmatch(account['auth_version'])
            or not isinstance(account['sections'], list)
            or any(not isinstance(section, str) or section not in _SECTIONS for section in account['sections'])
            or len(set(account['sections'])) != len(account['sections'])):
        raise ValueError('Validated scoped account required')
    session['scoped_admin'] = {'username': account['username'], 'auth_version': account['auth_version']}


def _resolve_session_account(get_setting):
    if session.get('logged_in') is not True:
        return None
    marker = session.get('scoped_admin')
    if (not isinstance(marker, dict) or set(marker) != {'username', 'auth_version'}
            or not isinstance(marker['username'], str) or not _USERNAME.fullmatch(marker['username'])
            or not isinstance(marker['auth_version'], str) or not _AUTH_VERSION.fullmatch(marker['auth_version'])):
        return None
    accounts = _load_accounts(get_setting)
    account = accounts.get(marker['username'].casefold()) if accounts is not None else None
    if (account is None or not account['enabled']
            or not hmac.compare_digest(account['auth_version'], marker['auth_version'])):
        return None
    return _safe_account(account)


def register_panel_access(flask_app, *, get_setting):
    """Deny unlisted scoped requests before handlers, preserving owner behavior."""
    if 'panel_scoped_access' in flask_app.extensions:
        raise RuntimeError('Panel access already registered')
    flask_app.extensions['panel_scoped_access'] = True

    @flask_app.before_request
    def validate_panel_access():
        if 'scoped_admin' not in session:
            return None
        account = _resolve_session_account(get_setting)
        if account is None:
            session.clear()
            return redirect(url_for('login_page'))
        g.panel_scoped_account = account
        methods = _COMMON.get(request.endpoint, frozenset())
        if request.method in methods:
            return None
        for section in account['sections']:
            if request.method in _SECTION_ENDPOINTS[section].get(request.endpoint, frozenset()):
                return None
        return abort(403)

    @flask_app.context_processor
    def panel_access_context():
        restricted = 'scoped_admin' in session
        account = getattr(g, 'panel_scoped_account', None)
        if restricted and account is None:
            account = _resolve_session_account(get_setting)
        allowed = frozenset(account['sections']) if account else frozenset()

        def can_access(section):
            return not restricted or (isinstance(section, str) and section in allowed)

        return {'panel_restricted': restricted, 'panel_can_access': can_access}
