"""Guild lab limits extracted from the 10101 patch-data tables."""
from __future__ import annotations

import re

# A guild can recruit at most 50 donor accounts per server day.
GUILD_DAILY_RECRUITMENT_ACCOUNT_LIMIT = 50
GUILD_PRIVATE_DAILY_INVITATION_LIMIT = GUILD_DAILY_RECRUITMENT_ACCOUNT_LIMIT

_DAILY_RECRUITMENT_LIMIT_PATTERN = re.compile(
    r"daily recruitment limit(?:\s+of)?\s*(\d+)?",
    re.IGNORECASE,
)

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

# ``GuildLevels.bytes`` field 3: MaximumGuildMember.  This is the same table
# used by the game client (``GuildLevelTable.GetMaxMemberCount``).  Keeping it
# here lets resident-guild filling follow the live guild level instead of a
# capacity that may have been supplied during an earlier ``init``.
GUILD_MAX_MEMBER_COUNTS: dict[int, int] = {
    1: 15,
    2: 16,
    3: 17,
    4: 18,
    5: 20,
    6: 22,
    7: 24,
    8: 26,
    9: 28,
    10: 29,
    11: 30,
    12: 30,
    13: 30,
    14: 30,
    15: 30,
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
# Resident-guild daily automation must not spend the next tier once a single
# donation would cost more than this safety limit.
GUILD_DAILY_PAID_RESEARCH_MAX_COST = 300


def parse_guild_daily_recruitment_limit(message: str) -> int | None:
    """Return a reported guild recruitment cap, or ``None`` if unrelated.

    Zero means the server reported the limit condition without including its
    numeric value.
    """
    matched = _DAILY_RECRUITMENT_LIMIT_PATTERN.search(str(message or ""))
    if matched is None:
        return None
    value = matched.group(1)
    return int(value) if value else 0


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


def guild_max_member_count(guild_level: int) -> int | None:
    """Return the client-table member capacity for a guild level."""
    level = int(guild_level)
    if level <= 0:
        return None
    if level in GUILD_MAX_MEMBER_COUNTS:
        return GUILD_MAX_MEMBER_COUNTS[level]
    if level > max(GUILD_MAX_MEMBER_COUNTS):
        return max(GUILD_MAX_MEMBER_COUNTS.values())
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
