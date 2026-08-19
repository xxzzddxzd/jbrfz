"""Fixed runtime defaults for live prod bot."""
from __future__ import annotations

# Live game gRPC (provisioned host; do not expose as CLI flag)
ENDPOINT = "https://cc-gameserver-client.live.prod.devslime.cloud:443"

# Invite clear target: stages 1..30 inclusive
TO_STAGE = 30

# MetadataProvider default before provisioning sets the real key.
# Server echoes `crumble-resource-key` on gRPC responses; we adopt it dynamically.
DEFAULT_RESOURCE_KEY = "dev-0000000000"
# Last known live key used as warm start (updated from responses when present).
# Captured from game 1.1.101. Provisioning still refreshes it before login.
FALLBACK_RESOURCE_KEY = "game-data-185237-ee36b3"

# Resource keys are global for the live game server.  These values were
# persisted by older account databases and must never override provisioning.
LEGACY_RESOURCE_KEYS = frozenset(
    {
        "",
        "dev-0000000000",
        "game-data-8319a6-a64b0c",
        "game-data-9db3ba-0ca6ad",
        "game-data-9db3ba-a069b0",
        "game-data-185237-02fbe8",
    }
)


def normalize_resource_key(value: object) -> str:
    """Return a usable key without allowing known stale values through."""
    key = str(value or "").strip()
    return FALLBACK_RESOURCE_KEY if key in LEGACY_RESOURCE_KEYS else key
