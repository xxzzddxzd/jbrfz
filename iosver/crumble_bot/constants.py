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
# Captured from the game after the 10101 update/restart on 2026-08-06.
FALLBACK_RESOURCE_KEY = "game-data-9db3ba-0ca6ad"
