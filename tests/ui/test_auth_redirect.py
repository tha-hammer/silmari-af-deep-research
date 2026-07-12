"""Unit tests for the auth-surface allowlist and the browser-navigation
discriminator that drives the expired-session -> /login redirect (kills the raw
SuperTokens ``try refresh token`` JSON on full-page loads)."""

from __future__ import annotations

from ui.app import _is_browser_navigation, _is_public_auth_path


def test_public_auth_paths_exact_and_prefix():
    assert _is_public_auth_path("/login")
    assert _is_public_auth_path("/login/reset-password")
    assert _is_public_auth_path("/auth")
    assert _is_public_auth_path("/auth/session/refresh")


def test_non_auth_paths_are_not_public():
    for p in ("/", "/defaults", "/api/runs", "/x/login", "/loginx"):
        assert not _is_public_auth_path(p)


def test_navigation_detected_by_sec_fetch_dest():
    # A top-level document load is a navigation regardless of Accept.
    assert _is_browser_navigation("document", "*/*") is True
    # A fetch()/XHR (dest=empty) is not, even with a text/html Accept.
    assert _is_browser_navigation("empty", "text/html") is False


def test_navigation_falls_back_to_accept_when_no_fetch_metadata():
    assert _is_browser_navigation("", "text/html,application/xhtml+xml") is True
    assert _is_browser_navigation("", "application/json") is False
    # Ambiguous (both) -> treat as XHR so the SPA's own guard handles the 401.
    assert _is_browser_navigation("", "text/html,application/json") is False
    assert _is_browser_navigation("", "*/*") is False
    assert _is_browser_navigation("", "") is False
