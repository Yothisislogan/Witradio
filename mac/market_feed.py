#!/usr/bin/env python3
"""Market and economy facts for The Market (show bible: data sources).

- FRED (free key in FRED_API_KEY): oil, the economy (CPI, jobs, retail sales)
  and the release calendar. Its stock index and mortgage series belong to
  their owners (S&P Dow Jones Indices, Nasdaq, Freddie Mac) and come through
  a separate, not-cleared source until those terms are settled or a licensed
  vendor supplies the closes.
- U.S. Treasury: the daily par yield curve, posted the same afternoon.
- BLS (optional key in BLS_API_KEY): the 8:30 AM releases straight from the
  source, for the Opening Bell.
- Federal Reserve Board press releases: what the Fed said, for "On the
  Record" (Board material is public domain).

Every change and percentage is computed here, from the source values, so
the host never does arithmetic and the fact check has exact numbers to match.

    python3 mac/market_feed.py --format closing_tape
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import feeds  # noqa: E402
from feeds import FactSheet, FeedError, Source  # noqa: E402

EASTERN = ZoneInfo("America/New_York")
FRED_BASE = "https://api.stlouisfed.org/fred"
TREASURY_URL = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{year}/all"
BLS_V2 = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
BLS_V1 = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
FED_PRESS = "https://www.federalreserve.gov/feeds/press_all.xml"
FORMATS = ("opening_bell", "closing_tape", "week_ahead")


def _day(value: str) -> date:
    return date.fromisoformat(value[:10])


def say_date(day: date) -> str:
    return f"{day:%A %B} {day.day}, {day.year}"


def say_month(day: date) -> str:
    return f"{day:%B %Y}"


def direction(change: float) -> str:
    return "up" if change > 0 else "down" if change < 0 else "unchanged"


# ── providers ────────────────────────────────────────────────────────────────


class FredProvider:
    name = "fred"

    def __init__(self, api_key: str, base: str = FRED_BASE):
        if not api_key:
            raise FeedError("FRED needs a free API key in FRED_API_KEY (fred.stlouisfed.org/docs/api/api_key.html)")
        self.api_key, self.base = api_key, base.rstrip("/")

    def _get(self, path: str, **params: Any) -> dict:
        data = feeds.get_json(f"{self.base}/{path}", params={"api_key": self.api_key, "file_type": "json", **params})
        if isinstance(data, dict) and data.get("error_message"):
            raise FeedError(f"FRED: {data['error_message']}")
        return data

    def observations(self, series_id: str, count: int = 14) -> list[tuple[date, float]]:
        """Newest first, skipping FRED's '.' for missing days."""
        data = self._get("series/observations", series_id=series_id, sort_order="desc", limit=count)
        found = []
        for row in data.get("observations") or []:
            try:
                found.append((_day(row["date"]), float(row["value"])))
            except (KeyError, ValueError):
                continue
        return found

    def release_dates(self, start: date, end: date) -> list[tuple[date, str]]:
        data = self._get("releases/dates", realtime_start=start.isoformat(), realtime_end=end.isoformat(),
                         include_release_dates_with_no_data="true", order_by="release_date", sort_order="asc",
                         limit=1000)
        rows = [(_day(r["date"]), str(r.get("release_name", "")).strip()) for r in data.get("release_dates") or []]
        return [(d, n) for d, n in rows if start <= d <= end and n]


class TreasuryProvider:
    name = "treasury"

    def __init__(self, url: str = TREASURY_URL):
        self.url = url

    def curve(self, today: date, count: int = 5) -> list[dict[str, Any]]:
        """The latest days' curves, newest first; last year's file only when this year's is too short."""
        rows: dict[date, dict[str, Any]] = {}
        for year in (today.year, today.year - 1):
            raw = feeds.get(self.url.format(year=year), params={"type": "daily_treasury_yield_curve",
                                                                 "field_tdr_date_value": year, "_format": "csv"})
            for row in csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))):
                try:
                    day = datetime.strptime(row["Date"], "%m/%d/%Y").date()
                except (KeyError, ValueError):
                    continue
                if day <= today and day not in rows:
                    rows[day] = {"date": day, **{k: float(v) for k, v in row.items() if k != "Date" and v}}
            if len(rows) >= 2:
                break
        if not rows:
            raise FeedError("Treasury: no yield curve rows")
        return [rows[day] for day in sorted(rows, reverse=True)[:count]]


class BlsProvider:
    name = "bls"

    def __init__(self, api_key: str = "", v2: str = BLS_V2, v1: str = BLS_V1):
        self.api_key, self.v2, self.v1 = api_key, v2, v1

    def series(self, ids: Sequence[str], start_year: int, end_year: int) -> dict[str, list[tuple[date, float]]]:
        body: dict[str, Any] = {"seriesid": list(ids), "startyear": str(start_year), "endyear": str(end_year)}
        if self.api_key:
            body["registrationkey"] = self.api_key
        data = feeds.get_json(self.v2 if self.api_key else self.v1, data=json.dumps(body).encode(),
                              headers={"Content-Type": "application/json"})
        if data.get("status") != "REQUEST_SUCCEEDED":
            raise FeedError(f"BLS: {' '.join(data.get('message') or []) or data.get('status')}")
        found: dict[str, list[tuple[date, float]]] = {}
        for series in (data.get("Results") or {}).get("series") or []:
            points = []
            for row in series.get("data") or []:
                period = str(row.get("period", ""))
                if not period.startswith("M") or period == "M13":
                    continue
                try:
                    points.append((date(int(row["year"]), int(period[1:]), 1), float(row["value"])))
                except (KeyError, ValueError):
                    continue
            found[series.get("seriesID", "")] = sorted(points, reverse=True)
        return found


class FedPressProvider:
    name = "fed_press"

    def __init__(self, url: str = FED_PRESS):
        self.url = url

    def releases(self, limit: int = 8) -> list[dict[str, str]]:
        try:
            root = ET.fromstring(feeds.get(self.url))
        except ET.ParseError as exc:
            raise FeedError("Federal Reserve: the press feed isn't valid RSS") from exc
        found = []
        for item in root.findall("./channel/item")[:limit]:
            when = item.findtext("pubDate") or ""
            try:
                day = parsedate_to_datetime(when).astimezone(EASTERN).date()
            except (TypeError, ValueError):
                continue
            found.append({"date": day.isoformat(), "title": " ".join((item.findtext("title") or "").split()),
                          "category": item.findtext("category") or "", "link": item.findtext("link") or ""})
        return found


# ── the fact sheet ───────────────────────────────────────────────────────────


@dataclass
class Providers:
    fred: FredProvider | None = None
    treasury: TreasuryProvider | None = None
    bls: BlsProvider | None = None
    fed: FedPressProvider | None = None


def _fmt(value: float, places: int = 2) -> str:
    return f"{value:,.{places}f}"


def _series_lines(fred: FredProvider, series: Mapping[str, Mapping[str, Any]], kinds: Sequence[str],
                  sheet: FactSheet, heading: str, sources: Mapping[str, Source]) -> None:
    for series_id, meta in series.items():
        kind = meta.get("kind", "price")
        if kind not in kinds:
            continue
        source = sources["fred_third_party" if meta.get("third_party") else "fred"]
        name, unit = meta.get("name", series_id), meta.get("unit", "")
        try:
            points = fred.observations(series_id, 14)
        except FeedError as exc:
            sheet.warnings.append(f"{series_id}: {exc}")
            continue
        if len(points) < 2:
            continue
        (day, value), (before_day, before) = points[0], points[1]
        if kind == "index":
            change = (value - before) / before * 100
            line = (f"{name} closed at {_fmt(value)} on {say_date(day)}, {direction(change)} "
                    f"{abs(change):.2f}% from {_fmt(before)} on {say_date(before_day)}.")
        elif kind == "price":
            change = value - before
            line = (f"{name}: ${_fmt(value)} {unit} on {say_date(day)}, {direction(change)} "
                    f"${abs(change):.2f} from {say_date(before_day)}.").replace("  ", " ")
        elif kind == "rate":
            change = value - before
            line = (f"{name}: {value:.2f}% for {say_month(day) if series_id == 'UNRATE' else say_date(day)}, "
                    f"{direction(change)} {abs(change):.2f} points from {before:.2f}%.")
        elif kind == "yoy":
            year_ago = next((v for d, v in points if (d.year, d.month) == (day.year - 1, day.month)), None)
            if year_ago is None:
                continue
            change = (value - year_ago) / year_ago * 100
            line = f"{name}: {direction(change)} {abs(change):.1f}% from a year earlier, for {say_month(day)}."
        elif kind == "monthly_change":
            change = value - before
            line = f"{name}: {direction(change)} {abs(change):,.0f} {unit} in {say_month(day)}."
        elif kind == "monthly_pct":
            change = (value - before) / before * 100
            line = f"{name}: {direction(change)} {abs(change):.1f}% in {say_month(day)} from the month before."
        else:
            continue
        sheet.add(heading, line, source)
        sheet.data.setdefault("series", {})[series_id] = {"date": day.isoformat(), "value": value,
                                                          "previous": before, "previous_date": before_day.isoformat()}


def build_facts(fmt: str, providers: Providers, config: Mapping[str, Any] | None = None, *,
                today: date | None = None) -> FactSheet:
    if fmt not in FORMATS:
        raise FeedError(f"format must be one of {', '.join(FORMATS)}")
    config = config or (feeds.load_feeds_config().get("markets") or {})
    provider_config = config.get("providers") or {}
    sources = {name: feeds.source_from(provider_config, name)
               for name in ("fred", "fred_third_party", "treasury", "bls", "fed_press")}
    series = config.get("series") or {}
    today = today or datetime.now(EASTERN).date()
    sheet = FactSheet(f"markets/{fmt}", feeds.now_iso())
    sheet.data["date"] = today.isoformat()

    if providers.fred:
        _series_lines(providers.fred, series, ("index",), sheet, "THE TAPE (latest closes available)", sources)
        _series_lines(providers.fred, series, ("price",), sheet, "COMMODITIES", sources)
        _series_lines(providers.fred, series, ("rate",), sheet, "RATES", sources)
        _series_lines(providers.fred, series, ("yoy", "monthly_change", "monthly_pct"), sheet, "THE ECONOMY", sources)
        window = {"opening_bell": (today, today), "closing_tape": (today + timedelta(days=1), today + timedelta(days=3)),
                  "week_ahead": (today + timedelta(days=1), today + timedelta(days=7))}[fmt]
        try:
            releases = providers.fred.release_dates(*window)
        except FeedError as exc:
            sheet.warnings.append(f"release calendar: {exc}")
            releases = []
        by_day: dict[date, list[str]] = {}
        for day, release in releases:
            by_day.setdefault(day, []).append(release)
        for day, names in sorted(by_day.items()):
            sheet.add("CALENDAR (scheduled data releases)", f"{say_date(day)}: {'; '.join(dict.fromkeys(names))}.",
                      sources["fred"])

    if providers.treasury:
        try:
            curve = providers.treasury.curve(today)
        except FeedError as exc:
            sheet.warnings.append(str(exc))
            curve = []
        if curve:
            latest = curve[0]
            parts = [f"{label} {latest[key]:.2f}%" for key, label in (("2 Yr", "2-year"), ("10 Yr", "10-year"),
                                                                     ("30 Yr", "30-year")) if key in latest]
            line = f"Treasury yields on {say_date(latest['date'])}: {', '.join(parts)}."
            if len(curve) > 1 and "10 Yr" in curve[1] and "10 Yr" in latest:
                change = latest["10 Yr"] - curve[1]["10 Yr"]
                line += (f" The 10-year was {curve[1]['10 Yr']:.2f}% on {say_date(curve[1]['date'])}, "
                         f"so it is {direction(change)} {abs(change):.2f} points.")
            sheet.add("TREASURY YIELDS", line, sources["treasury"])
            sheet.data["treasury"] = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in latest.items()}

    if providers.bls and fmt == "opening_bell":
        try:
            found = providers.bls.series(["CUUR0000SA0", "LNS14000000", "CES0000000001"], today.year - 1, today.year)
        except FeedError as exc:
            sheet.warnings.append(str(exc))
            found = {}
        cpi = found.get("CUUR0000SA0") or []
        if cpi:
            day, value = cpi[0]
            year_ago = next((v for d, v in cpi if (d.year, d.month) == (day.year - 1, day.month)), None)
            if year_ago:
                change = (value - year_ago) / year_ago * 100
                sheet.add("THIS MORNING'S RELEASES (Bureau of Labor Statistics)",
                          f"Consumer prices for {say_month(day)}: {direction(change)} {abs(change):.1f}% from a year earlier.",
                          sources["bls"])
        jobs = found.get("CES0000000001") or []
        if len(jobs) > 1:
            change = jobs[0][1] - jobs[1][1]
            sheet.add("THIS MORNING'S RELEASES (Bureau of Labor Statistics)",
                      f"Employers {'added' if change >= 0 else 'cut'} {abs(change):,.0f} thousand jobs in {say_month(jobs[0][0])}.",
                      sources["bls"])
        rate = found.get("LNS14000000") or []
        if rate:
            sheet.add("THIS MORNING'S RELEASES (Bureau of Labor Statistics)",
                      f"The unemployment rate for {say_month(rate[0][0])} was {rate[0][1]:.1f}%.", sources["bls"])

    if providers.fed:
        try:
            releases = providers.fed.releases(8)
        except FeedError as exc:
            sheet.warnings.append(str(exc))
            releases = []
        for item in releases:
            day = date.fromisoformat(item["date"])
            if (today - day).days <= 7:
                sheet.add("FEDERAL RESERVE BOARD (press releases)", f"{say_date(day)}: {item['title']}.", sources["fed_press"])
    return sheet


def providers_from_env(config: Mapping[str, Any] | None = None) -> Providers:
    config = config or (feeds.load_feeds_config().get("markets") or {})
    providers = config.get("providers") or {}
    fred_key = os.environ.get((providers.get("fred") or {}).get("api_key_env", "FRED_API_KEY"), "")
    bls_key = os.environ.get((providers.get("bls") or {}).get("api_key_env", "BLS_API_KEY"), "")
    return Providers(fred=FredProvider(fred_key) if fred_key else None, treasury=TreasuryProvider(),
                     bls=BlsProvider(bls_key), fed=FedPressProvider())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Market facts for The Market")
    parser.add_argument("--format", choices=FORMATS, default="closing_tape")
    parser.add_argument("--date", type=date.fromisoformat)
    parser.add_argument("--save", type=Path)
    args = parser.parse_args(argv)
    providers = providers_from_env()
    if providers.fred is None:
        print("note: FRED_API_KEY is not set, so closes, the economy and the calendar are missing")
    sheet = build_facts(args.format, providers, today=args.date)
    print(sheet.text() or "(nothing fetched)")
    print(f"\nsources: {', '.join(f'{s.name} ({'cleared' if s.cleared else 'NOT cleared for air'})' for s in sheet.sources.values())}")
    for warning in sheet.warnings:
        print(f"warning: {warning}")
    if args.save:
        sheet.save(args.save)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
