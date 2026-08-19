"""Download and decode the live patch-data tables used by red-dot rules."""
from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass
from typing import Any

import httpx
import pyzipper

from .resource import (
    RESOURCE_MANIFEST_BASE_URL,
    RESOURCE_MANIFEST_HEADERS,
    RESOURCE_METADATA_BODY,
    RESOURCE_METADATA_HEADERS,
    RESOURCE_METADATA_URL,
    manifest_url,
    parse_manifest_resource_key,
    parse_metadata_resource_hash,
)

_ZIP_PASSWORD_SALT = "Q29va2llUnVuQ3J1bWJsZQ=="


@dataclass(frozen=True)
class PatchData:
    resource_hash: str
    resource_key: str
    tables: dict[str, Any]

    def rows(self, name: str) -> list[dict[str, Any]]:
        value = self.tables.get(name)
        if not isinstance(value, list):
            return []
        return [row for row in value if isinstance(row, dict)]


def compute_zip_password(resource_key: str) -> str:
    """Match ``ZipFileReader.ComputeZipPassword`` from the game client."""
    value = f"{resource_key}{_ZIP_PASSWORD_SALT}".encode("ascii")
    return hashlib.sha1(value).hexdigest().upper()  # noqa: S324 - game format


def fetch_patch_data(*, timeout: float = 60.0) -> PatchData:
    """Fetch metadata, manifest and the encrypted live patch table bundle."""
    with httpx.Client(timeout=timeout, verify=True) as client:
        metadata_response = client.post(
            RESOURCE_METADATA_URL,
            headers=RESOURCE_METADATA_HEADERS,
            json=RESOURCE_METADATA_BODY,
        )
        metadata_response.raise_for_status()
        resource_hash = parse_metadata_resource_hash(metadata_response.content)
        if not resource_hash:
            raise RuntimeError("metadata response has no resource_hash")

        manifest_response = client.get(
            manifest_url(resource_hash),
            headers=RESOURCE_MANIFEST_HEADERS,
        )
        manifest_response.raise_for_status()
        manifest = manifest_response.json()
        resource_key = parse_manifest_resource_key(manifest)
        if not resource_key:
            raise RuntimeError("resource manifest has no resource_key")

        file_info = next(
            (
                row
                for row in manifest.get("files", [])
                if isinstance(row, dict) and row.get("path") == "patch_binary_set"
            ),
            None,
        )
        if file_info is None:
            raise RuntimeError("resource manifest has no patch_binary_set")

        archive_response = client.get(
            f"{RESOURCE_MANIFEST_BASE_URL}/{resource_hash}/patch_binary_set",
            headers=RESOURCE_MANIFEST_HEADERS,
        )
        archive_response.raise_for_status()
        archive = archive_response.content

    expected_size = int(file_info.get("size") or 0)
    if expected_size and len(archive) != expected_size:
        raise RuntimeError(
            f"patch_binary_set size mismatch: {len(archive)} != {expected_size}"
        )
    password = compute_zip_password(resource_key).encode("ascii")
    with pyzipper.AESZipFile(io.BytesIO(archive)) as zip_file:
        zip_file.pwd = password
        raw = zip_file.read("resource.json")
    tables = json.loads(raw)
    if not isinstance(tables, dict):
        raise RuntimeError("resource.json is not an object")
    return PatchData(
        resource_hash=resource_hash,
        resource_key=resource_key,
        tables=tables,
    )
