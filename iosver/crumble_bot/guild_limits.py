"""Guild lab limits extracted from the 10101 patch-data tables."""
from __future__ import annotations

# ``GuildLevels.bytes`` field 5: DailyFreeResearchCount.
GUILD_DAILY_FREE_RESEARCH_LIMITS: dict[int, int] = {
    1: 3,
    2: 3,
    3: 3,
    4: 4,
    5: 4,
    6: 4,
    7: 5,
    8: 5,
    9: 5,
    10: 6,
    11: 6,
    12: 7,
    13: 7,
    14: 8,
    15: 8,
}

# ``GuildLabPaidResearchs.bytes`` field 3, indexed by the one-based daily
# paid-research count.  The item id for every row is the diamond currency
# data id (1464007916).
GUILD_DAILY_PAID_RESEARCH_COSTS: tuple[int, ...] = (
    10,
    30,
    50,
    50,
    50,
    100,
    100,
    100,
    150,
    150,
    150,
    200,
    200,
    200,
    300,
    300,
    300,
    400,
    400,
    400,
    500,
    500,
    500,
    1000,
    1000,
    1000,
    10000,
)

GUILD_PAID_RESEARCH_PRICE_TIER_COUNT = len(GUILD_DAILY_PAID_RESEARCH_COSTS)
GUILD_MAX_DAILY_FREE_RESEARCH_LIMIT = max(
    GUILD_DAILY_FREE_RESEARCH_LIMITS.values()
)


def guild_daily_free_research_limit(guild_level: int) -> int | None:
    """Return the 10101 free-research allowance for a server guild level."""
    level = int(guild_level)
    if level <= 0:
        return None
    if level in GUILD_DAILY_FREE_RESEARCH_LIMITS:
        return GUILD_DAILY_FREE_RESEARCH_LIMITS[level]
    # A future server may expose a level above the bundled table before this
    # client is updated.  The last known tier is safer than reverting to three.
    if level > max(GUILD_DAILY_FREE_RESEARCH_LIMITS):
        return GUILD_MAX_DAILY_FREE_RESEARCH_LIMIT
    return None


def guild_paid_research_cost(daily_count: int) -> int | None:
    """Return the diamond price for a one-based paid-research count."""
    index = int(daily_count) - 1
    if index < 0:
        return None
    # The 10101 client clamps counts above the last table row to row 27;
    # this is a price tier, not a daily action limit.
    index = min(index, GUILD_PAID_RESEARCH_PRICE_TIER_COUNT - 1)
    return GUILD_DAILY_PAID_RESEARCH_COSTS[index]
