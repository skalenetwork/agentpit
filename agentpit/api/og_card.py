"""The share cards, 1200x630: an agent's, drawn from its live board row and
stamped with its valuation time, and a race's, drawn once per UTC hour and once
for good when final. Providers cache a preview per page URL, so the stamp keeps
every cached copy true and each share links a fresh URL."""
import base64
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import NamedTuple

import httpx
import resvg_py

from agentpit.datastructures.competition import HOUR, Race, Standing
from agentpit.datastructures.match_leg import MICRO
from agentpit.domain.runner import runner_for
from agentpit.services.leaderboard_service import RANK_FLOOR, LeaderboardRow, pct

ASSETS = Path(__file__).with_name("og")
FONTS = [str(ASSETS / "fonts" / "Geist-Regular.ttf"), str(ASSETS / "fonts" / "Geist-Medium.ttf")]
INK = "#0F1A44"
MUTED = "#5B6A85"
UP = "#047857"
DOWN = "#E11D48"
AVATAR = 268
ROW_TOP = 154
TEXT_LEFT = 388
LINE = 44 * 1.2
TREND_TOP = 446
TREND_HEIGHT = 56
NEUTRAL = "#E2E8F0"
MEDALS = ("#F2AE00", "#8EA3C8", "#E8793A")
LINEUP = 8


class Step(NamedTuple):
    x: int
    width: int
    top: int
    robot: int


STEPS = (Step(802, 164, 340, 136), Step(640, 152, 380, 112), Step(976, 152, 412, 112))

_http = httpx.Client(timeout=5.0)

# resvg holds the GIL for the whole ~200 ms render, and this process also
# serves every other route and runs the mirror feed. So a card is drawn once
# per change of its key and then served from memory, and only one is drawn at
# a time. A request waits up to _RENDER_WAIT_S for its turn: link previews
# fetch a new card from several crawlers at once, and each fetches once, so
# the ones that wait must get the card the first one drew. Past that wait the
# answer is a 503 to retry. An agent card's key changes on every valuation
# pass, so an old card is never served again and the cap only bounds memory.
_CARDS_MAX = 64
_RENDER_WAIT_S = 1.0
_render_gate = threading.Lock()
# card name -> (key, png)
_cards: dict[str, tuple[str, bytes]] = {}
_robots: dict[str, bytes] = {}


def card(name: str, key: str, render: Callable[[bytes], bytes], robot: Callable[[], bytes] = lambda: b"") -> bytes | None:
    """The card `name` for `key`, from memory while its key holds. Otherwise
    it is drawn under the gate, unless the request this one waited behind drew
    it, with `robot()`, fetched before the wait so a slow landing holds up no
    other draw; that fetch may raise httpx.HTTPError. None when the draw
    cannot start within _RENDER_WAIT_S."""
    cached = _cards.get(name)
    if cached is not None and cached[0] == key:
        return cached[1]
    art = robot()
    if not _render_gate.acquire(timeout=_RENDER_WAIT_S):
        return None
    try:
        cached = _cards.get(name)
        if cached is None or cached[0] != key:
            cached = (key, render(art))
            _cards.pop(name, None)
            if len(_cards) >= _CARDS_MAX:
                del _cards[next(iter(_cards))]
            _cards[name] = cached
    finally:
        _render_gate.release()
    return cached[1]


def fetch_robot(landing_url: str, address: str) -> bytes:
    """The agent's robot, which the landing draws from its address alone, so
    it is fetched once per process."""
    address = address.lower()
    if address not in _robots:
        response = _http.get(f"{landing_url}/agents/{address}/avatar.svg")
        response.raise_for_status()
        _robots[address] = response.content
    return _robots[address]


def _baseline(size: float, line: float) -> float:
    """Geist's ascender and descender are 1.005 and 0.295 em, centred in the line box."""
    return 1.005 * size + (line - 1.3 * size) / 2


def _tone(value: int) -> str:
    return UP if value > 0 else DOWN if value < 0 else INK


def _day(ts: int) -> str:
    at = datetime.fromtimestamp(ts, UTC)
    return f"{at.day} {at:%b %Y}, {at:%H:%M} UTC"


def _frame(stamp: str) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" font-family="Geist">'
        '<image href="og-base.png" width="1200" height="630"/>'
        f'<image href="logo.svg" x="72" y="60" width="{30 * 857 / 156}" height="30"/>'
        f'<text x="1128" y="{60 + _baseline(26, 30)}" text-anchor="end" font-size="26" letter-spacing="-0.26" fill="{MUTED}">'
        f"{escape(stamp)}</text>"
    )


def _png(svg: str) -> bytes:
    return resvg_py.svg_to_bytes(svg_string=svg, resources_dir=str(ASSETS), font_files=FONTS, skip_system_fonts=True)


def _line(top: float, icon: str, *spans: tuple[str, str]) -> str:
    text = "".join(f'<tspan fill="{color}">{escape(part)}</tspan>' for part, color in spans)
    return (
        f'<image href="{icon}" x="{TEXT_LEFT}" y="{top + (LINE - 40) / 2}" width="40" height="40"/>'
        f'<text x="{TEXT_LEFT + 56}" y="{top + _baseline(44, LINE)}" font-size="44" letter-spacing="-0.88" word-spacing="0.88">{text}</text>'
    )


def _standing(top: float, row: LeaderboardRow, ranked: int) -> str:
    if row.place is None:
        return _line(top, "hourglass.svg", (f"Warming up, {row.trades} of {RANK_FLOOR} trades", MUTED))
    change = row.place_change or 0
    arrow = [(f" {'▲' if change > 0 else '▼'}{abs(change)}", _tone(change))] if change else []
    return _line(top, "trophy.svg", (f"#{row.place} of {ranked}", INK), *arrow, (f" · {pct(row.return_pct)}", INK))


def _trend(closes: list[str]) -> str:
    """Earned at each daily close across the card, zero inside the range, in the tone of the latest."""
    if len(closes) < 2:
        return ""
    earned = [int(c) for c in closes]
    top, bottom = max(0, *earned), min(0, *earned)
    step = (1128 - 72) / (len(earned) - 1)
    points = [(72 + k * step, TREND_TOP + TREND_HEIGHT * (top - e) / (top - bottom or 1)) for k, e in enumerate(earned)]
    tone = _tone(earned[-1])
    x, y = points[-1]
    return (
        f'<polyline points="{" ".join(f"{px:.1f},{py:.1f}" for px, py in points)}" fill="none" stroke="{tone}" '
        'stroke-width="4" stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7" fill="{tone}" stroke="#FFFFFF" stroke-width="3"/>'
    )


def _robot(x: float, y: float, size: int, art: bytes) -> str:
    """A robot as the landing shows it: a rounded square, radius 28%."""
    clip = f"robot-{x:.0f}-{y:.0f}"
    return (
        f'<clipPath id="{clip}"><rect x="{x}" y="{y}" width="{size}" height="{size}" rx="{round(size * 0.28)}"/></clipPath>'
        f'<g clip-path="url(#{clip})"><rect x="{x}" y="{y}" width="{size}" height="{size}" fill="{NEUTRAL}"/>'
        f'<image href="data:image/svg+xml;base64,{base64.b64encode(art).decode()}" x="{x}" y="{y}" width="{size}" height="{size}"/></g>'
    )


def _fit(text: str, width: float, cap: int) -> int:
    """The largest size up to `cap` that sets `text` in Geist Medium within `width`."""
    return min(cap, int(width / (0.53 * len(text))))


def render_card(row: LeaderboardRow, ranked: int, valued_at: int, robot: bytes) -> bytes:
    """`ranked` counts the agents with a place; `valued_at` is when the row's figures were taken."""
    runner = runner_for(row.app, row.host)
    size = _fit(row.name, 720, 120)
    top = round(ROW_TOP + (AVATAR - (1.1 * size + 8 + 2 * LINE + 12)) / 2 - 4.5)
    first = top + 1.1 * size + 8
    svg = (
        _frame(f"As of {_day(valued_at)}")
        + _robot(72, ROW_TOP, AVATAR, robot)
        + f'<text x="{TEXT_LEFT}" y="{top + _baseline(size, 1.1 * size)}" font-size="{size}" font-weight="500" letter-spacing="{-0.04 * size}" fill="{INK}">{escape(row.name)}</text>'
        + _line(first, f"runners/{runner.slug}.svg", (f"Runs on {runner.label}", INK))
        + _standing(round(first + LINE + 12), row, ranked)
        + (_trend(row.trend) if row.place else "")
        + "</svg>"
    )
    return _png(svg)


def _text(x: float, top: float, size: int, text: str, fill: str, weight: int = 400, anchor: str = "start") -> str:
    return (
        f'<text x="{x}" y="{top + _baseline(size, 1.2 * size)}" text-anchor="{anchor}" font-size="{size}" '
        f'font-weight="{weight}" letter-spacing="{-0.02 * size}" fill="{fill}">{escape(text)}</text>'
    )


def _status(race: Race, at: int) -> str:
    if race.phase in ("upcoming", "locked"):
        start = datetime.fromtimestamp(race.startsAt, UTC)
        return f"Starts {start:%b} {start.day} at {start:%H:%M} UTC"
    if race.phase == "live":
        days, hours = divmod(-(-(race.endsAt - at) // HOUR), 24)
        return f"Live · {f'{days}d ' if days else ''}{hours}h left"
    return f"Final · {race.ranked} placed" if race.final else "Ended · final values coming in"


def _step(k: int, s: Standing, art: bytes, race: Race) -> str:
    x, width, top, size = STEPS[k]
    mid = x + width / 2
    y = top - size + 10
    crown = (
        f'<image href="crown.svg" x="{mid - 36}" y="{y - 52}" width="72" height="72" transform="rotate(-12 {mid} {y - 16})"/>'
        if k == 0 and race.final
        else ""
    )
    gap = _text(mid, top + 142, 24, f"${(s.behind or 0) / MICRO:,.0f} behind", MUTED, anchor="middle") if k > 0 and race.phase == "live" else ""
    return (
        f'<rect x="{x}" y="{top}" width="{width}" height="{654 - top}" rx="24" fill="{NEUTRAL}"/>'
        + _robot(mid - size / 2, y, size, art)
        + crown
        + _text(mid, top + 18, 44, str(k + 1), MEDALS[k], 500, "middle")
        + _text(mid, top + 76, _fit(s.name, width - 24, 26), s.name, INK, 500, "middle")
        + _text(mid, top + 108, 26, pct(s.returnPct), _tone(round(s.returnPct * 100)), 500, "middle")
        + gap
    )


def _lineup(race: Race, robots: dict[str, bytes]) -> str:
    shown = race.standings[:LINEUP]
    more = race.entrants - len(shown)
    return "".join(_robot(72 + 104 * k, 470, 88, robots[s.address]) for k, s in enumerate(shown)) + (
        _text(72 + 104 * len(shown) + 4, 490, 40, f"+{more:,}", MUTED, 500) if more > 0 else ""
    )


def _drawn(race: Race) -> list[Standing]:
    return race.standings[: min(3, race.ranked)] if race.ranked else race.standings[:LINEUP]


def render_race_card(race: Race, at: int, robots: dict[str, bytes]) -> bytes:
    """The title, where the race stands, its pool and entrants, and its
    podium, or its first entrants until one places; stamped with `at`, the
    hour it shows, until it is final. `robots` holds the robot of every agent
    drawn by its address."""
    usd, skl = sum(p.usd for p in race.prizes), sum(p.skl for p in race.prizes)
    pool = " + ".join([*([f"${usd:,}"] if usd else []), *([f"{skl:,} SKL"] if skl else [])]) or "Practice"
    lines = ((_status(race, at), INK), (pool, MUTED), (f"{race.entrants:,} entrant{'' if race.entrants == 1 else 's'}", MUTED))
    size = _fit(race.title, 720, 84)
    top = 172
    first = top + 1.1 * size + 12
    svg = (
        _frame("Final" if race.final else f"As of {_day(at)}")
        + f'<text x="72" y="{top + _baseline(size, 1.1 * size)}" font-size="{size}" font-weight="500" '
        f'letter-spacing="{-0.04 * size}" fill="{INK}">{escape(race.title)}</text>'
        + "".join(_text(72, first + 46 * k, 36, text, fill) for k, (text, fill) in enumerate(lines))
        + ("".join(_step(k, s, robots[s.address], race) for k, s in enumerate(_drawn(race))) if race.ranked else _lineup(race, robots))
        + "</svg>"
    )
    return _png(svg)


def race_card(race: Race, at: int, landing_url: str) -> bytes | None:
    """The race's card for the hour `at`, or for good once final, through
    card(); the robots it draws are fetched first, which may raise
    httpx.HTTPError."""
    robots = {s.address: fetch_robot(landing_url, s.address) for s in _drawn(race)}
    return card(f"race:{race.slug}", "final" if race.final else str(at), lambda _: render_race_card(race, at, robots))
