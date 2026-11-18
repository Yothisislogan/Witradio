#!/usr/bin/env python3
"""Library of Congress recordings for The Stacks (show bible: "The Stacks with Steve").

Two kinds of source, listed in config/loc_sources.yaml:

- loc: full recordings from a loc.gov collection: the Edison discs, the
  Variety Stage recordings and, opt-in only, the National Jukebox. Only
  files loc.gov serves from /storage-services/ are downloaded, never a
  streaming URL.
- citizen_dj: the phrase-length clips the Library's Citizen DJ project cut
  from National Jukebox recordings, for the remix segment. The clip lists
  come from the Citizen DJ repository, the audio from its asset bucket.

Every item passes three checks before anything is downloaded:

1. Publication year. The bible: "The recording's publication year is the
   gate. No year, no airplay." US recordings published in or before the
   cutoff year (1925 during 2026) are public domain. Edison items carry an
   imprint ("Orange, N.J. : Edison, 1914."); the Jukebox gives only the
   session date, and records were issued weeks to months after the session.
   So a recording made before the cutoff year counts as published by it; one
   made in the cutoff year arrives with no year, to be checked against the
   Discography of American Historical Recordings; anything later is skipped.
2. Issued. A label and catalog number (from the record, or the disc the
   transfer was made from) show the recording was published. Without one,
   or an imprint, it arrives with no year and waits for a check.
3. Content screen. The bible: "Screen every title and lyric before it
   enters the library. Default is exclude." Items in the Jukebox's "Ethnic
   characterizations" genre, or whose titles use slurs, are kept out before
   download. Humorous songs, comedies and a list of warning words are
   flagged. Everything that comes in is quarantined until someone listens
   and approves it, item by item.

loc.gov allows 20 JSON requests a minute and blocks for an hour past that;
any request during the block restarts the hour. So API calls are paced at
one every 3.5 s, item records are cached, each source keeps a ledger so a
rerun skips what it has seen, and a 429 or a CAPTCHA page stops the run.

    python3 tools/pull_loc.py sources
    python3 tools/pull_loc.py fetch loc-edison --dry-run
    python3 tools/pull_loc.py fetch loc-edison --limit 25
    python3 tools/pull_loc.py fetch cdj-jukebox --collection loc-jukebox-jazz --limit 10
    python3 tools/pull_loc.py review
    python3 tools/pull_loc.py approve 41 42 43
    python3 tools/pull_loc.py reject 44 --why "dialect comedy"
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import catalog as cat  # noqa: E402
import music_library as ml  # noqa: E402
from catalog import Catalog  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCES_PATH = PROJECT_ROOT / "config" / "loc_sources.yaml"
API_BASE = "https://www.loc.gov"
CDJ_DATA_BASE = "https://raw.githubusercontent.com/LibraryOfCongress/citizen-dj/master/_data"
CDJ_ASSET_BASE = "https://s3.amazonaws.com/citizen-dj-assets.labs.loc.gov"
CDJ_SITE = "https://citizen-dj.labs.loc.gov"
API_INTERVAL = 3.5  # seconds between JSON requests: loc.gov allows 20 a minute
MEDIA_INTERVAL = 0.5  # 150 a minute on /storage-services/
USER_AGENT = ml.USER_AGENT

VERIFY_CONTENT = "verify-content"
VERIFY_PUBLICATION = ml.VERIFY_PUBLICATION
MARKER = re.compile(r"\[(verify-[a-z]+)\]")
ITEM_ID = re.compile(r"/item/([^/?#]+)")

LABEL_CODES = {
    "victor": "Victor", "col": "Columbia", "zon": "Zonophone", "edison": "Edison", "okeh": "OKeh",
    "ok": "OKeh", "bruns": "Brunswick", "berliner": "Berliner", "gennett": "Gennett", "emerson": "Emerson",
}
RELATORS = {
    "prf": "performer", "voc": "vocalist", "sng": "singer", "itr": "instrumentalist", "cnd": "conductor",
    "cmp": "composer", "lyr": "lyricist", "aut": "author", "arr": "arranger", "spk": "speaker", "nrt": "narrator",
}
PERFORMING = ("vocalist", "singer", "musical group", "instrumentalist", "performer", "speaker", "narrator",
              "conductor")
ROLE_WORDS = set(PERFORMING) | {"composer", "lyricist", "author", "arranger"}
GROUP_WORDS = re.compile(r"\b(band|orchestra|quartet|quintet|trio|company|chorus|ensemble|singers|club|choir)\b", re.I)


class LocError(Exception):
    pass


class RateLimited(LocError):
    """loc.gov asked us to stop. Any request during its block restarts the hour."""


# ── configuration ────────────────────────────────────────────────────────────


@dataclass
class LocSource:
    name: str
    type: str  # loc | citizen_dj
    credit: str  # attribution after "Performer (Label Catalog, Year)."
    collection: str = ""
    collections: tuple[str, ...] = ()
    filters: tuple[str, ...] = ()
    description: str = ""
    note: str = ""
    opt_in: bool = False

    @property
    def url(self) -> str:
        if self.type == "loc":
            return f"{API_BASE}/collections/{self.collection}/"
        return CDJ_SITE

    def music_source(self) -> ml.Source:
        return ml.Source(name=self.name, license="public_domain", credit=self.credit, kind="stacks",
                         description=self.description, source_url=self.url)


@dataclass
class Screen:
    exclude_genres: tuple[str, ...] = ()
    exclude_terms: tuple[str, ...] = ()
    flag_genres: tuple[str, ...] = ()
    flag_terms: tuple[str, ...] = ()


def load_config(path: Path = SOURCES_PATH) -> tuple[dict[str, LocSource], Screen]:
    import yaml

    data = yaml.safe_load(path.read_text()) or {}
    sources: dict[str, LocSource] = {}
    for name, raw in (data.get("sources") or {}).items():
        raw = dict(raw or {})
        kind = raw.get("type")
        if kind not in ("loc", "citizen_dj"):
            raise LocError(f"{name}: type must be loc or citizen_dj")
        if kind == "loc" and not raw.get("collection"):
            raise LocError(f"{name}: a loc source needs a collection")
        if kind == "citizen_dj" and not raw.get("collections"):
            raise LocError(f"{name}: a citizen_dj source needs collections")
        if not raw.get("credit"):
            raise LocError(f"{name}: needs a credit")
        sources[name] = LocSource(
            name=name, type=kind, credit=str(raw["credit"]), collection=str(raw.get("collection", "")),
            collections=tuple(raw.get("collections") or ()), filters=tuple(raw.get("filters") or ()),
            description=str(raw.get("description", "")), note=" ".join(str(raw.get("note", "")).split()),
            opt_in=bool(raw.get("opt_in", False)),
        )
    screen_raw = data.get("screen") or {}
    screen = Screen(**{key: tuple(str(term).lower() for term in screen_raw.get(key) or ())
                       for key in ("exclude_genres", "exclude_terms", "flag_genres", "flag_terms")})
    return sources, screen


# ── loc.gov, paced ───────────────────────────────────────────────────────────


@dataclass
class AudioFile:
    url: str
    mimetype: str
    size: int | None = None


class LocClient:
    """Paced access to loc.gov, the Citizen DJ data and its clips."""

    def __init__(self, cache_dir: Path, *, api_base: str = API_BASE, cdj_data_base: str = CDJ_DATA_BASE,
                 cdj_asset_base: str = CDJ_ASSET_BASE, api_interval: float = API_INTERVAL,
                 media_interval: float = MEDIA_INTERVAL, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.cache_dir = Path(cache_dir)
        self.api_base = api_base.rstrip("/")
        self.cdj_data_base = cdj_data_base.rstrip("/")
        self.cdj_asset_base = cdj_asset_base.rstrip("/")
        self.intervals = {"api": api_interval, "media": media_interval}
        self.sleep = sleep
        self.clock = clock
        self._last = {"api": -math.inf, "media": -math.inf}
        self.api_calls = 0

    def _wait(self, lane: str) -> None:
        delay = self._last[lane] + self.intervals[lane] - self.clock()
        if delay > 0:
            self.sleep(delay)
        self._last[lane] = self.clock()

    def get_json(self, url: str, *, lane: str = "api", cache: Path | None = None) -> dict:
        if cache is not None and cache.exists():
            return json.loads(cache.read_text())
        self._wait(lane)
        if lane == "api":
            self.api_calls += 1
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read()
                kind = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise RateLimited("loc.gov answered 429 Too Many Requests") from exc
            raise LocError(f"{url}: HTTP {exc.code}") from exc
        if "json" not in kind and not body.lstrip().startswith((b"{", b"[")):
            raise RateLimited("loc.gov answered with a web page (likely a CAPTCHA) instead of JSON")
        data = json.loads(body)
        if cache is not None:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_name(cache.name + ".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(cache)
        return data

    def search(self, collection: str, *, filters: Sequence[str] = (), query: str | None = None,
               dates: tuple[int, int] | None = None, page_size: int = 100) -> Iterator[dict]:
        params: dict[str, Any] = {"fo": "json", "c": page_size, "at": "results,pagination"}
        if filters:
            params["fa"] = "|".join(filters)
        if query:
            params["q"] = query
        if dates:
            params["dates"] = f"{dates[0]}/{dates[1]}"
        page = 1
        while True:
            params["sp"] = page
            data = self.get_json(f"{self.api_base}/collections/{collection}/?{urllib.parse.urlencode(params)}")
            yield from data.get("results") or []
            pagination = data.get("pagination") or {}
            if not pagination.get("next"):
                return
            page += 1

    def item(self, item_id: str) -> dict:
        return self.get_json(f"{self.api_base}/item/{item_id}/?fo=json",
                             cache=self.cache_dir / "items" / f"{item_id}.json")

    def citizen_dj(self, uid: str) -> dict:
        return self.get_json(f"{self.cdj_data_base}/{uid}.json", lane="media",
                             cache=self.cache_dir / "citizen-dj" / f"{uid}.json")

    def clip_url(self, uid: str, filename: str) -> str:
        return f"{self.cdj_asset_base}/audio/samplepacks/{uid}/{urllib.parse.quote(filename)}.wav"

    def fetch_file(self, url: str, dest: Path, *, size: int | None = None,
                   log: Callable[[str], None] = print) -> bool:
        """Download into dest, resuming a .part file. False if it was already there."""
        if dest.exists() and (size is None or dest.stat().st_size == size):
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        for attempt in range(3):
            offset = part.stat().st_size if part.exists() else 0
            if size is not None and offset > size:
                part.unlink()
                offset = 0
            headers = {"User-Agent": USER_AGENT}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            self._wait("media")
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as response:
                    if response.headers.get("Content-Type", "").startswith("text/html"):
                        raise RateLimited("loc.gov answered with a web page (likely a CAPTCHA) instead of audio")
                    mode = "ab" if offset and response.status == 206 else "wb"
                    with part.open(mode) as handle:
                        shutil.copyfileobj(response, handle, 1 << 20)
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    raise RateLimited("loc.gov answered 429 Too Many Requests") from exc
                if not (exc.code == 416 and size is not None and offset == size):
                    log(f"    download error (HTTP {exc.code}), attempt {attempt + 1}/3")
                    continue
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                log(f"    download error ({exc}), attempt {attempt + 1}/3")
                self.sleep(2 ** attempt)
                continue
            if size is not None and part.stat().st_size != size:
                log(f"    incomplete ({part.stat().st_size} of {size} bytes), resuming")
                continue
            part.replace(dest)
            return True
        raise LocError(f"Could not download {url}")


def audio_files(doc: dict) -> list[AudioFile]:
    """Static audio files of an item record; streaming URLs are left out."""
    found: list[AudioFile] = []
    for resource in doc.get("resources") or []:
        for group in resource.get("files") or []:
            for entry in group if isinstance(group, list) else [group]:
                if not isinstance(entry, dict):
                    continue
                mimetype = str(entry.get("mimetype") or "")
                candidates = []
                if mimetype.startswith("audio/") and entry.get("url"):
                    candidates.append((entry["url"], mimetype, entry.get("size")))
                for derivative in entry.get("derivatives") or []:
                    if isinstance(derivative, dict) and derivative.get("derivativeUrl"):
                        url = derivative["derivativeUrl"]
                        candidates.append((url, _mimetype_of(url), None))
                for url, kind, size in candidates:
                    if "/storage-services/" not in url or not kind.startswith("audio/"):
                        continue
                    if url.startswith("//"):
                        url = "https:" + url
                    if not any(f.url == url for f in found):
                        found.append(AudioFile(url, kind, int(size) if str(size or "").isdigit() else None))
    return found


def _mimetype_of(url: str) -> str:
    ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
    return {".wav": "audio/wav", ".flac": "audio/flac", ".mp3": "audio/mpeg"}.get(ext, "")


def best_audio(doc: dict, prefer: str = "wav") -> AudioFile | None:
    order = ["audio/wav", "audio/x-wav", "audio/flac", "audio/mpeg"]
    if prefer == "mp3":
        order = ["audio/mpeg"] + order[:3]
    files = audio_files(doc)
    ranked = sorted(files, key=lambda f: order.index(f.mimetype) if f.mimetype in order else len(order))
    return ranked[0] if ranked else None


# ── metadata ─────────────────────────────────────────────────────────────────


@dataclass
class Recording:
    item_id: str
    url: str
    title: str
    recorded: str = ""  # session date as given: YYYY or YYYY-MM-DD
    published_year: int | None = None  # from an imprint, where there is one
    imprint: str = ""
    label: str = ""
    catalog: str = ""
    matrix: str = ""
    take: str = ""
    performers: list[str] = field(default_factory=list)  # primary first, conductors excluded
    conductors: list[str] = field(default_factory=list)
    composers: list[str] = field(default_factory=list)
    lyricists: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    other_titles: list[str] = field(default_factory=list)
    summary: str = ""
    description: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    language: str = ""
    rights_advisory: str = ""

    @property
    def recorded_year(self) -> int | None:
        match = re.match(r"\s*(1[89]\d\d)", self.recorded or "")
        return int(match.group(1)) if match else None

    @property
    def issue(self) -> str:
        return " ".join(part for part in (self.label, self.catalog) if part)

    @property
    def performer(self) -> str:
        return " and ".join(self.performers[:2]) or (self.conductors[:1] or ["Unknown performer"])[0]

    def credit(self, year: int | None, attribution: str) -> str:
        when = str(year) if year else (f"recorded {self.recorded}" if self.recorded else "year unknown")
        inside = ", ".join(part for part in (self.issue, when) if part)
        return f"{self.performer} ({inside}). {attribution}"


def display_name(name: str) -> str:
    """'Dunlap, Marguerite' -> 'Marguerite Dunlap'; groups are left alone."""
    corrected = re.search(r"\[i\.\s?e\.,?\s*([^\]]+)\]", name)
    if corrected:  # "Orchestrina Napoletana [i.e., Cibelli's Neapolitan Orchestra]"
        name = corrected.group(1)
    name = re.sub(r"\s*\[[^\]]*\]", "", name)
    name = re.sub(r"\s+", " ", name.strip().strip(",;").strip())
    name = re.sub(r"(?<=[^\W\d_]{2})\.$", "", name)  # a closing period, but not an initial's
    if name.count(",") == 1 and not GROUP_WORDS.search(name):
        last, first = (part.strip() for part in name.split(","))
        if first and last:
            return f"{first} {last}"
    return name


def parse_person(raw: str) -> tuple[str, str]:
    """('Marguerite Dunlap', 'vocalist') from 'Dunlap, Marguerite -- Vocalist -- Contralto';
    ('New York Military Band', 'performer') from 'New York Military Band. prf'."""
    text, role = raw.strip(), ""
    if " -- " in text:
        bits = [bit.strip() for bit in text.split(" -- ") if bit.strip()]
        text, role = bits[0], (bits[1].lower() if len(bits) > 1 else "")
    else:
        match = re.search(r"[.,]\s*([a-z]{3})\.?$", text)
        if match and match.group(1) in RELATORS:
            role, text = RELATORS[match.group(1)], text[:match.start()]
        else:  # "Fields, Arthur, performer."
            match = re.search(r",\s*([A-Za-z ]+?)\.?$", text)
            if match and match.group(1).lower() in ROLE_WORDS:
                role, text = match.group(1).lower(), text[:match.start()]
    text = re.sub(r",?\s*(?:approximately\s*)?\d{4}\??-(?:\d{4}\??)?\.?$", "", text.strip())  # life dates
    return display_name(text), role


def sort_people(raw: Sequence[str], primary: set[str]) -> tuple[list[str], list[str], list[str], list[str]]:
    """(performers, conductors, composers, lyricists), primary performers first."""
    people = [parse_person(name) for name in raw if name and name.strip()]
    labelled = any(role in PERFORMING for _, role in people)
    performers: list[tuple[str, str]] = []
    composers: list[str] = []
    lyricists: list[str] = []
    for name, role in people:
        # Unlabelled names next to labelled performers are the writers (Edison's records).
        if role == "composer" or (role == "" and labelled):
            if name not in composers:
                composers.append(name)
        elif role in ("lyricist", "author"):
            if name not in lyricists:
                lyricists.append(name)
        elif role in PERFORMING or role == "":
            if all(name != existing for existing, _ in performers):
                performers.append((name, role))
    wanted = {display_name(p).lower() for p in primary}

    def rank(entry: tuple[int, tuple[str, str]]) -> tuple[int, int]:
        index, (name, _) = entry
        return (0 if name.lower() in wanted else 1, index)

    ordered = sorted(enumerate(performers), key=rank)
    players = [name for _, (name, role) in ordered if role != "conductor"]
    conductors = [name for _, (name, role) in ordered if role == "conductor"]
    return players, conductors, composers, lyricists


def format_catalog(raw: str) -> str:
    """'2380x' -> '2380-X', 'a1234' -> 'A1234', '17156' -> '17156'."""
    match = re.fullmatch(r"(\d+)([a-z])", raw)
    return f"{match.group(1)}-{match.group(2).upper()}" if match else raw.upper()


def parse_recording(record: dict) -> Recording:
    """From a search result or an item record's "item" block."""
    nested = record.get("item") if isinstance(record.get("item"), dict) else {}

    def first(*keys: str) -> str:
        for key in keys:
            for source in (record, nested):
                value = source.get(key)
                if isinstance(value, list):
                    value = next((v for v in value if isinstance(v, (str, int)) and str(v).strip()), "")
                if isinstance(value, (str, int)) and str(value).strip():
                    return str(value).strip()
        return ""

    def strings(*keys: str) -> list[str]:
        out: list[str] = []
        for key in keys:
            for source in (record, nested):
                value = source.get(key)
                for v in [value] if isinstance(value, str) else (value or []):
                    if isinstance(v, str) and v.strip() and v.strip() not in out:
                        out.append(v.strip())
        return out

    match = ITEM_ID.search(first("url", "id"))
    item_id = match.group(1) if match else first("id")
    rec = Recording(item_id=item_id, url=f"{API_BASE}/item/{item_id}/", title=first("title") or "Untitled")
    rec.recorded = first("recording_date", "date")
    imprint_label = ""
    imprint = first("created_published")
    if imprint:
        years = re.findall(r"\b(1[89]\d\d)\b", imprint)
        if years:
            rec.published_year, rec.imprint = int(years[-1]), imprint.rstrip(". ")
        named = re.search(r":\s*([^,:\[]+?)\s*,\s*\[?c?1[89]\d\d", imprint)
        imprint_label = named.group(1).strip() if named else ""
    rec.description = strings("description")
    rec.notes = strings("notes")
    rec.summary = first("summary")
    rec.other_titles = strings("other_title", "alternative_title")
    rec.genres = [g.lower() for g in strings("subject_genre", "genre", "subject")]
    rec.language = first("language")
    rec.rights_advisory = first("rights_advisory")

    label, number = first("recording_label"), first("recording_catalog_number")
    if not (label and number):
        for text in rec.description:
            found = re.match(r"^(.+?) ([A-Z]{0,2}\d[\w-]*)\. Matrix/Take", text)
            if found:
                label, number = found.group(1), found.group(2)
                break
    if not (label and number):
        for text in strings("call_number", "shelf_id"):
            shelf = re.match(r"^(.*?[A-Za-z].*?)\s+([A-Z]{0,2}\d[\w-]*)$", text)
            if shelf and "://" not in text:
                label, number = shelf.group(1), shelf.group(2)
                break
    if not (label and number):
        coded = re.match(r"^[a-z]+_([a-z]+)_([0-9a-z]+)_\d+_", first("file_id"))
        if coded:
            label = LABEL_CODES.get(coded.group(1), coded.group(1).title())
            number = format_catalog(coded.group(2))
    rec.label, rec.catalog = (label or imprint_label), number
    rec.matrix = re.sub(r"\s*\(Matrix ID\)", "", first("recording_matrix_number"))
    rec.take = first("recording_take_number")

    raw_people = strings("contributor_names") or [p for p in (nested.get("contributors") or []) if isinstance(p, str)]
    primary = {p.lower() for p in strings("contributor_primary")}
    rec.performers, rec.conductors, rec.composers, rec.lyricists = sort_people(raw_people, primary)
    if not raw_people:
        rec.performers = [display_name(name).title() for name in strings("contributor")]
    return rec


# ── the three checks ─────────────────────────────────────────────────────────


@dataclass
class Decision:
    action: str  # take | skip | exclude
    reason: str = ""
    year: int | None = None
    composition_year: int | None = None
    quarantine: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


def _has_term(text: str, term: str) -> bool:
    return re.search(rf"(?<![a-z]){re.escape(term)}(?:s|es)?(?![a-z])", text) is not None


def screen_recording(rec: Recording, screen: Screen) -> tuple[list[str], list[str]]:
    """(reasons to keep it out, warnings for the reviewer)."""
    text = " ".join([rec.title, *rec.other_titles, rec.summary, *rec.description, *rec.notes]).lower()
    genres = set(rec.genres)
    excluded = [f"genre: {g}" for g in screen.exclude_genres if g in genres]
    excluded += [f'"{term}"' for term in screen.exclude_terms if _has_term(text, term)]
    flags = [f"genre: {g}" for g in screen.flag_genres if g in genres]
    flags += [f'"{term}"' for term in screen.flag_terms if _has_term(text, term)]
    return excluded, flags


def judge(rec: Recording, screen: Screen, today: date, *, include: bool = False) -> Decision:
    cutoff = cat.recording_pd_cutoff(today)
    excluded, flags = screen_recording(rec, screen)
    if excluded and not include:
        return Decision("exclude", reason="; ".join(excluded))
    decision = Decision("take", quarantine=[VERIFY_CONTENT], flags=excluded + flags)
    if excluded:
        decision.notes.append("brought in for review despite the screen (" + "; ".join(excluded) + ")")

    recorded = rec.recorded_year
    if rec.published_year is not None:
        if rec.published_year > cutoff:
            return Decision("skip", reason=f"published {rec.published_year}: public domain on "
                                           f"January 1, {rec.published_year + 101}")
        decision.year = rec.published_year
        decision.notes.append(f"published {rec.published_year}" + (f" ({rec.imprint})" if rec.imprint else ""))
    elif recorded is None:
        return Decision("skip", reason="no date on file (no year, no airplay)")
    elif recorded > cutoff:
        return Decision("skip", reason=f"recorded {rec.recorded}, so published {recorded} or later: "
                                       f"not public domain yet")
    elif recorded == cutoff:
        decision.quarantine.append(VERIFY_PUBLICATION)
        decision.notes.append(
            f"recorded {rec.recorded}: public domain only if issued in {cutoff} (a {cutoff + 1} release waits "
            f"for January 1, {cutoff + 102}). Check the release date in DAHR, then set the year.")
    else:
        decision.year = recorded
        decision.notes.append(f"publication year from the session date ({rec.recorded}); records were issued "
                              "weeks to months after recording")

    if rec.catalog:
        decision.notes.append(f"issued as {rec.issue}")
    elif rec.published_year is None:
        decision.year = None
        if VERIFY_PUBLICATION not in decision.quarantine:
            decision.quarantine.append(VERIFY_PUBLICATION)
        decision.notes.append("no issue number on file: confirm the record was published, then set the year")

    decision.composition_year = rec.published_year or recorded
    if decision.flags:
        decision.notes.append("screen flags: " + "; ".join(decision.flags))
    return decision


def license_evidence(rec: Recording, year: int | None, today: date) -> str:
    cutoff = cat.recording_pd_cutoff(today)
    if year is None:
        return (f"Publication not yet confirmed (recorded {rec.recorded or 'on an unknown date'}). US sound "
                f"recordings published in or before {cutoff} are public domain. {rec.url}")
    since = 2022 if year < 1923 else year + 101
    issued = f" as {rec.issue}" if rec.issue else ""
    return (f"US sound recording published {year}{issued}; public domain since January 1, {since} under the "
            f"Music Modernization Act. {rec.url}")


def planned_work(source: LocSource, rec: Recording, decision: Decision, master: Path, today: date, *,
                 title: str | None = None, track_name: str | None = None, length: float | None = None,
                 extra_tags: Iterable[str] = ()) -> ml.PlannedWork:
    title = title or rec.title
    tags = {"stacks", "loc", source.name, *extra_tags, *rec.genres}
    if decision.flags:
        tags.add("screen-flag")
    review = list(decision.notes)
    review += [f"LOC: {line}" for line in rec.description]
    if rec.rights_advisory:
        review.append(f"LOC: {rec.rights_advisory}")
    review += ["listen before approving: lyrics are not screened automatically", rec.url]
    return ml.PlannedWork(
        source=source.music_source(), title=title,
        tracks=[ml.Track(name=track_name or rec.item_id, title=title, master=master, length=length)],
        parts=[None], composer=", ".join(rec.composers) or None,
        performer=rec.performer if rec.performers else None, tags=tags,
        composition_year=decision.composition_year,
        composition_basis=f"no later than its recording ({rec.recorded})" if decision.composition_year else "",
        review=review, quarantine=list(decision.quarantine), year=decision.year, source_url=rec.url,
        credit_line=rec.credit(decision.year, source.credit),
        license_evidence=license_evidence(rec, decision.year, today),
    )


# ── fetching ─────────────────────────────────────────────────────────────────


@dataclass
class Outcome:
    key: str
    title: str
    action: str  # added | planned | excluded | skipped | failed
    reason: str = ""
    year: int | None = None
    credit: str = ""
    flags: list[str] = field(default_factory=list)
    work_ids: list[int] = field(default_factory=list)
    url: str = ""


class Ledger:
    """What a source has already seen, so reruns don't spend API calls on it."""

    FINAL = ("added", "excluded", "skipped")

    def __init__(self, path: Path):
        self.path = path
        try:
            self.entries: dict[str, dict] = json.loads(path.read_text()) if path.exists() else {}
        except json.JSONDecodeError:
            self.entries = {}

    def done(self, key: str) -> bool:
        return self.entries.get(key, {}).get("outcome") in self.FINAL

    def record(self, outcome: Outcome) -> None:
        self.entries[outcome.key] = {
            "outcome": outcome.action, "title": outcome.title, "reason": outcome.reason, "url": outcome.url,
            "work_ids": outcome.work_ids, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.entries, indent=1, ensure_ascii=False))
        tmp.replace(self.path)


def _line(outcome: Outcome) -> str:
    mark = {"added": "+", "planned": "+", "excluded": "x", "skipped": "-", "failed": "!"}[outcome.action]
    detail = outcome.credit if outcome.action in ("added", "planned") else outcome.reason
    flags = f"  [flag: {'; '.join(outcome.flags)}]" if outcome.flags else ""
    return f"  {mark} {outcome.title[:60]}: {detail}{flags}"


def fetch_loc(client: LocClient, catalog: Catalog, source: LocSource, library: Path, screen: Screen, *,
              query: str | None = None, years: tuple[int, int] | None = None, limit: int = 25,
              dry_run: bool = False, include: Sequence[str] = (), prefer: str = "wav",
              today: date | None = None, log: Callable[[str], None] = print) -> list[Outcome]:
    today = today or date.today()
    years = years or (1890, cat.recording_pd_cutoff(today))
    ledger = Ledger(client.cache_dir / "ledger" / f"{source.name}.json")
    outcomes: list[Outcome] = []
    taken = 0

    def note(outcome: Outcome) -> None:
        outcomes.append(outcome)
        log(_line(outcome))
        if not dry_run and outcome.action != "planned":
            ledger.record(outcome)

    for result in client.search(source.collection, filters=source.filters, query=query, dates=years):
        if limit and taken >= limit:
            break
        rec = parse_recording(result)
        forced = rec.item_id in include
        if ledger.done(rec.item_id) and not forced:
            continue
        decision = judge(rec, screen, today, include=forced)
        if decision.action != "take":
            note(Outcome(rec.item_id, rec.title, "excluded" if decision.action == "exclude" else "skipped",
                         reason=decision.reason, url=rec.url))
            continue
        if dry_run:
            note(Outcome(rec.item_id, rec.title, "planned", year=decision.year, flags=decision.flags,
                         credit=rec.credit(decision.year, source.credit), url=rec.url))
            taken += 1
            continue
        try:
            doc = client.item(rec.item_id)
            rec = parse_recording(doc.get("item") or {})
            decision = judge(rec, screen, today, include=forced)
            if decision.action != "take":
                note(Outcome(rec.item_id, rec.title, "excluded" if decision.action == "exclude" else "skipped",
                             reason=decision.reason, url=rec.url))
                continue
            audio = best_audio(doc, prefer)
            if audio is None:
                note(Outcome(rec.item_id, rec.title, "skipped", reason="loc.gov serves no downloadable audio file",
                             url=rec.url))
                continue
            ext = Path(urllib.parse.urlparse(audio.url).path).suffix.lower() or ".audio"
            master = library / "masters" / source.name / f"{rec.item_id}{ext}"
            client.fetch_file(audio.url, master, size=audio.size, log=log)
            work = planned_work(source, rec, decision, master, today, length=cat.probe(master).duration_sec)
            result_ = ml.ingest(catalog, work, library / "air", jobs=1, spectral=False, log=log)
        except RateLimited:
            raise
        except (ml.LibraryError, cat.CatalogError, LocError, OSError, ValueError, KeyError) as exc:
            note(Outcome(rec.item_id, rec.title, "failed", reason=str(exc)[:300], url=rec.url))
            continue
        note(Outcome(rec.item_id, rec.title, "added", year=decision.year, flags=decision.flags,
                     credit=work.credit, work_ids=[result_.work_id] if result_.work_id else [], url=rec.url))
        taken += 1
    return outcomes


def fetch_citizen_dj(client: LocClient, catalog: Catalog, source: LocSource, library: Path, screen: Screen, *,
                     collections: Sequence[str] | None = None, limit: int = 25, dry_run: bool = False,
                     include: Sequence[str] = (), today: date | None = None,
                     log: Callable[[str], None] = print) -> list[Outcome]:
    today = today or date.today()
    uids = list(collections or source.collections)
    unknown = [uid for uid in uids if uid not in source.collections]
    if unknown:
        raise LocError(f"{source.name} has no collection {', '.join(unknown)}; it has {', '.join(source.collections)}")
    ledger = Ledger(client.cache_dir / "ledger" / f"{source.name}.json")
    outcomes: list[Outcome] = []
    taken = 0

    def note(outcome: Outcome) -> None:
        outcomes.append(outcome)
        log(_line(outcome))
        if not dry_run and outcome.action != "planned":
            ledger.record(outcome)

    for uid in uids:
        data = client.citizen_dj(uid)
        log(f"{uid}: {len(data.get('items') or [])} recordings")
        for entry in data.get("items") or []:
            if limit and taken >= limit:
                return outcomes
            item_id = str(entry.get("id") or "")
            key = f"{uid}/{item_id}"
            forced = item_id in include
            if not item_id or (ledger.done(key) and not forced):
                continue
            try:
                rec = parse_recording(client.item(item_id).get("item") or {})
            except RateLimited:
                raise
            except (LocError, OSError, ValueError) as exc:
                note(Outcome(key, str(entry.get("title", item_id)), "failed", reason=str(exc)[:300]))
                continue
            decision = judge(rec, screen, today, include=forced)
            if decision.action != "take":
                note(Outcome(key, rec.title, "excluded" if decision.action == "exclude" else "skipped",
                             reason=decision.reason, url=rec.url))
                continue
            credit = rec.credit(decision.year, source.credit)
            clips = entry.get("clips") or []
            if dry_run:
                note(Outcome(key, rec.title, "planned", year=decision.year, flags=decision.flags,
                             credit=f"{credit} [{len(clips)} clips]", url=rec.url))
                taken += 1
                continue
            work_ids: list[int] = []
            try:
                for number, clip in enumerate(clips, start=1):
                    name = str(clip["filename"])
                    master = library / "masters" / source.name / uid / f"{name}.wav"
                    client.fetch_file(client.clip_url(uid, name), master, log=log)
                    title = f"{rec.title} (clip {number}, at {clip.get('timeStamp', '?')})"
                    work = planned_work(source, rec, decision, master, today, title=title,
                                        track_name=f"{uid}/{name}", length=cat.probe(master).duration_sec,
                                        extra_tags=("remix-clip", "citizen-dj", uid))
                    result_ = ml.ingest(catalog, work, library / "air", jobs=1, spectral=False, log=log)
                    if result_.work_id:
                        work_ids.append(result_.work_id)
            except RateLimited:
                raise
            except (ml.LibraryError, cat.CatalogError, LocError, OSError, ValueError, KeyError) as exc:
                note(Outcome(key, rec.title, "failed", reason=str(exc)[:300], url=rec.url))
                continue
            note(Outcome(key, rec.title, "added", year=decision.year, flags=decision.flags,
                         credit=f"{credit} [{len(work_ids)} clips]", work_ids=work_ids, url=rec.url))
            taken += 1
    return outcomes


def write_report(library: Path, source: LocSource, outcomes: Sequence[Outcome]) -> Path:
    import yaml

    path = library / "reports" / f"{source.name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{k: v for k, v in vars(o).items() if v not in ("", [], None)} for o in outcomes]
    path.write_text(yaml.safe_dump({"source": source.name, "run": rows}, sort_keys=False, allow_unicode=True))
    return path


# ── the content screen, by hand ──────────────────────────────────────────────


def review_queue(catalog: Catalog, sources: Iterable[str]) -> list[cat.Work]:
    names = set(sources)
    return [w for w in catalog.works(status="quarantined") if w.source in names]


def approve(catalog: Catalog, work_ids: Sequence[int], reasons: Sequence[str] = (), *, by: str | None = None,
            today: date | None = None) -> list[str]:
    """Put works on air after someone listened. Publication doubts need --reason publication."""
    today = today or date.today()
    allowed = {VERIFY_CONTENT} | {f"verify-{reason}" for reason in reasons}
    messages = []
    for work_id in work_ids:
        try:
            work = catalog.work(work_id)
        except cat.CatalogError as exc:
            messages.append(f"{work_id}: {exc}")
            continue
        if work.status != "quarantined":
            messages.append(f"{work_id}: already {work.status}")
            continue
        markers = set(MARKER.findall(work.notes or ""))
        missing = markers - allowed
        if missing:
            needed = ", ".join(sorted(m.removeprefix("verify-") for m in missing))
            messages.append(f"{work_id}: also needs a check of: {needed} (then --reason {needed})")
            continue
        if VERIFY_PUBLICATION in markers and work.year is None:
            messages.append(f"{work_id}: set the publication year first: python3 mac/catalog.py edit {work_id} --year YYYY")
            continue
        signed = f" by {by}" if by else ""
        checked = ", ".join(sorted(m.removeprefix("verify-") for m in markers)) or "content"
        catalog.update_work(work_id, notes=f"{work.notes or ''}\napproved after listening ({checked}) on {today}{signed}")
        catalog.set_status(work_id, "active")
        problems = catalog.airability(catalog.work(work_id), today)
        messages.append(f"{work_id}: approved: {work.title}" + (f" (not airable yet: {'; '.join(problems)})" if problems else ""))
    return messages


def reject(catalog: Catalog, work_ids: Sequence[int], why: str, *, by: str | None = None,
           today: date | None = None) -> list[str]:
    today = today or date.today()
    messages = []
    for work_id in work_ids:
        try:
            work = catalog.work(work_id)
        except cat.CatalogError as exc:
            messages.append(f"{work_id}: {exc}")
            continue
        signed = f" by {by}" if by else ""
        catalog.set_status(work_id, "rejected",
                           note=f"{work.notes or ''}\nrejected in the content screen on {today}{signed}: {why}")
        messages.append(f"{work_id}: rejected: {work.title}")
    return messages


# ── command line ─────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    from station_config import load_station_config

    parser = argparse.ArgumentParser(description="Pull Library of Congress recordings for The Stacks")
    parser.add_argument("--station", default="wit-next")
    parser.add_argument("--db", type=Path, help="catalog database (default: the station's)")
    parser.add_argument("--library", type=Path, help="library folder (default: the station's)")
    parser.add_argument("--sources", type=Path, default=SOURCES_PATH)
    parser.add_argument("--api-base", default=API_BASE, help=argparse.SUPPRESS)
    parser.add_argument("--cdj-data-base", default=CDJ_DATA_BASE, help=argparse.SUPPRESS)
    parser.add_argument("--cdj-asset-base", default=CDJ_ASSET_BASE, help=argparse.SUPPRESS)
    parser.add_argument("--api-interval", type=float, default=API_INTERVAL, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("sources", help="list the configured sources")
    p_fetch = sub.add_parser("fetch", help="screen a source and bring its items in, quarantined")
    p_fetch.add_argument("source")
    p_fetch.add_argument("--query", help="loc.gov search words, e.g. banjo")
    p_fetch.add_argument("--from", dest="year_from", type=int, help="earliest recording year")
    p_fetch.add_argument("--to", dest="year_to", type=int, help="latest recording year")
    p_fetch.add_argument("--collection", action="append", help="citizen_dj: only this collection (repeatable)")
    p_fetch.add_argument("--limit", type=int, default=25, help="items to bring in this run; 0 for no limit")
    p_fetch.add_argument("--include", action="append", default=[], metavar="ITEM_ID",
                         help="bring in an item the screen kept out, for review")
    p_fetch.add_argument("--prefer", choices=("wav", "mp3"), default="wav", help="file to take when loc.gov has both")
    p_fetch.add_argument("--opt-in", action="store_true", help="allow an opt-in source")
    p_fetch.add_argument("--dry-run", action="store_true", help="screen only; download nothing")
    p_review = sub.add_parser("review", help="list what waits for the content screen")
    p_review.add_argument("--source")
    p_review.add_argument("--limit", type=int, default=50)
    p_approve = sub.add_parser("approve", help="put works on air after listening to them")
    p_approve.add_argument("work_ids", nargs="+", type=int)
    p_approve.add_argument("--reason", action="append", default=[], choices=("publication",),
                           help="also checked the publication year")
    p_approve.add_argument("--by", help="who listened")
    p_reject = sub.add_parser("reject", help="keep works off the air")
    p_reject.add_argument("work_ids", nargs="+", type=int)
    p_reject.add_argument("--why", required=True)
    p_reject.add_argument("--by")
    p_excluded = sub.add_parser("excluded", help="what the screen kept out, with loc.gov links")
    p_excluded.add_argument("--source")
    args = parser.parse_args(argv)

    sources, screen = load_config(args.sources)
    if args.cmd == "sources":
        for name, s in sources.items():
            where = s.collection if s.type == "loc" else f"{len(s.collections)} Citizen DJ collections"
            flag = " (opt-in)" if s.opt_in else ""
            print(f"{name:20s} {s.type:11s} {where:55s} {s.description}{flag}")
        return 0

    station = load_station_config(args.station)
    library = (args.library or station.library_dir).expanduser()
    client = LocClient(library / "loc-cache", api_base=args.api_base, cdj_data_base=args.cdj_data_base,
                       cdj_asset_base=args.cdj_asset_base, api_interval=args.api_interval)

    if args.cmd == "excluded":
        for name in [args.source] if args.source else sources:
            entries = Ledger(client.cache_dir / "ledger" / f"{name}.json").entries
            for key, entry in entries.items():
                if entry.get("outcome") == "excluded":
                    print(f"{name:14s} {key:32s} {entry.get('title', '')[:50]:50s} {entry.get('reason', '')}  {entry.get('url', '')}")
        return 0

    with Catalog(args.db or station.catalog_db_path) as catalog:
        if args.cmd == "fetch":
            source = sources.get(args.source)
            if source is None:
                parser.error(f"unknown source {args.source}; see `sources`")
            if source.opt_in and not args.opt_in:
                print(f"{source.name} is opt-in. {source.note}")
                return 2
            mode = "screening" if args.dry_run else "fetching"
            print(f"{source.name}: {mode} (loc.gov allows 20 requests a minute, so this runs at one every "
                  f"{args.api_interval:g} s)")
            outcomes: list[Outcome] = []
            try:
                if source.type == "loc":
                    years = None
                    if args.year_from or args.year_to:
                        cutoff = cat.recording_pd_cutoff(date.today())
                        years = (args.year_from or 1890, min(args.year_to or cutoff, cutoff))
                    outcomes = fetch_loc(client, catalog, source, library, screen, query=args.query, years=years,
                                         limit=args.limit, dry_run=args.dry_run, include=args.include,
                                         prefer=args.prefer)
                else:
                    outcomes = fetch_citizen_dj(client, catalog, source, library, screen,
                                                collections=args.collection, limit=args.limit,
                                                dry_run=args.dry_run, include=args.include)
            except RateLimited as exc:
                print(f"\nStopped: {exc}. Wait an hour before the next run: any request during loc.gov's block "
                      "restarts it. What came in is kept, and the next run carries on.")
                return 3
            except LocError as exc:
                print(f"{source.name}: {exc}")
                return 1
            counts: dict[str, int] = {}
            for o in outcomes:
                counts[o.action] = counts.get(o.action, 0) + 1
            summary = ", ".join(f"{n} {action}" for action, n in counts.items()) or "nothing new"
            print(f"\n{source.name}: {summary}; {client.api_calls} loc.gov API calls")
            if not args.dry_run:
                print(f"report: {write_report(library, source, outcomes)}")
                if counts.get("added"):
                    print("Everything added is quarantined until someone listens: python3 tools/pull_loc.py review")
            return 0

        names = [args.source] if args.source else list(sources)
        if args.cmd == "review":
            queue = review_queue(catalog, names)
            for work in queue[:args.limit]:
                markers = ", ".join(m.removeprefix("verify-") for m in MARKER.findall(work.notes or ""))
                flags = next((line.removeprefix("screen flags: ") for line in (work.notes or "").splitlines()
                              if line.startswith("screen flags: ")), "")
                print(f"{work.id:5d}  {work.title[:60]}")
                print(f"       {work.credit_line}")
                print(f"       check: {markers}" + (f"   flags: {flags}" if flags else "") + f"   {work.source_url or ''}")
            print(f"{len(queue)} waiting" + (f" (showing {args.limit})" if len(queue) > args.limit else ""))
            return 0
        if args.cmd == "approve":
            for message in approve(catalog, args.work_ids, args.reason, by=args.by):
                print(message)
            return 0
        if args.cmd == "reject":
            for message in reject(catalog, args.work_ids, args.why, by=args.by):
                print(message)
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
