"""Account session helpers + DevPlay guest login.

Valid HTTP entry (from Charles capture 2026-07-31):
  POST https://account.devplay.com/v3/login
  - guest_secret:""  → create new guest (returns mid + guest_secret + tokens)
  - guest_secret:<s> → re-login existing guest (refresh tokens)
"""
from __future__ import annotations

import base64
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from .constants import ENDPOINT as DEFAULT_ENDPOINT
from .constants import FALLBACK_RESOURCE_KEY as DEFAULT_RESOURCE_KEY
from .constants import normalize_resource_key
from .resource import fetch_resource_key

from .headers import DeviceIds, Session

LOGIN_URL = "https://account.devplay.com/v3/login"
API_KEY = "wUsUkXPVSujBcOt4mDJX"

# App constants from capture (CookieRun Crumble iOS)
APP_BUILD = "2026081413"
APP_VERSION = "1.1.101"
SDK_VERSION = "1.6.3-hotfix1"
BUNDLE_ID = "com.devsisters.cc"
UNITY_VERSION = "6000.3.15f1"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


# Real iPhone identifier + supported iOS ranges (inclusive-ish pools).
# model id: Apple productType (utsname.machine).
_IOS_DEVICE_PROFILES: tuple[dict[str, object], ...] = (
    # iPhone 12 family — iOS 14.1+ ; common still on 16/17
    {"model": "iPhone13,2", "name": "iPhone 12", "ios": ("15.7.9", "16.1.1", "16.6.1", "16.7.10", "17.0.3", "17.5.1")},
    {"model": "iPhone13,3", "name": "iPhone 12 Pro", "ios": ("15.7.9", "16.1.2", "16.6.1", "17.1.1", "17.5.1", "17.6.1")},
    {"model": "iPhone13,4", "name": "iPhone 12 Pro Max", "ios": ("15.8.2", "16.3.1", "16.7.5", "17.2.1", "17.5.1", "18.0.1")},
    # iPhone 13 family
    {"model": "iPhone14,5", "name": "iPhone 13", "ios": ("15.7.1", "16.1.1", "16.5.1", "17.0.3", "17.4.1", "17.6.1", "18.1")},
    {"model": "iPhone14,2", "name": "iPhone 13 Pro", "ios": ("15.6.1", "16.2", "16.6", "17.1.2", "17.5.1", "18.0", "18.1.1")},
    {"model": "iPhone14,3", "name": "iPhone 13 Pro Max", "ios": ("15.7.9", "16.1.1", "16.7.2", "17.2", "17.5.1", "17.6.1", "18.1")},
    {"model": "iPhone14,4", "name": "iPhone 13 mini", "ios": ("15.7.9", "16.3.1", "16.7.8", "17.4.1", "17.6.1", "18.0.1")},
    # iPhone 14 family
    {"model": "iPhone14,7", "name": "iPhone 14", "ios": ("16.0.2", "16.3.1", "16.6.1", "17.1.1", "17.5.1", "18.0", "18.1.1")},
    {"model": "iPhone14,8", "name": "iPhone 14 Plus", "ios": ("16.0.3", "16.5", "17.0.2", "17.4.1", "17.6.1", "18.1")},
    {"model": "iPhone15,2", "name": "iPhone 14 Pro", "ios": ("16.0.2", "16.4.1", "16.7.1", "17.2.1", "17.5.1", "18.0.1", "18.1.1")},
    {"model": "iPhone15,3", "name": "iPhone 14 Pro Max", "ios": ("16.1", "16.5.1", "17.0.3", "17.4.1", "17.6.1", "18.1", "18.2")},
    # iPhone 15 family
    {"model": "iPhone15,4", "name": "iPhone 15", "ios": ("17.0.2", "17.1.1", "17.4.1", "17.5.1", "17.6.1", "18.0", "18.1.1")},
    {"model": "iPhone15,5", "name": "iPhone 15 Plus", "ios": ("17.0.3", "17.2.1", "17.5.1", "18.0.1", "18.1", "18.2")},
    {"model": "iPhone16,1", "name": "iPhone 15 Pro", "ios": ("17.0.3", "17.1.2", "17.4.1", "17.5.1", "17.6.1", "18.0", "18.1.1", "18.2")},
    {"model": "iPhone16,2", "name": "iPhone 15 Pro Max", "ios": ("17.0.3", "17.2", "17.5.1", "17.6.1", "18.0.1", "18.1.1", "18.2")},
    # iPhone 16 family
    {"model": "iPhone17,3", "name": "iPhone 16", "ios": ("18.0", "18.0.1", "18.1", "18.1.1", "18.2", "18.2.1")},
    {"model": "iPhone17,4", "name": "iPhone 16 Plus", "ios": ("18.0", "18.0.1", "18.1", "18.1.1", "18.2")},
    {"model": "iPhone17,1", "name": "iPhone 16 Pro", "ios": ("18.0.1", "18.1", "18.1.1", "18.2", "18.2.1")},
    {"model": "iPhone17,2", "name": "iPhone 16 Pro Max", "ios": ("18.0.1", "18.1", "18.1.1", "18.2", "18.2.1")},
)

# Darwin version roughly paired with major iOS for UA (not exact patch mapping).
_IOS_MAJOR_TO_DARWIN = {
    15: "21.6.0",
    16: "22.1.0",
    17: "23.1.0",
    18: "24.1.0",
}


def _pick_ios_device_profile(
    *,
    model: str | None = None,
    os_version: str | None = None,
) -> dict[str, str]:
    """Pick a real iPhone model + compatible real iOS version."""
    import random

    if model and os_version:
        return {"model": model, "os_version": os_version, "name": model}

    profiles = list(_IOS_DEVICE_PROFILES)
    if model:
        matched = [p for p in profiles if p["model"] == model]
        if matched:
            profiles = matched
    prof = random.choice(profiles)
    ios_list = list(prof["ios"])  # type: ignore[arg-type]
    if os_version and os_version in ios_list:
        ver = os_version
    elif os_version:
        # caller forced a version not in pool — still accept
        ver = os_version
    else:
        ver = random.choice(ios_list)
    return {
        "model": str(prof["model"]),
        "os_version": str(ver),
        "name": str(prof.get("name") or prof["model"]),
    }


def new_device_ids(
    *,
    model: str | None = None,
    os_version: str | None = None,
) -> Dict[str, str]:
    """Fresh device identity for a brand-new guest (real iPhone model + iOS)."""
    picked = _pick_ios_device_profile(model=model, os_version=os_version)
    return {
        "fgs_id": str(uuid.uuid4()),
        "anonymous_id": str(uuid.uuid4()),
        "app_installed_id": str(uuid.uuid4()).upper(),
        "devsisters_id": str(uuid.uuid4()).upper(),
        "semi_device_id": str(uuid.uuid4()).upper(),
        "device_id": str(uuid.uuid4()),
        "model": picked["model"],
        "os_version": picked["os_version"],
        "model_name": picked.get("name", picked["model"]),
        "manufacturer": "apple",
    }


@dataclass
class AccountState:
    mid: str
    guest_secret: str = ""
    refresh_token: str = ""
    game_access_token: str = ""
    oven_access_token: str = ""
    resource_key: str = DEFAULT_RESOURCE_KEY
    device: Optional[Dict[str, str]] = None
    endpoint: str = DEFAULT_ENDPOINT
    inviter_mid: str = ""
    next_stage: int = 1
    diamond_balance: Optional[int] = None
    email: str = ""
    updated_at: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_session(self) -> Session:
        d = self.device or {}
        resource_key = normalize_resource_key(self.resource_key)
        # A state loaded directly from an old DB/config may be converted to a
        # session without passing through guest_login(). Resolve that legacy
        # value from the same metadata/manifest flow instead of using the
        # hardcoded fallback in the request.
        if resource_key != str(self.resource_key or "").strip():
            resource_key = fetch_resource_key()
            self.resource_key = resource_key
        return Session(
            mid=self.mid,
            game_access_token=self.game_access_token,
            resource_key=resource_key,
            device=DeviceIds(
                fgs_id=d.get("fgs_id", ""),
                anonymous_id=d.get("anonymous_id", ""),
                app_installed_id=d.get("app_installed_id", ""),
                devsisters_id=d.get("devsisters_id", ""),
                semi_device_id=d.get("semi_device_id", ""),
            ),
        )

    def save(self, path: Path) -> None:
        self.updated_at = time.time()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    def to_yaml_dict(self) -> Dict[str, Any]:
        dev = dict(self.device or {})
        # only expose ids used by gRPC headers in config (plus device_id for re-login)
        keep = {
            "fgs_id": dev.get("fgs_id", ""),
            "anonymous_id": dev.get("anonymous_id", ""),
            "app_installed_id": dev.get("app_installed_id", ""),
            "devsisters_id": dev.get("devsisters_id", ""),
            "semi_device_id": dev.get("semi_device_id", ""),
            "device_id": dev.get("device_id", ""),
            "model": dev.get("model", "iPhone14,3"),
            "os_version": dev.get("os_version", "16.1.1"),
            "model_name": dev.get("model_name", ""),
            "manufacturer": dev.get("manufacturer", "apple"),
        }
        return {
            "endpoint": self.endpoint,
            "resource_key": self.resource_key,
            "account": {
                "mid": self.mid,
                "guest_secret": self.guest_secret,
                "refresh_token": self.refresh_token,
                "game_access_token": self.game_access_token,
                "oven_access_token": self.oven_access_token,
                "email": self.email,
            },
            "device": keep,
            "invite": {"inviter_mid": self.inviter_mid},
            "stage": {
                "team_index": 1,
                "from_stage": 0,
                "to_stage": 30,
                "clear_points": [0, 1],
                "sleep_ms": 200,
                "samples_dir": "data/samples",
                "try_start": True,
            },
        }

    def save_yaml(self, path: Path) -> None:
        import yaml

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(self.to_yaml_dict(), sort_keys=False, allow_unicode=True))

    @classmethod
    def load(cls, path: Path) -> "AccountState":
        data = json.loads(Path(path).read_text())
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


def account_from_config(cfg: Dict[str, Any]) -> AccountState:
    acc = cfg.get("account") or {}
    dev = cfg.get("device") or {}
    inv = cfg.get("invite") or {}
    st = cfg.get("stage") or {}
    return AccountState(
        mid=acc["mid"],
        guest_secret=acc.get("guest_secret", ""),
        refresh_token=acc.get("refresh_token", ""),
        game_access_token=acc.get("game_access_token", ""),
        oven_access_token=acc.get("oven_access_token", ""),
        resource_key=cfg.get("resource_key") or acc.get("resource_key") or DEFAULT_RESOURCE_KEY,
        device=dict(dev),
        endpoint=cfg.get("endpoint") or DEFAULT_ENDPOINT,
        inviter_mid=inv.get("inviter_mid", ""),
        next_stage=int(st.get("from_stage") or 1),
        diamond_balance=acc.get("diamond_balance"),
        email=acc.get("email", ""),
    )


def jwt_exp(token: str) -> Optional[int]:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part))
        return int(payload.get("exp") or 0) or None
    except Exception:
        return None


def build_login_headers(
    *,
    os_version: str = "16.1.1",
    locale: str = "zh-Hant",
    timezone: str = "Asia/Shanghai",
    country: str = "CN",
) -> Dict[str, str]:
    try:
        major = int(str(os_version).split(".")[0])
    except Exception:
        major = 16
    darwin = _IOS_MAJOR_TO_DARWIN.get(major, "22.1.0")
    # CFNetwork train roughly tracks Darwin major
    cf_map = {15: "1335", 16: "1399", 17: "1490", 18: "1568"}
    cf = cf_map.get(major, "1399")
    return {
        "Host": "account.devplay.com",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh-Hans;q=0.9",
        "User-Agent": f"CookieRunCrumble/{APP_BUILD} CFNetwork/{cf} Darwin/{darwin}",
        "X-API-Key": API_KEY,
        "X-Env": "prod",
        "X-Bundle-Id": BUNDLE_ID,
        "X-SDK-Version": SDK_VERSION,
        "X-Unity-Version": UNITY_VERSION,
        "X-Platform": "",
        "X-Os-Name": _b64("IOS"),
        "X-Os-Version": _b64(os_version),
        "X-Timezone": _b64(timezone),
        "X-LocaleOnGame": _b64(locale),
        "X-Location-Country": _b64(country),
        "X-App-Version": _b64(APP_VERSION),
        "X-App-Build": _b64(APP_BUILD),
    }


def build_login_body(
    device: Dict[str, str],
    *,
    guest_secret: str = "",
    locale: str = "zh-Hant",
    timezone: str = "Asia/Shanghai",
    country: str = "CN",
    agree_terms: bool = True,
) -> Dict[str, Any]:
    os_version = device.get("os_version") or "16.1.1"
    model = device.get("model") or "iPhone14,3"
    now = int(time.time())
    terms = []
    if agree_terms:
        # ids from live capture (guest create)
        for tid in (28, 30, 32):
            terms.append({"id": tid, "is_agreed": True, "agree_dt": now})
    return {
        "lc": {
            "app_build": APP_BUILD,
            "app_version": APP_VERSION,
            "locale_on_game": locale,
            "location_country": country,
            "os_name": "ios",
            "os_version": os_version,
            "platform": [],
            "store": "appstore",
            "timezone": timezone,
            "anonymous_id": device.get("anonymous_id") or str(uuid.uuid4()),
            "library_version": SDK_VERSION,
            "library_name": "DevPlay Unity SDK",
            "fgs_id": device.get("fgs_id") or str(uuid.uuid4()),
            "app_installed_id": device.get("app_installed_id") or str(uuid.uuid4()).upper(),
            "devsisters_id": device.get("devsisters_id") or str(uuid.uuid4()).upper(),
            "semi_device_id": device.get("semi_device_id") or str(uuid.uuid4()).upper(),
            "device": {
                "traits": [],
                "manufacturer": device.get("manufacturer") or "apple",
                "model": model,
                "version": os_version,
            },
        },
        "device_id": device.get("device_id") or str(uuid.uuid4()),
        "device_type": "ios",
        "push_token": device.get("push_token") or "",
        "timezone": timezone,
        "lang": locale,
        "fallback_country_code": country,
        "agree_ad_day_push": "",
        "agree_ad_night_push": "",
        "recall_session_id": "",
        "terms_updates": terms,
        "guest_secret": guest_secret or "",
    }


class DevPlayError(RuntimeError):
    def __init__(self, code: Any, body: Any):
        super().__init__(f"DevPlay login failed code={code} body={body}")
        self.code = code
        self.body = body


def guest_login(
    *,
    guest_secret: str = "",
    device: Optional[Dict[str, str]] = None,
    timeout: float = 30.0,
    inviter_mid: str = "",
    resource_key: str = DEFAULT_RESOURCE_KEY,
    endpoint: str = DEFAULT_ENDPOINT,
) -> AccountState:
    """Create (empty secret) or re-login (with secret) a guest via /v3/login."""
    # The game performs this CDN request before its account/login RPC.  Resolve
    # it here as well, so the first gRPC request uses the server's current key.
    resolved_resource_key = fetch_resource_key(timeout=timeout)
    dev = dict(device or new_device_ids())
    # ensure device_id exists for re-login body
    if not dev.get("device_id"):
        dev["device_id"] = str(uuid.uuid4())

    headers = build_login_headers(os_version=dev.get("os_version") or "16.1.1")
    body = build_login_body(dev, guest_secret=guest_secret)

    with httpx.Client(timeout=timeout, verify=True) as client:
        resp = client.post(LOGIN_URL, headers=headers, json=body)
        try:
            data = resp.json()
        except Exception as e:
            raise RuntimeError(f"login non-json http={resp.status_code}: {resp.text[:300]}") from e

    code = data.get("code")
    if resp.status_code != 200 or code != 20000:
        raise DevPlayError(code, data)

    member = data.get("member") or {}
    mid = member.get("mid") or ""
    if not mid:
        raise DevPlayError(code, data)

    state = AccountState(
        mid=mid,
        guest_secret=data.get("guest_secret") or guest_secret or "",
        refresh_token=data.get("refresh_token") or "",
        game_access_token=data.get("game_access_token") or "",
        oven_access_token=data.get("oven_access_token") or "",
        resource_key=resolved_resource_key,
        device=dev,
        endpoint=endpoint,
        inviter_mid=inviter_mid,
        next_stage=1,
        email=member.get("email") or "",
        extra={
            "link_status": data.get("link_status"),
            "country_code": data.get("country_code"),
            "terms_type": data.get("terms_type"),
            "created": not bool(guest_secret),
        },
    )
    return state


def relogin(state: AccountState) -> AccountState:
    if not state.guest_secret:
        raise RuntimeError("cannot re-login without guest_secret")
    fresh = guest_login(
        guest_secret=state.guest_secret,
        device=state.device,
        inviter_mid=state.inviter_mid,
        endpoint=state.endpoint,
    )
    # preserve progress fields
    fresh.next_stage = state.next_stage
    fresh.inviter_mid = state.inviter_mid or fresh.inviter_mid
    fresh.diamond_balance = state.diamond_balance
    return fresh


def ensure_token_fresh(state: AccountState, *, skew_sec: int = 60, auto_refresh: bool = True) -> AccountState:
    exp = jwt_exp(state.game_access_token) if state.game_access_token else None
    now = int(time.time())
    if state.game_access_token and exp and exp > now + skew_sec:
        return state
    if auto_refresh and state.guest_secret:
        return relogin(state)
    raise RuntimeError(
        "game_access_token missing/expired and no guest_secret for re-login; "
        f"exp={exp} now={now}"
    )
