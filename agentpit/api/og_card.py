"""The per-agent share card: 1200x630, and nothing on it changes, because
providers cache OG images for days."""
import base64
from html import escape
from pathlib import Path

import httpx
import resvg_py

from agentpit.domain.runner import Runner

ASSETS = Path(__file__).with_name("og")
FONTS = [str(ASSETS / "fonts" / "Geist-Regular.ttf"), str(ASSETS / "fonts" / "Geist-Medium.ttf")]
INK = "#0F1A44"
MUTED = "#5B6A85"
AVATAR = 268
ROW_TOP = 154
TEXT_LEFT = 388
LINE = 44 * 1.2

_http = httpx.Client(timeout=5.0)


def fetch_robot(landing_url: str, address: str) -> bytes:
    response = _http.get(f"{landing_url}/agents/{address.lower()}/avatar.svg")
    response.raise_for_status()
    return response.content


def _baseline(size: float, line: float) -> float:
    """Geist's ascender and descender are 1.005 and 0.295 em, centred in the line box."""
    return 1.005 * size + (line - 1.3 * size) / 2


def _line(top: float, icon: str, text: str, color: str) -> str:
    return (
        f'<image href="{icon}" x="{TEXT_LEFT}" y="{top + (LINE - 40) / 2}" width="40" height="40"/>'
        f'<text x="{TEXT_LEFT + 56}" y="{top + _baseline(44, LINE)}" font-size="44" letter-spacing="-0.88" word-spacing="0.88" fill="{color}">{escape(text)}</text>'
    )


def render_card(name: str, address: str, runner: Runner, robot: bytes) -> bytes:
    size = min(120, int(720 / (0.53 * len(name))))
    top = round(ROW_TOP + (AVATAR - (1.1 * size + 8 + 2 * LINE + 12)) / 2 - 4.5)
    first = top + 1.1 * size + 8
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" font-family="Geist">'
        '<image href="og-base.png" width="1200" height="630"/>'
        f'<image href="logo.svg" x="72" y="60" width="{30 * 857 / 156}" height="30"/>'
        f'<clipPath id="robot"><rect x="72" y="{ROW_TOP}" width="{AVATAR}" height="{AVATAR}" rx="{round(AVATAR * 0.28)}"/></clipPath>'
        f'<g clip-path="url(#robot)"><rect x="72" y="{ROW_TOP}" width="{AVATAR}" height="{AVATAR}" fill="#E2E8F0"/>'
        f'<image href="data:image/svg+xml;base64,{base64.b64encode(robot).decode()}" x="72" y="{ROW_TOP}" width="{AVATAR}" height="{AVATAR}"/></g>'
        f'<text x="{TEXT_LEFT}" y="{top + _baseline(size, 1.1 * size)}" font-size="{size}" font-weight="500" letter-spacing="{-0.04 * size}" fill="{INK}">{escape(name)}</text>'
        + _line(first, f"runners/{runner.slug}.svg", f"Runs on {runner.label}", INK)
        + _line(round(first + LINE + 12), "wallet.svg", f"{address[:6]}…{address[-4:]}", MUTED)
        + "</svg>"
    )
    return resvg_py.svg_to_bytes(svg_string=svg, resources_dir=str(ASSETS), font_files=FONTS, skip_system_fonts=True)
