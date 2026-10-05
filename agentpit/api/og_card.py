"""The per-agent share card: 1200x630, drawn from the agent's live board row and
stamped with its valuation time. Providers cache a preview per page URL, so the
stamp keeps every cached copy true and each share links a fresh URL."""
import base64
from datetime import UTC, datetime
from html import escape
from pathlib import Path

import httpx
import resvg_py

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

_http = httpx.Client(timeout=5.0)


def fetch_robot(landing_url: str, address: str) -> bytes:
    response = _http.get(f"{landing_url}/agents/{address.lower()}/avatar.svg")
    response.raise_for_status()
    return response.content


def _baseline(size: float, line: float) -> float:
    """Geist's ascender and descender are 1.005 and 0.295 em, centred in the line box."""
    return 1.005 * size + (line - 1.3 * size) / 2


def _tone(value: int) -> str:
    return UP if value > 0 else DOWN if value < 0 else INK


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


def render_card(row: LeaderboardRow, ranked: int, valued_at: int, robot: bytes) -> bytes:
    """`ranked` counts the agents with a place; `valued_at` is when the row's figures were taken."""
    runner = runner_for(row.app, row.host)
    at = datetime.fromtimestamp(valued_at, UTC)
    size = min(120, int(720 / (0.53 * len(row.name))))
    top = round(ROW_TOP + (AVATAR - (1.1 * size + 8 + 2 * LINE + 12)) / 2 - 4.5)
    first = top + 1.1 * size + 8
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" font-family="Geist">'
        '<image href="og-base.png" width="1200" height="630"/>'
        f'<image href="logo.svg" x="72" y="60" width="{30 * 857 / 156}" height="30"/>'
        f'<text x="1128" y="{60 + _baseline(26, 30)}" text-anchor="end" font-size="26" letter-spacing="-0.26" fill="{MUTED}">'
        f'As of {at.day} {at:%b %Y}, {at:%H:%M} UTC</text>'
        f'<clipPath id="robot"><rect x="72" y="{ROW_TOP}" width="{AVATAR}" height="{AVATAR}" rx="{round(AVATAR * 0.28)}"/></clipPath>'
        f'<g clip-path="url(#robot)"><rect x="72" y="{ROW_TOP}" width="{AVATAR}" height="{AVATAR}" fill="#E2E8F0"/>'
        f'<image href="data:image/svg+xml;base64,{base64.b64encode(robot).decode()}" x="72" y="{ROW_TOP}" width="{AVATAR}" height="{AVATAR}"/></g>'
        f'<text x="{TEXT_LEFT}" y="{top + _baseline(size, 1.1 * size)}" font-size="{size}" font-weight="500" letter-spacing="{-0.04 * size}" fill="{INK}">{escape(row.name)}</text>'
        + _line(first, f"runners/{runner.slug}.svg", (f"Runs on {runner.label}", INK))
        + _standing(round(first + LINE + 12), row, ranked)
        + (_trend(row.trend) if row.place else "")
        + "</svg>"
    )
    return resvg_py.svg_to_bytes(svg_string=svg, resources_dir=str(ASSETS), font_files=FONTS, skip_system_fonts=True)
