"""Tests for release_manifest fetcher — Phase A Week 2.

Pin: cache TTL respected, manifest_unreachable=true on fetch failure
(graceful fallback), fresh fetch returns spec-exact response shape.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from nodeble_api_server import release_manifest


@pytest.fixture(autouse=True)
def _reset_cache():
    release_manifest.clear_cache()
    yield
    release_manifest.clear_cache()


def test_fetch_returns_unreachable_when_cache_empty_and_fetch_fails():
    """Fresh process + manifest unreachable → manifest_unreachable=true with empty strategies."""
    with patch("nodeble_api_server.release_manifest.urllib.request.urlopen") as mock_open:
        mock_open.side_effect = OSError("network down")
        result = release_manifest.fetch()

    assert result["manifest_unreachable"] is True
    assert result["strategies"] == {}
    assert result["manifest_url"] == "https://nodeble.app/releases.json"


def test_fetch_caches_successful_result():
    """Second fetch within TTL returns cached without re-hitting network."""
    fake_response_body = b'{"strategies": {"wheel": {"latest": "0.7.2", "released_at": "2026-04-20T00:00:00Z", "changelog_url": "https://gh.com/x"}}}'

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = fake_response_body
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("nodeble_api_server.release_manifest.urllib.request.urlopen", return_value=mock_resp) as mock_open:
        # First fetch — hits network
        result1 = release_manifest.fetch()
        assert result1["manifest_unreachable"] is False
        assert "wheel" in result1["strategies"]
        assert mock_open.call_count == 1

        # Second fetch — cache hit, no new network call
        result2 = release_manifest.fetch()
        assert result2["strategies"] == result1["strategies"]
        assert mock_open.call_count == 1, "Second fetch should be cached"


def test_fetch_force_bypasses_cache():
    """force=True re-fetches even if cache fresh."""
    fake_body = b'{"strategies": {"wheel": {"latest": "0.7.2", "released_at": "x", "changelog_url": "x"}}}'

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = fake_body
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("nodeble_api_server.release_manifest.urllib.request.urlopen", return_value=mock_resp) as mock_open:
        release_manifest.fetch()
        release_manifest.fetch(force=True)
        assert mock_open.call_count == 2, "force=True should bypass cache"


def test_fetch_falls_back_to_stale_cache_on_failure():
    """If we have cached data and a re-fetch fails, return cached + manifest_unreachable=true."""
    fake_body = b'{"strategies": {"wheel": {"latest": "0.7.2", "released_at": "x", "changelog_url": "x"}}}'

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = fake_body
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)

    # First successful fetch caches
    with patch("nodeble_api_server.release_manifest.urllib.request.urlopen", return_value=mock_resp):
        release_manifest.fetch()

    # Force re-fetch with failure — should fall back to cache
    with patch("nodeble_api_server.release_manifest.urllib.request.urlopen") as mock_fail:
        mock_fail.side_effect = OSError("transient network error")
        result = release_manifest.fetch(force=True)
        assert result["manifest_unreachable"] is True
        assert "wheel" in result["strategies"]  # stale cached data still served


def test_fetch_handles_non_200_status():
    """503 / 404 from manifest URL → manifest_unreachable=true."""
    mock_resp = MagicMock()
    mock_resp.status = 503
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("nodeble_api_server.release_manifest.urllib.request.urlopen", return_value=mock_resp):
        result = release_manifest.fetch()
    assert result["manifest_unreachable"] is True


def test_fetch_handles_invalid_json():
    """Manifest returns non-JSON body → manifest_unreachable=true."""
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = b"not a json file"
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("nodeble_api_server.release_manifest.urllib.request.urlopen", return_value=mock_resp):
        result = release_manifest.fetch()
    assert result["manifest_unreachable"] is True


def test_response_shape_matches_contract_freeze_spec():
    """Response always has manifest_url + fetched_at + manifest_unreachable + strategies keys."""
    # Even on fail, shape is consistent
    with patch("nodeble_api_server.release_manifest.urllib.request.urlopen") as mock_open:
        mock_open.side_effect = OSError("down")
        result = release_manifest.fetch()

    required_keys = {"manifest_url", "fetched_at", "manifest_unreachable", "strategies"}
    assert set(result.keys()) >= required_keys
    assert isinstance(result["strategies"], dict)
