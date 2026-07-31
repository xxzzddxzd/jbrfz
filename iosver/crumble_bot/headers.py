"""gRPC metadata builder matching MetadataProvider.CreateHeaders."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Dict, Mapping, Optional

from .constants import FALLBACK_RESOURCE_KEY


@dataclass
class DeviceIds:
    fgs_id: str = ""
    anonymous_id: str = ""
    app_installed_id: str = ""
    devsisters_id: str = ""
    semi_device_id: str = ""


@dataclass
class Session:
    mid: str
    game_access_token: str
    resource_key: str = FALLBACK_RESOURCE_KEY
    device: Optional[DeviceIds] = None

    def adopt_resource_key(self, headers: Mapping[str, str] | None) -> bool:
        """Update resource_key from gRPC response header when server provides it."""
        if not headers:
            return False
        # httpx lower-cases header names
        rk = headers.get("crumble-resource-key") or headers.get("Crumble-Resource-Key")
        if not rk:
            return False
        rk = str(rk).strip()
        if not rk or rk == self.resource_key:
            return False
        self.resource_key = rk
        return True


def build_metadata(session: Session) -> Dict[str, str]:
    device = session.device or DeviceIds()
    now_ms = int(time.time() * 1000)
    meta = {
        "crumble-user-id": session.mid,
        "crumble-resource-key": session.resource_key,
        "request-time": str(now_ms),
        "crumble-request-id": str(uuid.uuid4()),
        "crumble-access-token": session.game_access_token,
    }
    if device.fgs_id:
        meta["dev-play-fgs-id"] = device.fgs_id
    if device.anonymous_id:
        meta["dev-play-anonymous-id"] = device.anonymous_id
    if device.app_installed_id:
        meta["dev-play-app-installed-id"] = device.app_installed_id
    if device.devsisters_id:
        meta["dev-play-devsisters-id"] = device.devsisters_id
    if device.semi_device_id:
        meta["dev-play-semi-device-id"] = device.semi_device_id
    return meta


def with_request_time_override(meta: Dict[str, str], millis: int) -> Dict[str, str]:
    out = dict(meta)
    out["request-time"] = str(millis)
    out["crumble-request-id"] = str(uuid.uuid4())
    return out
