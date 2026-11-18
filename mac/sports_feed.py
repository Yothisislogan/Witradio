#!/usr/bin/env python3
"""Sports facts for Ying & Yang: results, tonight's games and headlines.

The provider today is ESPN's public site API, which is good for rehearsing
the show but carries no broadcast license (config/feeds.yaml marks it not
cleared, so the show can't reach the air from it). A licensed feed slots in
as another provider with the same two calls: scoreboard(league, day) and
news(league, limit).

Headlines about arrests, courts, injuries, health or apologies are held back
from the debate topics (show bible guardrails: takes target play, coaching
and front-office decisions; no legal cases or health speculation; never mock
an injury).

    python3 mac/sports_feed.py                 today's sheet, printed
    python3 mac/sports_feed.py --date 2026-11-17 --leagues nba nhl
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import feeds  # noqa: E402
from feeds import FactSheet, FeedError, Source  # noqa: E402

ET = ZoneInfo("America/New_York")
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
LEAGUES = {  # id: (ESPN path, on-air label, days of results to look back over)
    "nfl": ("football/nfl", "NFL", 4),
    "college-football": ("football/college-football", "College football", 4),
    "nba": ("basketball/nba", "NBA", 1),
    "wnba": ("basketball/wnba", "WNBA", 1),
    "mens-college-basketball": ("basketball/mens-college-basketball", "Men's college basketball", 1),
    "womens-college-basketball": ("basketball/womens-college-basketball", "Women's college basketball", 1),
    "mlb": ("baseball/mlb", "MLB", 1),
    "nhl": ("hockey/nhl", "NHL", 1),
    "mls": ("soccer/usa.1", "MLS", 2),
}
SENSITIVE = re.compile(
    r"\b(arrest\w*|charged|charges|court|lawsuit|sued|suing|police|domestic|assault\w*|criminal\w*|indict\w*|"
    r"jail|prison|sentenc\w*|died|dies|death|dead|funeral|injur\w*|concussion|surgery|illness|cancer|"
    r"hospital\w*|mental health|suspen\w*|investigat\w*|allegation\w*|abuse|gambling|betting|bets?|odds|"
    r"divorce|pregnan\w*|apolog\w*|slur|racis\w*|derogatory)\b", re.I)


@dataclass
class Team:
    name: str
    short: str
    score: int | None = None
    record: str = ""
    winner: bool = False


@dataclass
class Game:
    league: str
    game_id: str
    start: str  # UTC ISO
    state: str  # pre | in | post
    detail: str
    home: Team
    away: Team
    venue: str = ""
    broadcast: str = ""
    recap: str = ""
    leaders: list[str] = field(default_factory=list)

    @property
    def local_start(self) -> datetime:
        return datetime.fromisoformat(self.start.replace("Z", "+00:00")).astimezone(ET)

    @property
    def day_label(self) -> str:
        return f"{self.local_start:%A %B} {self.local_start.day}"

    def result_line(self) -> str:
        first, second = (self.home, self.away) if (self.home.score or 0) >= (self.away.score or 0) else (self.away, self.home)
        status = self.detail if self.detail and self.detail.lower() != "final" else "final"
        line = f"{self.day_label}: {first.name} {first.score}, {second.name} {second.score} ({status})."
        records = [f"{t.name} {t.record}" for t in (first, second) if t.record]
        if records:
            line += " Records now: " + "; ".join(records) + "."
        if self.leaders:
            line += " " + " ".join(self.leaders)
        if self.recap:
            line += f" Recap: {self.recap}"
        return line

    def live_line(self) -> str:
        return f"{self.away.name} {self.away.score} at {self.home.name} {self.home.score} ({self.detail})."

    def preview_line(self) -> str:
        line = f"{self.away.name} at {self.home.name}, {self.local_start:%-I:%M %p} Eastern"
        if self.venue:
            line += f", {self.venue}"
        line += "."
        records = [f"{t.name} {t.record}" for t in (self.away, self.home) if t.record]
        if records:
            line += " Records: " + "; ".join(records) + "."
        return line


@dataclass
class Headline:
    league: str
    headline: str
    description: str
    published: str
    sensitive: bool = False


class EspnProvider:
    name = "espn"

    def __init__(self, base: str = ESPN_BASE, timeout: float = 30):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def scoreboard(self, league: str, day: date) -> list[Game]:
        path = LEAGUES[league][0]
        data = feeds.get_json(f"{self.base}/{path}/scoreboard", params={"dates": f"{day:%Y%m%d}"}, timeout=self.timeout)
        return [game for event in data.get("events") or [] if (game := self._game(league, event))]

    def news(self, league: str, limit: int = 8) -> list[Headline]:
        path = LEAGUES[league][0]
        data = feeds.get_json(f"{self.base}/{path}/news", params={"limit": limit}, timeout=self.timeout)
        found = []
        for article in data.get("articles") or []:
            headline, description = str(article.get("headline") or "").strip(), str(article.get("description") or "").strip()
            if headline:
                found.append(Headline(league, headline, description, str(article.get("published") or ""),
                                      sensitive=bool(SENSITIVE.search(f"{headline} {description}"))))
        return found

    @staticmethod
    def _game(league: str, event: dict) -> Game | None:
        competitions = event.get("competitions") or []
        if not competitions:
            return None
        competition = competitions[0]
        sides: dict[str, Team] = {}
        leaders: list[str] = []
        for competitor in competition.get("competitors") or []:
            team = competitor.get("team") or {}
            records = {r.get("name") or r.get("type"): r.get("summary") for r in competitor.get("records") or []}
            score = competitor.get("score")
            sides[competitor.get("homeAway", "home")] = Team(
                name=str(team.get("displayName") or team.get("name") or "?"),
                short=str(team.get("shortDisplayName") or team.get("name") or ""),
                score=int(score) if str(score or "").isdigit() else None,
                record=str(records.get("overall") or records.get("total") or ""),
                winner=bool(competitor.get("winner")))
            for category in (competitor.get("leaders") or [])[:1]:
                top = (category.get("leaders") or [{}])[0]
                athlete = (top.get("athlete") or {}).get("displayName")
                if athlete and top.get("displayValue"):
                    label = category.get("displayName") or category.get("name") or "Leader"
                    leaders.append(f"{label} leader for the {sides[competitor.get('homeAway', 'home')].short}: "
                                   f"{athlete}, {top['displayValue']}.")
        if "home" not in sides or "away" not in sides:
            return None
        status = (event.get("status") or competition.get("status") or {}).get("type") or {}
        broadcasts = [n for b in competition.get("broadcasts") or [] for n in b.get("names") or []]
        headlines = competition.get("headlines") or []
        recap = str((headlines[0].get("shortLinkText") or headlines[0].get("description") or "") if headlines else "")
        return Game(league=league, game_id=str(event.get("id", "")), start=str(event.get("date", "")),
                    state=str(status.get("state") or "pre"), detail=str(status.get("shortDetail") or status.get("detail") or ""),
                    home=sides["home"], away=sides["away"], venue=str((competition.get("venue") or {}).get("fullName") or ""),
                    broadcast=", ".join(broadcasts), recap=recap.strip(), leaders=leaders)


def build_facts(provider, leagues: Iterable[str], source: Source, *, today: date | None = None,
                news_limit: int = 8) -> FactSheet:
    today = today or datetime.now(ET).date()
    sheet = FactSheet("sports", feeds.now_iso())
    games: list[dict] = []
    held: list[str] = []
    for league in leagues:
        if league not in LEAGUES:
            sheet.warnings.append(f"unknown league {league}")
            continue
        _, label, lookback = LEAGUES[league]
        try:
            finals: dict[str, Game] = {}
            for back in range(lookback, 0, -1):
                for game in provider.scoreboard(league, today - timedelta(days=back)):
                    if game.state == "post":
                        finals[game.game_id] = game
            todays = provider.scoreboard(league, today)
        except FeedError as exc:
            sheet.warnings.append(f"{label}: {exc}")
            continue
        for game in todays:
            if game.state == "post":
                finals[game.game_id] = game
        for game in finals.values():
            sheet.add(f"RESULTS, {label}", game.result_line(), source)
            games.append(asdict(game))
        for game in (g for g in todays if g.state == "in"):
            sheet.add(f"IN PROGRESS, {label}", game.live_line(), source)
        for game in [g for g in todays if g.state == "pre"][:12]:
            sheet.add(f"COMING UP TODAY, {label}", game.preview_line(), source)
            games.append(asdict(game))
        if not finals and not todays:
            continue  # out of season: no headlines either
        try:
            for item in provider.news(league, news_limit):
                if item.sensitive:
                    held.append(item.headline)
                    continue
                text = f"{item.headline}. {item.description}" if item.description else item.headline
                sheet.add(f"HEADLINES, {label} ({source.credit} reporting; restate in your own words)", text, source)
        except FeedError as exc:
            sheet.warnings.append(f"{label} news: {exc}")
    sheet.data.update({"games": games, "held_back": held, "date": today.isoformat()})
    return sheet


def for_config(config: dict | None = None):
    config = config or feeds.load_feeds_config()
    sports = config.get("sports") or {}
    name = sports.get("provider", "espn")
    if name != "espn":
        raise FeedError(f"no sports provider called {name} yet")
    return EspnProvider(), feeds.source_from(sports.get("providers") or {}, name), list(sports.get("leagues") or []), \
        int(sports.get("news_limit", 8))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sports facts for Ying & Yang")
    parser.add_argument("--date", type=date.fromisoformat, help="the show's date (default: today, Eastern)")
    parser.add_argument("--leagues", nargs="*")
    parser.add_argument("--save", type=Path, help="also write the fact sheet as JSON")
    args = parser.parse_args(argv)
    provider, source, leagues, news_limit = for_config()
    sheet = build_facts(provider, args.leagues or leagues, source, today=args.date, news_limit=news_limit)
    print(sheet.text() or "(no games or headlines)")
    print(f"\nsources: {', '.join(f'{s.name} ({'cleared' if s.cleared else 'NOT cleared for air'})' for s in sheet.sources.values())}")
    for warning in sheet.warnings:
        print(f"warning: {warning}")
    if sheet.data.get("held_back"):
        print(f"held back from debate topics: {len(sheet.data['held_back'])} sensitive headlines")
    if args.save:
        sheet.save(args.save)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
