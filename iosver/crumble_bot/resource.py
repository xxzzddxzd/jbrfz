"""Live game provisioning and resource manifest lookup.

The game first posts its current client version to DevPlay metadata, receives
the CDN ``resource_hash``, then downloads that hash's manifest.  Keep both
requests here so all login paths use the same value and old per-account keys
cannot leak into the first gRPC request.
"""
from __future__ import annotations

import json
import logging
from threading import Lock
from typing import Any

import httpx

from .constants import FALLBACK_RESOURCE_KEY

log = logging.getLogger(__name__)

RESOURCE_METADATA_URL = "https://account.devplay.com/v4/metadata"
RESOURCE_METADATA_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "*/*",
    "User-Agent": "CookieRunCrumble/2026081413 CFNetwork/1399 Darwin/22.1.0",
    "Accept-Language": "zh-CN,zh-Hans;q=0.9",
    "X-API-Key": "wUsUkXPVSujBcOt4mDJX",
    "X-Env": "prod",
    "X-Bundle-Id": "com.devsisters.cc",
    "X-SDK-Version": "1.6.3-hotfix1",
    "X-Unity-Version": "6000.3.15f1",
    "X-Platform": "",
    "X-Os-Name": "SU9T",
    "X-Os-Version": "MTYuMS4x",
    "X-Timezone": "QXNpYS9TaGFuZ2hhaQ==",
    "X-LocaleOnGame": "emgtSGFudA==",
    "X-Location-Country": "Q04=",
    "X-App-Version": "MS4xLjEwMQ==",
    "X-App-Build": "MjAyNjA4MTQxMw==",
}
RESOURCE_METADATA_BODY = {
    "device_type": "ios",
    "app_version": "1.1.101",
    "app_build": "2026081413",
}
RESOURCE_MANIFEST_BASE_URL = "https://cc.devscdn.com/cc/resource"
RESOURCE_MANIFEST_HEADERS = {
    "Accept": "*/*",
    "User-Agent": "CookieRunCrumble/2026081413 CFNetwork/1399 Darwin/22.1.0",
    "Accept-Language": "zh-CN,zh-Hans;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "X-Unity-Version": "6000.3.15f1",
}

_cache_lock = Lock()
_cached_resource_key: str | None = None


def parse_manifest_resource_key(payload: Any) -> str | None:
    """Extract and validate ``resource_key`` from a manifest response."""
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = json.loads(bytes(payload).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
    elif isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    key = payload.get("resource_key")
    if not isinstance(key, str):
        return None
    key = key.strip()
    if not key.startswith("game-data-") or len(key) <= len("game-data-"):
        return None
    return key


def parse_metadata_resource_hash(payload: Any) -> str | None:
    """Extract the CDN resource hash from the metadata response."""
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = json.loads(bytes(payload).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
    elif isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    resource_hash = payload.get("resource_hash")
    if not isinstance(resource_hash, str):
        return None
    resource_hash = resource_hash.strip()
    return resource_hash or None


def manifest_url(resource_hash: str) -> str:
    """Build the CDN manifest URL returned by DevPlay metadata."""
    return f"{RESOURCE_MANIFEST_BASE_URL}/{resource_hash}/manifest.json"


def fetch_resource_key(*, timeout: float = 30.0, force: bool = False) -> str:
    """Fetch the current key once per command process.

    A manifest outage must not prevent a login when the last known live key is
    still valid, so failures fall back to ``FALLBACK_RESOURCE_KEY``.
    """
    global _cached_resource_key
    with _cache_lock:
        if _cached_resource_key and not force:
            return _cached_resource_key

        try:
            with httpx.Client(timeout=timeout, verify=True) as client:
                metadata_response = client.post(
                    RESOURCE_METADATA_URL,
                    headers=RESOURCE_METADATA_HEADERS,
                    json=RESOURCE_METADATA_BODY,
                )
                metadata_response.raise_for_status()
                resource_hash = parse_metadata_resource_hash(
                    metadata_response.content
                )
                if not resource_hash:
                    raise RuntimeError(
                        "metadata response has no usable resource_hash"
                    )
                log.debug("resource_hash <- metadata (%s)", resource_hash)

                response = client.get(
                    manifest_url(resource_hash),
                    headers=RESOURCE_MANIFEST_HEADERS,
                )
                response.raise_for_status()
                key = parse_manifest_resource_key(response.content)
        except Exception as error:  # network errors should not block login
            log.warning("resource manifest lookup failed: %s", error)
            key = None

        if key:
            _cached_resource_key = key
            log.info("resource_key <- manifest (%s)", key)
            return key

        return FALLBACK_RESOURCE_KEY


def clear_resource_key_cache() -> None:
    """Reset the process cache (used by tests and explicit refreshes)."""
    global _cached_resource_key
    with _cache_lock:
        _cached_resource_key = None
