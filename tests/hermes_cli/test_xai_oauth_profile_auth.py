"""Regression tests for xAI OAuth auth resolution in profile/cron contexts."""

import pytest

from hermes_cli import auth
from hermes_cli.auth import AuthError


def test_read_xai_oauth_tokens_uses_credential_pool_when_provider_tokens_empty(monkeypatch):
    """Profile auth can have fresh pool tokens while singleton provider state is empty.

    This mirrors profiled cron after re-auth/credential-pool sync: the xAI
    OAuth credential is usable, but `providers.xai-oauth.tokens` may be empty
    or stale. Treating that as missing auth makes cron keep failing after the
    user has successfully re-authenticated.
    """
    store = {
        "providers": {"xai-oauth": {"tokens": {}, "last_auth_error": {}}},
        "credential_pool": {
            "xai-oauth": [
                {
                    "access_token": "pool-access",
                    "refresh_token": "pool-refresh",
                    "token_type": "Bearer",
                    "last_refresh": "2026-06-03T19:00:00Z",
                }
            ]
        },
    }
    monkeypatch.setattr(auth, "_load_auth_store", lambda: store)
    monkeypatch.setattr(auth, "_load_global_auth_store", lambda: {})

    resolved = auth._read_xai_oauth_tokens(_lock=False)

    assert resolved["tokens"]["access_token"] == "pool-access"
    assert resolved["tokens"]["refresh_token"] == "pool-refresh"
    assert resolved["tokens"]["token_type"] == "Bearer"
    assert resolved["last_refresh"] == "2026-06-03T19:00:00Z"

def test_read_xai_oauth_tokens_allows_access_only_managed_delivery(monkeypatch):
    """PiB multi-device delivery stores access without refresh_token.

    Hermes must still treat a non-empty access JWT as usable credentials so a
    mid-chat credential resync does not hard-fail while the access token is
    still valid.
    """
    store = {
        "providers": {
            "xai-oauth": {
                "tokens": {
                    "access_token": "access-only",
                    "token_type": "Bearer",
                    "scope": "openid profile email offline_access",
                },
                "auth_mode": "oauth_device_code",
                "source": "pib-web-account",
            }
        }
    }
    monkeypatch.setattr(auth, "_load_auth_store", lambda: store)
    monkeypatch.setattr(auth, "_load_global_auth_store", lambda: {})

    resolved = auth._read_xai_oauth_tokens(_lock=False)

    assert resolved["tokens"]["access_token"] == "access-only"
    assert not str(resolved["tokens"].get("refresh_token") or "").strip()


def test_resolve_xai_runtime_access_only_uses_valid_jwt(tmp_path, monkeypatch):
    import base64
    import json
    import time
    from hermes_cli.auth import resolve_xai_oauth_runtime_credentials

    def jwt(exp):
        payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
        return f"h.{payload}.s"

    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    access = jwt(int(time.time()) + 3600)
    (hermes_home / "auth.json").write_text(json.dumps({
        "version": 1,
        "providers": {
            "xai-oauth": {
                "tokens": {"access_token": access, "token_type": "Bearer"},
                "auth_mode": "oauth_device_code",
            }
        },
    }))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    creds = resolve_xai_oauth_runtime_credentials()
    assert creds["api_key"] == access
    assert creds["provider"] == "xai-oauth"


def test_resolve_xai_runtime_access_only_expired_fails_clearly(tmp_path, monkeypatch):
    import base64
    import json
    import time
    from hermes_cli.auth import resolve_xai_oauth_runtime_credentials

    def jwt(exp):
        payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
        return f"h.{payload}.s"

    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    access = jwt(int(time.time()) - 30)
    (hermes_home / "auth.json").write_text(json.dumps({
        "version": 1,
        "providers": {
            "xai-oauth": {
                "tokens": {"access_token": access, "token_type": "Bearer"},
                "auth_mode": "oauth_device_code",
            }
        },
    }))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    with pytest.raises(AuthError) as exc:
        resolve_xai_oauth_runtime_credentials()
    assert exc.value.code == "xai_auth_access_expired_no_refresh"
    assert exc.value.relogin_required is False

