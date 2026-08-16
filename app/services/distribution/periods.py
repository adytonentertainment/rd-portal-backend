"""Period-code coverage math for cadence de-dup (infra PRD §2.5, V-LEDG-CADENCE).

A period code is `PUB<YY><H|Q><n>`; a half-year fully contains its two quarters
(H1 ⊇ Q1,Q2 ; H2 ⊇ Q3,Q4). Used so a writer whose special account appears in
both its quarterly and the overlapping semiannual batch sees the period once.
"""

import re
from typing import Optional, Tuple

_RE = re.compile(r"^PUB(?P<yy>\d{2})(?P<kind>[HQ])(?P<n>\d)$")

_RANGES = {
    ("H", "1"): (1, 6), ("H", "2"): (7, 12),
    ("Q", "1"): (1, 3), ("Q", "2"): (4, 6), ("Q", "3"): (7, 9), ("Q", "4"): (10, 12),
}


def parse_period(code: str) -> Optional[Tuple[int, int, int]]:
    """-> (year, month_start, month_end) or None if unparseable."""
    m = _RE.match(code or "")
    if not m:
        return None
    rng = _RANGES.get((m.group("kind"), m.group("n")))
    if rng is None:
        return None
    return 2000 + int(m.group("yy")), rng[0], rng[1]


def covers(outer: str, inner: str) -> bool:
    """True if `outer` strictly contains `inner` (e.g. PUB25H2 covers PUB25Q4).
    Equal periods are NOT 'covers' (that's the same-period supersede path)."""
    a, b = parse_period(outer), parse_period(inner)
    if a is None or b is None or outer == inner:
        return False
    return a[0] == b[0] and a[1] <= b[1] and a[2] >= b[2]
