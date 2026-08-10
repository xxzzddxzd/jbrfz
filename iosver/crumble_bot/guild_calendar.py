"""Game-calendar helpers shared by guild workflows."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo


# The game constructs its calendar with ``GameDateConverter.CreateBaseKst``.
# KST midnight is 23:00 on the previous civil day in Asia/Shanghai.
GUILD_DAILY_TIMEZONE = ZoneInfo("Asia/Seoul")


def guild_day_key(now: Optional[float] = None) -> str:
    """Return the current guild server day in the game's KST calendar."""
    timestamp = time.time() if now is None else float(now)
    return datetime.fromtimestamp(timestamp, GUILD_DAILY_TIMEZONE).date().isoformat()


def guild_daily_count(
    count: int,
    last_action_at_millis: int,
    *,
    now: Optional[float] = None,
) -> int:
    """Return a raw daily counter only when its server timestamp is today.

    Older SQLite rows do not contain the action timestamp.  In that case the
    caller's persisted day key remains the compatibility fallback.
    """
    raw_count = max(0, int(count))
    last_action = int(last_action_at_millis or 0)
    if last_action <= 0:
        return raw_count
    return (
        raw_count
        if guild_day_key(last_action / 1000.0) == guild_day_key(now)
        else 0
    )
