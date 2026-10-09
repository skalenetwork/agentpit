"""Leagues of the Sports section, keyed off Polymarket's series slug, then tags."""
import re
from dataclasses import dataclass

SPORTS = {
    "esports": "Esports",
    "soccer": "Soccer",
    "tennis": "Tennis",
    "football": "Football",
    "basketball": "Basketball",
    "baseball": "Baseball",
    "hockey": "Hockey",
    "mma": "MMA",
    "motorsport": "Motorsport",
    "cricket": "Cricket",
    "other": "Other",
}

CATCH_ALLS = ("soccer", "tennis", "esports", "basketball", "baseball", "hockey", "cricket")

SEASON = re.compile(r"-\d{4}(-\d{2})?$")
ET = "America/New_York"


@dataclass(frozen=True)
class League:
    key: str
    label: str
    sport: str
    slugs: tuple[str, ...] = ()
    tz: str = "UTC"

    @property
    def names(self) -> tuple[str, ...]:
        return self.slugs or (self.key,)

    @property
    def words(self) -> str:
        return " ".join((self.key, self.label, self.sport, *self.names))


LEAGUES = (
    League("nfl", "NFL", "football", tz=ET),
    League("cfb", "College football", "football", ("cfb", "ncaaf"), ET),
    League("nba", "NBA", "basketball", tz=ET),
    League("wnba", "WNBA", "basketball", tz=ET),
    League("nhl", "NHL", "hockey", tz=ET),
    League("mlb", "MLB", "baseball", tz=ET),
    League("kbo", "KBO", "baseball"),
    League("epl", "Premier League", "soccer", ("epl", "premier-league")),
    League("ucl", "Champions League", "soccer", ("ucl", "champions-league")),
    League("unl", "UEFA Nations League", "soccer", ("soccer-unl", "uefa-nations-league")),
    League("la-liga", "La Liga", "soccer"),
    League("la-liga-2", "La Liga 2", "soccer"),
    League("world-cup", "World Cup", "soccer", ("world-cup", "fifa-world-cup")),
    League("friendlies", "Friendlies", "soccer", ("fifa-friendly",)),
    League("copa-del-rey", "Copa del Rey", "soccer"),
    League("eredivisie", "Eredivisie", "soccer", ("ere", "eredivisie")),
    League("atp", "ATP", "tennis"),
    League("wta", "WTA", "tennis", ("wta", "wta-doubles")),
    League("itf", "ITF", "tennis"),
    League("cs2", "Counter-Strike 2", "esports", ("counter-strike", "counter-strike-2")),
    League("lol", "League of Legends", "esports", ("league-of-legends",)),
    League("dota-2", "Dota 2", "esports"),
    League("valorant", "Valorant", "esports"),
    League("mlbb", "Mobile Legends", "esports", ("mobile-legends-bang-bang",)),
    League("r6", "Rainbow Six Siege", "esports", ("rainbow-six-siege",)),
    League("hok", "Honor of Kings", "esports", ("honor-of-kings",)),
    League("overwatch", "Overwatch", "esports"),
    League("cricket", "Cricket", "cricket", ("cricket", "international-cricket", "legends-league-cricket")),
    League("ufc", "UFC", "mma"),
    League("f1", "Formula 1", "motorsport", ("f1", "formula1")),
)

OTHER = League("other", "Other", "other")
SERIES_WORDS = {name: league.words for league in LEAGUES for name in league.names}


def league_of(series_slug: str | None, tags: set[str]) -> League:
    series = SEASON.sub("", series_slug or "")
    return (
        next((league for league in LEAGUES if series in league.names), None)
        or next((league for league in LEAGUES if tags.intersection(league.names)), None)
        or next((League(sport, f"Other {SPORTS[sport].lower()}", sport) for sport in CATCH_ALLS if sport in tags), OTHER)
    )
