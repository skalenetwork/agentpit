from pathlib import Path

import pytest

from agentpit.datastructures.competition import Prize, load_competition
from agentpit.services.competition_service import FOLDER

HEAD = 'title = "T"\nstarts_at = 2026-11-01T00:00:00Z\nends_at = 2026-12-01T00:00:00Z\n'


@pytest.mark.parametrize("path", sorted(FOLDER.glob("*.toml")), ids=lambda path: path.name)
def test_every_competition_file_loads(path: Path):
    load_competition(path)


def test_a_file_is_named_by_its_slug_and_its_ranges_expand(tmp_path):
    path = tmp_path / "monthly-2026-11.toml"
    path.write_text(HEAD + "[prizes]\n1 = { usd = 700, skl = 100 }\n2-3 = { usd = 100, skl = 100 }\n4-10 = { usd = 0, skl = 100 }\n")

    competition = load_competition(path)

    assert (competition.slug, competition.starts_at, competition.ends_at) == ("monthly-2026-11", 1793491200, 1796083200)
    assert competition.prizes == [Prize(usd=700, skl=100)] + [Prize(usd=100, skl=100)] * 2 + [Prize(usd=0, skl=100)] * 7


@pytest.mark.parametrize(
    "name, text",
    [
        ("Bad_Slug.toml", HEAD),
        ("ok.toml", HEAD + 'slug = "ok"\n'),
        ("ok.toml", HEAD + 'title = "U"\n'),
        ("ok.toml", HEAD.replace('"T"', '" T"')),
        ("ok.toml", HEAD.replace('"T"', '"A\\nB"')),
        ("ok.toml", HEAD.replace('"T"', '"' + "x" * 65 + '"')),
        ("ok.toml", HEAD.replace("2026-11-01T00:00:00Z", "2026-11-01")),
        ("ok.toml", HEAD.replace("2026-11-01T00:00:00Z", "2026-11-01T00:00:00")),
        ("ok.toml", HEAD.replace("2026-11-01T00:00:00Z", '"2026-11-01T00:00:00Z"')),
        ("ok.toml", HEAD.replace("2026-11-01T00:00:00Z", "1793491200")),
        ("ok.toml", HEAD.replace("2026-11-01T00:00:00Z", "2026-11-01T00:00:00+02:00")),
        ("ok.toml", HEAD.replace("2026-11-01T00:00:00Z", "2026-11-01T00:00:00.5Z")),
        ("ok.toml", HEAD.replace("2026-12-01T00:00:00Z", "2026-11-01T00:00:00Z")),
        ("ok.toml", HEAD + "[prizes]\n"),
        ("ok.toml", HEAD + "[prizes]\n1 = { usd = 0, skl = 0 }\n"),
        ("ok.toml", HEAD + "[prizes]\n1 = { usd = 700.0, skl = 0 }\n"),
        ("ok.toml", HEAD + '[prizes]\n1 = { usd = "700", skl = 0 }\n'),
        ("ok.toml", HEAD + "[prizes]\n1 = { usd = -1, skl = 1 }\n"),
        ("ok.toml", HEAD + "[prizes]\n1 = { usd = 1 }\n"),
        ("ok.toml", HEAD + "[prizes]\n1 = { usd = 1, tokens = 1 }\n"),
        ("ok.toml", HEAD + "[prizes]\n2 = { usd = 1, skl = 0 }\n"),
        ("ok.toml", HEAD + "[prizes]\n01 = { usd = 1, skl = 0 }\n"),
        ("ok.toml", HEAD + "[prizes]\n1 = { usd = 2, skl = 0 }\n3 = { usd = 1, skl = 0 }\n"),
        ("ok.toml", HEAD + "[prizes]\n1 = { usd = 2, skl = 0 }\n2-2 = { usd = 1, skl = 0 }\n"),
        ("ok.toml", HEAD + "[prizes]\n1-3 = { usd = 2, skl = 0 }\n3 = { usd = 1, skl = 0 }\n"),
        ("ok.toml", HEAD + "[prizes]\n1 = { usd = 1, skl = 0 }\n2 = { usd = 2, skl = 0 }\n"),
        ("ok.toml", HEAD + "[prizes]\n1 = { usd = 2, skl = 0 }\n2 = { usd = 1, skl = 5 }\n"),
        ("ok.toml", HEAD + "[prizes]\n1 = { usd = 2, skl = 0 }\n2-5 = { usd = 1, skl = 0 }\n6-10 = { usd = 1, skl = 0 }\n"),
        ("ok.toml", HEAD + "[prizes]\n1 = { usd = 4000, skl = 0 }\n2-3 = { usd = 501, skl = 0 }\n"),
        ("ok.toml", HEAD + '[prizes]\n1 = { usd = 1, skl = 0 }\ntitle = "T"\n'),
    ],
)
def test_a_bad_file_is_refused(tmp_path, name: str, text: str):
    path = tmp_path / name
    path.write_text(text)

    with pytest.raises(ValueError):
        load_competition(path)
