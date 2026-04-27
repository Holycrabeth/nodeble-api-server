"""Release manifest fetcher — fetches https://nodeble.app/releases.json.

Phase A Week 2 per Phase 4.1 contract freeze §6 Q2 + Backend Director
plan Task A.12.

Cached 5 minutes. Failure mode: serve stale cache + set
manifest_unreachable=true. UI shows "更新检测不可用" badge gracefully.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import urllib.request
import urllib.error
import json
import logging


logger = logging.getLogger(__name__)

MANIFEST_URL = "https://nodeble.app/releases.json"
CACHE_TTL_SEC = 300  # 5 min
FETCH_TIMEOUT_SEC = 10


# In-memory cache (process-local; survives across requests, lost on restart)
_cache: dict[str, Any] = {
    "data": None,                  # parsed manifest dict
    "fetched_at": None,            # monotonic seconds
    "fetched_at_iso": None,        # ISO 8601 UTC when last fetched
    "manifest_unreachable": True,  # True until first successful fetch
}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_cache_fresh() -> bool:
    """True if last successful fetch was within CACHE_TTL_SEC."""
    if _cache["fetched_at"] is None:
        return False
    return (time.monotonic() - _cache["fetched_at"]) < CACHE_TTL_SEC


def fetch(force: bool = False) -> dict[str, Any]:
    """Fetch manifest. Returns cached if fresh + not forced.

    Returns dict matching Phase 4.1 contract §1.1 strategy-versions
    response shape:
      {manifest_url, fetched_at, manifest_unreachable, strategies}
    """
    if not force and _is_cache_fresh() and _cache["data"] is not None:
        return _build_response_from_cache()

    try:
        req = urllib.request.Request(
            MANIFEST_URL,
            headers={"User-Agent": "nodeble-api-server/1.0"},
        )
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SEC) as resp:
            if resp.status != 200:
                logger.warning(
                    "Manifest fetch returned %s, falling back to cache (stale)",
                    resp.status,
                )
                return _build_response_from_cache(unreachable=True)
            body_bytes = resp.read()
            data = json.loads(body_bytes.decode("utf-8"))
    except urllib.error.URLError as e:
        logger.warning("Manifest fetch URLError (%s) — fall back to cache", e)
        return _build_response_from_cache(unreachable=True)
    except json.JSONDecodeError as e:
        logger.warning("Manifest fetch returned invalid JSON: %s", e)
        return _build_response_from_cache(unreachable=True)
    except Exception as e:  # defensive — manifest fetcher is non-critical
        logger.warning("Manifest fetch unexpected error: %s — fall back", e)
        return _build_response_from_cache(unreachable=True)

    _cache["data"] = data
    _cache["fetched_at"] = time.monotonic()
    _cache["fetched_at_iso"] = _utc_iso()
    _cache["manifest_unreachable"] = False

    return _build_response_from_cache()


def _build_response_from_cache(unreachable: bool = False) -> dict[str, Any]:
    """Convert cached manifest to API response shape.

    If unreachable=True OR cache empty: return manifest_unreachable=True
    with whatever cached strategies dict we have (may be empty).
    """
    data = _cache.get("data")
    if data is None:
        return {
            "manifest_url": MANIFEST_URL,
            "fetched_at": _cache.get("fetched_at_iso") or _utc_iso(),
            "manifest_unreachable": True,
            "strategies": {},
        }

    return {
        "manifest_url": MANIFEST_URL,
        "fetched_at": _cache["fetched_at_iso"],
        "manifest_unreachable": unreachable or _cache.get("manifest_unreachable", False),
        "strategies": data.get("strategies", {}),
    }


def clear_cache() -> None:
    """Clear cache (test helper)."""
    _cache["data"] = None
    _cache["fetched_at"] = None
    _cache["fetched_at_iso"] = None
    _cache["manifest_unreachable"] = True
