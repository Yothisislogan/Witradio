#!/usr/bin/env python3
"""Pull cleared lossless classical into the WIT-FM library and catalog.

Sources live in config/music_sources.yaml (Internet Archive items checked on
Nov 18, 2026). Local folders cover the rest: Musopen site downloads and the
Marine Band CD rips. For every source the pipeline:

1. downloads the original lossless files (resumable, checked against the
   Archive's md5) into library/masters/<source>/
2. checks each master's spectrum for a lossy encoder's cutoff (fake lossless)
3. makes a 16-bit/44.1 kHz FLAC air copy in library/air/<source>/: a copy if
   the master already is one, otherwise resampled and dithered once
4. groups files into works (a symphony's movements, a prelude and its fugue)
   and fills in composer, form, tags and the composition's year
5. measures one loudness gain per work and adds it to the catalog with its
   license, evidence and credit line
6. writes library/reports/<source>.yaml listing what needs a human

Anything doubtful arrives quarantined, never on air: a source marked
`verify`, a steep spectral cutoff, or a piece published after its composer
died (it may not be public domain). `approve` clears a source after review.

    python3 tools/pull_music.py sources
    python3 tools/pull_music.py fetch all --dry-run
    python3 tools/pull_music.py fetch open-goldberg open-wtc-1
    python3 tools/pull_music.py local ~/Music/musopen --source musopen-site \\
        --license public_domain --credit "{performer} via Musopen"
    python3 tools/pull_music.py approve ishizaka-art-of-fugue
    python3 tools/pull_music.py report
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import catalog as cat  # noqa: E402
from catalog import Catalog  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCES_PATH = PROJECT_ROOT / "config" / "music_sources.yaml"
ARCHIVE_BASE = os.environ.get("WRIT_ARCHIVE_BASE", "https://archive.org")
USER_AGENT = "WIT-FM-library/1.0"
LOSSLESS_EXTENSIONS = {".flac", ".wav", ".aif", ".aiff"}
AIR_RATE = 44100
# Roughly what a 16/44.1 FLAC of acoustic music takes, for disk estimates.
AIR_BYTES_PER_HOUR = 380e6

# Quarantine reasons, kept in the work's notes so `approve` can clear them.
VERIFY_LICENSE = "verify-license"
VERIFY_PUBLICATION = "verify-publication"
VERIFY_AUDIO = "verify-audio"


class LibraryError(RuntimeError):
    pass


# ── composers ────────────────────────────────────────────────────────────────

# (full name, year of death, extra spellings). A death year on or before the
# public-domain cutoff bounds when the music was written; a later one means
# someone has to look up the publication year.
COMPOSERS: list[tuple[str, int, tuple[str, ...]]] = [
    ("Johann Sebastian Bach", 1750, ("j s bach", "js bach", "johann s bach")),
    ("Carl Philipp Emanuel Bach", 1788, ("c p e bach", "cpe bach")),
    ("Ludwig van Beethoven", 1827, ()),
    ("Alexander Borodin", 1887, ()),
    ("Johannes Brahms", 1897, ()),
    ("Frédéric Chopin", 1849, ("fryderyk chopin",)),
    ("Antonín Dvořák", 1904, ()),
    ("Edvard Grieg", 1907, ("edward grieg",)),
    ("Joseph Haydn", 1809, ("franz joseph haydn",)),
    ("Felix Mendelssohn", 1847, ("felix mendelssohn bartholdy",)),
    ("Fanny Mendelssohn", 1847, ("fanny hensel",)),
    ("Wolfgang Amadeus Mozart", 1791, ("w a mozart",)),
    ("Nikolai Rimsky-Korsakov", 1908, ()),
    ("Franz Schubert", 1828, ()),
    ("Bedřich Smetana", 1884, ()),
    ("Josef Suk", 1935, ()),
    ("Pyotr Ilyich Tchaikovsky", 1893, ("peter tchaikovsky", "p i tchaikovsky")),
    ("Antonio Vivaldi", 1741, ()),
    ("George Frideric Handel", 1759, ("georg friedrich handel",)),
    ("Robert Schumann", 1856, ()),
    ("Clara Schumann", 1896, ()),
    ("Franz Liszt", 1886, ()),
    ("Claude Debussy", 1918, ()),
    ("Erik Satie", 1925, ()),
    ("Maurice Ravel", 1937, ()),
    ("Sergei Rachmaninoff", 1943, ("sergei rachmaninov",)),
    ("John Philip Sousa", 1932, ()),
    ("Edward Elgar", 1934, ()),
    ("Gustav Holst", 1934, ()),
    ("Gabriel Fauré", 1924, ()),
    ("Camille Saint-Saëns", 1921, ()),
    ("Gustav Mahler", 1911, ()),
    ("Anton Bruckner", 1896, ()),
    ("Giuseppe Verdi", 1901, ()),
    ("Giacomo Puccini", 1924, ()),
    ("Richard Wagner", 1883, ()),
    ("Georges Bizet", 1875, ()),
    ("Hector Berlioz", 1869, ()),
    ("Modest Mussorgsky", 1881, ()),
    ("Henry Purcell", 1695, ()),
    ("Claudio Monteverdi", 1643, ()),
    ("Arcangelo Corelli", 1713, ()),
    ("Domenico Scarlatti", 1757, ()),
    ("Alessandro Scarlatti", 1725, ()),
    ("Georg Philipp Telemann", 1767, ()),
    ("Johann Pachelbel", 1706, ()),
    ("Niccolò Paganini", 1840, ()),
    ("Gioachino Rossini", 1868, ()),
    ("Carl Maria von Weber", 1826, ()),
    ("Isaac Albéniz", 1909, ()),
    ("Enrique Granados", 1916, ()),
    ("Alexander Scriabin", 1915, ()),
    ("Scott Joplin", 1917, ()),
    ("Carl Nielsen", 1931, ()),
    ("Jean Sibelius", 1957, ()),
    ("Leoš Janáček", 1928, ()),
    ("César Franck", 1890, ()),
    ("Johann Strauss II", 1899, ("johann strauss jr", "johann strauss")),
    ("Richard Strauss", 1949, ()),
    ("Arthur Sullivan", 1900, ()),
    ("Max Bruch", 1920, ()),
    ("Luigi Boccherini", 1805, ()),
    ("Muzio Clementi", 1832, ()),
    ("Mikhail Glinka", 1857, ()),
    ("Jean-Philippe Rameau", 1764, ()),
    ("François Couperin", 1733, ()),
    ("Dieterich Buxtehude", 1707, ()),
    ("William Byrd", 1623, ()),
    ("Thomas Tallis", 1585, ()),
    ("Giovanni Pierluigi da Palestrina", 1594, ()),
    ("Tomaso Albinoni", 1751, ()),
    ("Gaetano Donizetti", 1848, ()),
    ("Samuel Coleridge-Taylor", 1912, ()),
    ("Amy Beach", 1944, ()),
    ("Florence Price", 1953, ()),
]


def fold(text: str | None) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _composer_index() -> tuple[dict[str, tuple[str, int]], dict[str, list[tuple[str, int]]]]:
    exact: dict[str, tuple[str, int]] = {}
    by_surname: dict[str, list[tuple[str, int]]] = collections.defaultdict(list)
    for name, died, aliases in COMPOSERS:
        exact[fold(name)] = (name, died)
        for alias in aliases:
            exact[fold(alias)] = (name, died)
        by_surname[fold(name).split(" ")[-1]].append((name, died))
    return exact, by_surname


_EXACT, _SURNAMES = _composer_index()


def lookup_composer(name: str | None) -> tuple[str, int] | None:
    """(canonical name, year of death), or None if unknown or ambiguous."""
    key = fold(name)
    if not key:
        return None
    if key in _EXACT:
        return _EXACT[key]
    candidates = _SURNAMES.get(key.split(" ")[-1], [])
    if len(candidates) == 1:
        return candidates[0]
    return None  # e.g. plain "Schumann" or "Strauss": a first name is needed


# ── titles, forms and tags ───────────────────────────────────────────────────

FORMS: list[tuple[str, str, tuple[str, ...]]] = [
    ("prelude and fugue", "prelude_and_fugue", ("keyboard",)),
    ("symphon", "symphony", ("symphonic", "orchestra")),
    ("concerto", "concerto", ()),
    ("overture", "overture", ("orchestra",)),
    ("string quartet", "quartet", ("strings", "chamber")),
    ("quartet", "quartet", ("chamber",)),
    ("quintet", "quintet", ("chamber",)),
    ("trio", "trio", ("chamber",)),
    ("cello sonata", "sonata", ("strings", "cello", "chamber")),
    ("violin sonata", "sonata", ("strings", "violin", "chamber")),
    ("sonata", "sonata", ()),
    ("variation", "variations", ()),
    ("art of the fugue", "fugue", ()),
    ("suite", "suite", ()),
    ("requiem", "requiem", ("sacred", "choral", "requiem")),
    ("mass", "mass", ("sacred", "choral")),
    ("fugue", "fugue", ()),
    ("prelude", "prelude", ()),
    ("nocturne", "nocturne", ("quiet",)),
    ("berceuse", "lullaby", ("quiet",)),
    ("lullaby", "lullaby", ("quiet",)),
    ("mazurka", "mazurka", ()),
    ("waltz", "waltz", ()),
    ("valse", "waltz", ()),
    ("polonaise", "polonaise", ()),
    ("etude", "etude", ()),
    ("impromptu", "impromptu", ()),
    ("ballade", "ballade", ()),
    ("scherzo", "scherzo", ()),
    ("barcarolle", "barcarolle", ()),
    ("fantais", "fantasia", ()),
    ("fantasy", "fantasia", ()),
    ("rondo", "rondo", ()),
    ("march", "march", ("march",)),
    ("polka", "dance", ()),
    ("ecossaise", "dance", ()),
    ("meditation", "meditation", ("quiet",)),
    ("vltava", "tone_poem", ("orchestra",)),
    ("steppes", "tone_poem", ("orchestra",)),
    ("hebrides", "overture", ("orchestra",)),
]
CHAMBER_OR_SOLO = {"quartet", "quintet", "trio", "sonata", "prelude_and_fugue", "fugue", "mazurka",
                   "waltz", "etude", "nocturne", "impromptu", "ballade", "polonaise"}

# Signs that a piece was published after its composer died, so the death year
# doesn't prove it is public domain: "posth.", or a scholar's catalogue number
# (Chopin's B./KK., Beethoven's WoO) standing in for an opus number.
_POSTHUMOUS_RE = re.compile(r"\bposth\b|\b(?:B|KK|WoO)\.?\s*\d", re.IGNORECASE)
_OPUS_RE = re.compile(r"\bOp\.?\s*\d", re.IGNORECASE)


def infer_form(title: str) -> tuple[str | None, set[str]]:
    folded = fold(title)
    for needle, form, tags in FORMS:
        if needle in folded:
            return form, set(tags)
    return None, set()


def maybe_posthumous(title: str) -> bool:
    if _OPUS_RE.search(title) and "posth" not in title.lower():
        return False
    return bool(_POSTHUMOUS_RE.search(title))


_NUMERAL_RE = re.compile(r"^(?:\d{1,3}|[IVXLC]+\.?)$")


def parse_tagged_title(title: str) -> tuple[str | None, str, str | None]:
    """Split "Composer - Work - 01 - Movement" style title tags.

    Also handles "Composer - 01 - Work - Movement" and "Composer - Work".
    Returns (composer or None, work title, movement or None).
    """
    segments = [s.strip() for s in (title or "").split(" - ") if s.strip()]
    composer = None
    if len(segments) > 1 and lookup_composer(segments[0]):
        composer = segments.pop(0)
    numbered = [i for i, s in enumerate(segments) if _NUMERAL_RE.match(s)]
    if not numbered:
        return composer, ": ".join(segments), None
    i = numbered[0]
    if i == 0:  # "01 - Work - Movement"
        work = segments[1] if len(segments) > 1 else ""
        movement = " - ".join(segments[2:]) or None
    else:
        work = " - ".join(segments[:i])
        movement = " - ".join(segments[i + 1:]) or None
    return composer, work, movement


_MOVEMENT_RE = re.compile(r"^(?P<work>.+?)\s+-\s+(?P<mvt>(?:[IVX]+|\d{1,2})\.\s+.+)$")


def split_movement(stem: str) -> tuple[str, str | None]:
    match = _MOVEMENT_RE.match(stem)
    if not match:
        return stem, None
    return match.group("work").strip(), match.group("mvt").strip()


def _roman(text: str) -> int:
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
    total, prev = 0, 0
    for ch in reversed(text.upper()):
        v = values.get(ch, 0)
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    return total


def movement_order(movement: str | None) -> int:
    if not movement:
        return 0
    head = movement.split(".")[0].strip()
    if head.isdigit():
        return int(head)
    return _roman(head) or 0


def clean_title(title: str) -> str:
    title = title.replace(" _ ", " / ")
    title = re.sub(r"\bBWV\.\s*", "BWV ", title)
    return re.sub(r"\s+", " ", title).strip(" -")


def slugify(text: str, limit: int = 70) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", fold(text)).strip("-")
    return slug[:limit].rstrip("-") or "untitled"


# ── sources ──────────────────────────────────────────────────────────────────


@dataclass
class Source:
    name: str
    license: str
    credit: str
    license_evidence: str = ""
    description: str = ""
    archive_item: str | None = None
    grouping: str = "movements"
    title_from: str = "tag"
    verify: str | None = None
    composer: str | None = None
    performer: str | None = None
    year: int | None = None
    tags: tuple[str, ...] = ()
    work: dict[str, Any] = field(default_factory=dict)
    exclude: tuple[str, ...] = ()
    source_url: str | None = None

    @property
    def url(self) -> str | None:
        if self.source_url:
            return self.source_url
        return f"https://archive.org/details/{self.archive_item}" if self.archive_item else None


def load_sources(path: Path = SOURCES_PATH) -> dict[str, Source]:
    import yaml

    data = yaml.safe_load(path.read_text()) or {}
    sources = {}
    for name, raw in (data.get("sources") or {}).items():
        raw = dict(raw or {})
        if raw.get("license") not in ("cc0", "public_domain"):
            raise LibraryError(f"{name}: license must be cc0 or public_domain")
        sources[name] = Source(
            name=name,
            license=raw["license"],
            credit=str(raw.get("credit") or "{performer}"),
            license_evidence=" ".join(str(raw.get("license_evidence", "")).split()),
            description=str(raw.get("description", "")),
            archive_item=raw.get("archive_item"),
            grouping=str(raw.get("grouping", "movements")),
            title_from=str(raw.get("title_from", "tag")),
            verify=" ".join(str(raw["verify"]).split()) if raw.get("verify") else None,
            composer=raw.get("composer"),
            performer=raw.get("performer"),
            year=raw.get("year"),
            tags=tuple(raw.get("tags") or ()),
            work=dict(raw.get("work") or {}),
            exclude=tuple(raw.get("exclude") or ()),
        )
    return sources


# ── tracks and works ─────────────────────────────────────────────────────────


@dataclass
class Track:
    name: str  # path inside the item or folder, e.g. "Brahms_Symphony/01.flac"
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    composer: str | None = None
    number: int | None = None
    length: float | None = None
    size: int | None = None
    md5: str | None = None
    master: Path | None = None  # local path once downloaded / found

    @property
    def folder(self) -> str:
        return self.name.rsplit("/", 1)[0] if "/" in self.name else ""

    @property
    def stem(self) -> str:
        return Path(self.name).stem


@dataclass
class PlannedWork:
    source: Source
    title: str
    tracks: list[Track]
    parts: list[str | None]
    composer: str | None = None
    performer: str | None = None
    form: str | None = None
    tags: set[str] = field(default_factory=set)
    composition_year: int | None = None
    composition_basis: str = ""
    review: list[str] = field(default_factory=list)
    quarantine: list[str] = field(default_factory=list)  # reasons (VERIFY_*)

    @property
    def hours(self) -> float:
        return sum(t.length or 0 for t in self.tracks) / 3600

    @property
    def credit(self) -> str:
        return self.source.credit.format(performer=self.performer or "Unknown performer")

    @property
    def slug(self) -> str:
        digest = hashlib.sha1(self.tracks[0].name.encode()).hexdigest()[:6]
        return f"{slugify(self.title, 60)}-{digest}"


def _track_number(value: Any) -> int | None:
    match = re.match(r"\s*(\d+)", str(value or ""))
    return int(match.group(1)) if match else None


def tracks_from_archive(metadata: dict, source: Source) -> list[Track]:
    tracks = []
    for f in metadata.get("files") or []:
        name = str(f.get("name", ""))
        if f.get("source") != "original" or Path(name).suffix.lower() not in LOSSLESS_EXTENSIONS:
            continue
        if any(pattern.lower() in name.lower() for pattern in source.exclude):
            continue
        try:
            length = float(f.get("length")) if f.get("length") else None
        except ValueError:
            length = None
        tracks.append(Track(
            name=name, title=f.get("title"), artist=f.get("artist") or f.get("creator"),
            album=f.get("album"), composer=f.get("composer"), number=_track_number(f.get("track")),
            length=length, size=int(f["size"]) if f.get("size") else None, md5=f.get("md5"),
        ))
    return tracks


def tracks_from_folder(folder: Path) -> list[Track]:
    tracks = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in LOSSLESS_EXTENSIONS:
            continue
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration:format_tags:stream_tags",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(result.stdout or "{}") if result.returncode == 0 else {}
        fmt = data.get("format") or {}
        tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
        for stream in data.get("streams") or []:
            for k, v in (stream.get("tags") or {}).items():
                tags.setdefault(k.lower(), v)
        try:
            length = float(fmt.get("duration"))
        except (TypeError, ValueError):
            length = None
        tracks.append(Track(
            name=path.relative_to(folder).as_posix(), title=tags.get("title"),
            artist=tags.get("artist") or tags.get("album_artist") or tags.get("performer"),
            album=tags.get("album"), composer=tags.get("composer"),
            number=_track_number(tags.get("track")), length=length, size=path.stat().st_size,
            master=path,
        ))
    return tracks


def _order(tracks: list[Track]) -> list[Track]:
    return sorted(tracks, key=lambda t: (t.number if t.number is not None else 10**6, t.name))


def group_tracks(tracks: list[Track], source: Source) -> list[PlannedWork]:
    if not tracks:
        return []
    groups: list[tuple[str, list[Track], list[str | None]]] = []
    if source.grouping == "album":
        ordered = _order(tracks)
        title = source.work.get("title") or ordered[0].album or source.description
        groups.append((title, ordered, [t.title or t.stem for t in ordered]))
    elif source.grouping == "folder":
        by_folder: dict[str, list[Track]] = collections.OrderedDict()
        for t in tracks:
            by_folder.setdefault(t.folder, []).append(t)
        for folder, members in by_folder.items():
            ordered = _order(members)
            parts, title = [], None
            for t in ordered:
                _, work, movement = parse_tagged_title(t.title or t.stem)
                title = title or work
                parts.append(movement)
            groups.append((title or folder.replace("_", " "), ordered, parts))
    elif source.grouping == "catalog_number":
        by_number: dict[str, list[Track]] = collections.OrderedDict()
        for t in _order(tracks):
            match = cat._CATALOG_NO_RE.search(t.title or t.stem)
            key = match.group(0) if match else t.name
            by_number.setdefault(key, []).append(t)
        template = source.work.get("title_template")
        for key, members in by_number.items():
            first = members[0].title or members[0].stem
            title = first
            if template:
                number = re.search(r"No\.\s*(\d+)", first)
                key_sig = re.search(r"\bin ([A-G](?:[- ](?:sharp|flat))? (?:major|minor))", first, re.IGNORECASE)
                catalog_no = re.search(r"(\d+)", key)
                if number and key_sig and catalog_no:
                    title = template.format(number=number.group(1), key=key_sig.group(1),
                                            catalog=catalog_no.group(1))
            groups.append((title, members, [t.title or t.stem for t in members]))
    elif source.grouping == "movements":
        by_work: dict[str, list[tuple[Track, str | None]]] = collections.OrderedDict()
        for t in tracks:
            base = t.stem if source.title_from == "filename" else (t.title or t.stem)
            work, movement = split_movement(base)
            by_work.setdefault(work, []).append((t, movement))
        for work, members in by_work.items():
            members.sort(key=lambda m: (movement_order(m[1]), m[0].number or 0, m[0].name))
            groups.append((work, [m[0] for m in members], [m[1] for m in members]))
    else:
        raise LibraryError(f"{source.name}: unknown grouping {source.grouping!r}")

    works = []
    for title, members, parts in groups:
        works.append(describe(source, clean_title(title), members, parts))
    return works


def describe(source: Source, title: str, tracks: list[Track], parts: list[str | None],
             today: date | None = None) -> PlannedWork:
    """Fill in composer, performer, form, tags, the composition year and review notes."""
    today = today or date.today()
    work = PlannedWork(source=source, title=title, tracks=tracks, parts=parts)

    # Composer: the source's fixed value, a composer tag, or the title tag's first segment.
    named = source.composer
    if not named:
        named = next((t.composer for t in tracks if t.composer), None)
    if not named:
        named = next((c for c in (parse_tagged_title(t.title or "")[0] for t in tracks) if c), None)
    known = lookup_composer(named)
    work.composer = known[0] if known else named
    if not work.composer:
        work.review.append("no composer found: set it with catalog.py edit")

    artists = collections.Counter(t.artist for t in tracks if t.artist)
    work.performer = source.performer or (artists.most_common(1)[0][0] if artists else None)
    if not work.performer:
        work.review.append("no performer found: the credit line needs one")

    work.form, tags = infer_form(title)
    work.form = source.work.get("form", work.form)
    work.tags = tags | set(source.tags)
    if (work.form in CHAMBER_OR_SOLO and work.performer
            and "orchestra" in work.performer.lower()):
        work.review.append(
            f"performer tag says {work.performer!r} plays a {work.form}; check the credit"
        )

    cutoff = cat.composition_pd_cutoff(today)
    if source.work.get("composition_year"):
        work.composition_year = int(source.work["composition_year"])
        work.composition_basis = "published or composed by " + str(work.composition_year)
    elif known and known[1] <= cutoff:
        work.composition_year = known[1]
        work.composition_basis = f"bounded by the composer's death in {known[1]}"
        if maybe_posthumous(title) or any(
            maybe_posthumous(t.name) or maybe_posthumous(t.title or "") for t in tracks
        ):
            work.quarantine.append(VERIFY_PUBLICATION)
            work.review.append(
                "possibly published after the composer died: confirm first publication before "
                f"{cutoff + 1}, then approve"
            )
    elif known:
        work.review.append(
            f"{known[0]} died in {known[1]}: set the publication year with "
            "catalog.py edit ID --composition-year YYYY"
        )
    if source.verify:
        work.quarantine.append(VERIFY_LICENSE)
    return work


# ── downloading ──────────────────────────────────────────────────────────────


def fetch_json(url: str, timeout: float = 60) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def archive_metadata(item: str, base: str = ARCHIVE_BASE) -> dict:
    data = fetch_json(f"{base}/metadata/{urllib.parse.quote(item)}")
    if not data or not data.get("files"):
        raise LibraryError(f"Internet Archive item {item!r} not found or empty")
    return data


def _md5_of(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, dest: Path, *, size: int | None = None, md5: str | None = None,
             retries: int = 3, log: Callable[[str], None] = print) -> bool:
    """Fetch url into dest, resuming a .part file. Returns False if already there."""
    if dest.exists() and (size is None or dest.stat().st_size == size):
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    for attempt in range(retries):
        offset = part.stat().st_size if part.exists() else 0
        if size is not None and offset > size:
            part.unlink()
            offset = 0
        headers = {"User-Agent": USER_AGENT}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=120) as response:
                mode = "ab" if offset and response.status == 206 else "wb"
                with part.open(mode) as handle:
                    shutil.copyfileobj(response, handle, 1 << 20)
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and size is not None and offset == size:
                pass  # already complete
            else:
                log(f"    download error ({exc.code}), attempt {attempt + 1}/{retries}")
                time.sleep(2 ** attempt)
                continue
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            log(f"    download error ({exc}), attempt {attempt + 1}/{retries}")
            time.sleep(2 ** attempt)
            continue
        if size is not None and part.stat().st_size != size:
            log(f"    incomplete ({part.stat().st_size} of {size} bytes), resuming")
            continue
        if md5 and _md5_of(part) != md5:
            log("    checksum mismatch, downloading again")
            part.unlink()
            continue
        part.replace(dest)
        return True
    raise LibraryError(f"Could not download {url}")


# ── air copies ───────────────────────────────────────────────────────────────


def make_air_copy(master: Path, dest: Path) -> Path:
    """16-bit/44.1 kHz stereo FLAC. A master that already is one is copied
    byte for byte (the lossless gate needs that); anything else is resampled
    and dithered once."""
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    info = cat.probe(master)
    tmp = dest.with_name(dest.name + ".tmp.flac")
    if info.codec == "flac" and info.sample_rate == AIR_RATE and info.bit_depth == 16 and info.channels == 2:
        shutil.copyfile(master, tmp)
    else:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error", "-i", str(master), "-map", "0:a:0", "-map_metadata", "0",
                "-af", f"aresample=osr={AIR_RATE}:osf=s16:resampler=soxr:precision=28:cutoff=0.97"
                       ":dither_method=triangular",
                "-ac", "2", "-c:a", "flac", "-sample_fmt", "s16", "-compression_level", "8", str(tmp),
            ],
            capture_output=True, text=True, timeout=3600,
        )
        if result.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise LibraryError(f"Conversion failed for {master.name}: {result.stderr.strip()[-300:]}")
    tmp.replace(dest)
    return dest


# ── intake ───────────────────────────────────────────────────────────────────


@dataclass
class Result:
    work: PlannedWork
    work_id: int | None = None
    status: str = ""
    skipped: str | None = None
    rejected: list[str] = field(default_factory=list)
    unairable: list[str] = field(default_factory=list)


def ingest(catalog: Catalog, work: PlannedWork, air_root: Path, *, jobs: int = 2,
           spectral: bool = True, log: Callable[[str], None] = print) -> Result:
    result = Result(work)
    air_dir = air_root / work.source.name / work.slug
    air_paths = [
        air_dir / f"{i:02d}-{slugify(part or t.stem, 50)}.flac"
        for i, (t, part) in enumerate(zip(work.tracks, work.parts), start=1)
    ]
    if all(catalog.has_path(p) for p in air_paths):
        result.skipped = "already in the catalog"
        return result

    cutoffs: list[float | None] = []
    for t in work.tracks:
        if spectral:
            check = cat.spectral_check(t.master)
            cutoffs.append(check.cutoff_hz)
            if check.verdict == "reject":
                result.rejected.append(
                    f"{t.name}: hard cutoff at {check.cutoff_hz:.0f} Hz, likely made from a lossy file"
                )
            elif check.verdict == "suspect" and VERIFY_AUDIO not in work.quarantine:
                work.quarantine.append(VERIFY_AUDIO)
                work.review.append(f"{t.name}: steep cutoff at {check.cutoff_hz:.0f} Hz; listen, then approve")
        else:
            cutoffs.append(None)
    if result.rejected:
        return result

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        list(pool.map(make_air_copy, [t.master for t in work.tracks], air_paths))

    files = []
    for t, part, air, cutoff in zip(work.tracks, work.parts, air_paths, cutoffs):
        info = cat.probe(air)
        files.append({
            "path": str(air), "master_path": str(t.master), "part_title": part,
            "codec": info.codec, "sample_rate": info.sample_rate, "bit_depth": info.bit_depth,
            "channels": info.channels, "duration_sec": info.duration_sec, "lossless": info.lossless,
            "sha256": cat.sha256_file(air), "hf_cutoff_hz": cutoff,
        })
    lufs, peak = cat.measure_loudness(air_paths)
    notes = [f"[{reason}]" for reason in work.quarantine] + work.review
    if work.composition_basis:
        notes.insert(0, f"composition year {work.composition_year}: {work.composition_basis}")
    work_id = catalog.add_work(
        kind="classical", title=work.title, composer=work.composer, performer=work.performer,
        form=work.form, year=work.source.year, composition_year=work.composition_year,
        source=work.source.name, source_url=work.source.url, license=work.source.license,
        license_evidence=work.source.license_evidence or None, credit_line=work.credit,
        tags=sorted(work.tags), gain_db=cat.fixed_gain_db(lufs, peak),
        loudness_lufs=lufs if lufs != float("-inf") else None,
        true_peak_dbtp=peak if peak != float("-inf") else None,
        status="quarantined" if work.quarantine else "active",
        notes="\n".join(notes) or None, files=files,
    )
    result.work_id = work_id
    result.status = "quarantined" if work.quarantine else "active"
    result.unairable = catalog.airability(catalog.work(work_id))
    return result


def write_report(library: Path, source: Source, results: Sequence[Result]) -> Path:
    import yaml

    path = library / "reports" / f"{source.name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for r in results:
        w = r.work
        rows.append({
            "work_id": r.work_id, "title": w.title, "composer": w.composer, "performer": w.performer,
            "credit": w.credit, "form": w.form, "tags": sorted(w.tags), "minutes": round(w.hours * 60, 1),
            "composition_year": w.composition_year, "composition_basis": w.composition_basis or None,
            "status": r.status or ("rejected" if r.rejected else None), "skipped": r.skipped,
            "quarantine": w.quarantine or None, "review": w.review or None,
            "rejected": r.rejected or None, "not_airable": r.unairable or None,
            "files": [t.name for t in w.tracks],
        })
    payload = {"source": source.name, "license": source.license,
               "license_evidence": source.license_evidence, "works": rows}
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=100))
    return path


# ── commands ─────────────────────────────────────────────────────────────────


def _gb(n: float) -> str:
    return f"{n / 1e9:.2f} GB"


def plan_archive_source(source: Source, base: str = ARCHIVE_BASE) -> tuple[list[Track], list[PlannedWork]]:
    metadata = archive_metadata(source.archive_item, base)
    tracks = tracks_from_archive(metadata, source)
    if not tracks:
        raise LibraryError(f"{source.name}: no original lossless files in {source.archive_item}")
    return tracks, group_tracks(tracks, source)


def print_plan(source: Source, tracks: list[Track], works: list[PlannedWork], log=print) -> None:
    hours = sum(w.hours for w in works)
    size = sum(t.size or 0 for t in tracks)
    review = [w for w in works if w.review or w.quarantine]
    log(f"{source.name}: {len(tracks)} files, {_gb(size)} to download, {hours:.1f} h in {len(works)} works"
        f" ({len(review)} need review)")
    for w in works[:8]:
        flag = " [quarantined]" if w.quarantine else ""
        log(f"  {w.hours * 60:6.1f} min  {w.composer or '?'}: {w.title} | {w.credit} | "
            f"{w.form or '-'} | year {w.composition_year or '?'}{flag}")
    if len(works) > 8:
        log(f"  ... and {len(works) - 8} more")
    for w in review[:6]:
        log(f"  review: {w.title}: {'; '.join(w.review) or ', '.join(w.quarantine)}")


def fetch_source(catalog: Catalog, source: Source, library: Path, *, base: str = ARCHIVE_BASE,
                 dry_run: bool = False, jobs: int = 2, spectral: bool = True,
                 log: Callable[[str], None] = print) -> list[Result]:
    tracks, works = plan_archive_source(source, base)
    print_plan(source, tracks, works, log)
    if dry_run:
        return []
    needed = sum(t.size or 0 for t in tracks) + sum(w.hours for w in works) * AIR_BYTES_PER_HOUR
    library.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(library).free
    if needed > free:
        raise LibraryError(f"Needs about {_gb(needed)} but {library} has {_gb(free)} free")
    masters = library / "masters" / source.name
    for i, t in enumerate(tracks, start=1):
        t.master = masters / t.name
        url = f"{base}/download/{urllib.parse.quote(source.archive_item)}/{urllib.parse.quote(t.name)}"
        if download(url, t.master, size=t.size, md5=t.md5, log=log):
            log(f"  [{i}/{len(tracks)}] downloaded {t.name}")
    return ingest_all(catalog, source, works, library, jobs=jobs, spectral=spectral, log=log)


def ingest_all(catalog: Catalog, source: Source, works: list[PlannedWork], library: Path, *,
               jobs: int, spectral: bool, log: Callable[[str], None]) -> list[Result]:
    results = []
    for i, work in enumerate(works, start=1):
        result = ingest(catalog, work, library / "air", jobs=jobs, spectral=spectral, log=log)
        results.append(result)
        state = result.skipped or (f"REJECTED: {result.rejected[0]}" if result.rejected else result.status)
        log(f"  [{i}/{len(works)}] {work.title}: {state}")
    report = write_report(library, source, results)
    added = [r for r in results if r.work_id]
    log(f"{source.name}: added {len(added)} works, {sum(r.work.hours for r in added):.1f} h; report {report}")
    return results


def approve(catalog: Catalog, source: str, reasons: Iterable[str]) -> int:
    """Reactivate a source's quarantined works whose only doubts were reviewed."""
    wanted = set(reasons)
    count = 0
    for work in catalog.works(status="quarantined"):
        if work.source != source:
            continue
        markers = set(re.findall(r"\[(verify-[a-z]+)\]", work.notes or ""))
        if markers and markers <= wanted:
            note = (work.notes or "") + f"\napproved after review ({', '.join(sorted(markers))}) on {date.today()}"
            catalog.update_work(work.id, notes=note)
            catalog.set_status(work.id, "active")
            count += 1
    return count


def main(argv: Sequence[str] | None = None) -> int:
    from station_config import load_station_config

    parser = argparse.ArgumentParser(description="Pull cleared lossless classical into WIT-FM")
    parser.add_argument("--station", default="wit-next")
    parser.add_argument("--db", type=Path, help="catalog database (default: the station's)")
    parser.add_argument("--library", type=Path, help="library folder (default: the station's)")
    parser.add_argument("--sources", type=Path, default=SOURCES_PATH)
    parser.add_argument("--archive-base", default=ARCHIVE_BASE, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("sources", help="list the configured sources")

    p_fetch = sub.add_parser("fetch", help="download and add Internet Archive sources")
    p_fetch.add_argument("names", nargs="+", help="source names, or 'all'")
    p_fetch.add_argument("--dry-run", action="store_true", help="show the works, download nothing")
    p_fetch.add_argument("--jobs", type=int, default=2, help="parallel conversions")
    p_fetch.add_argument("--no-spectral", action="store_true")

    p_local = sub.add_parser("local", help="add a folder of lossless files (Musopen downloads, CD rips)")
    p_local.add_argument("folder", type=Path)
    p_local.add_argument("--source", required=True, help="a name for this source, e.g. musopen-site")
    p_local.add_argument("--license", required=True, choices=("cc0", "public_domain"))
    p_local.add_argument("--credit", required=True, help='e.g. "{performer} via Musopen"')
    p_local.add_argument("--license-evidence", default="")
    p_local.add_argument("--source-url")
    p_local.add_argument("--grouping", default="movements", choices=("album", "folder", "catalog_number", "movements"))
    p_local.add_argument("--title-from", default="tag", choices=("tag", "filename"))
    p_local.add_argument("--composer")
    p_local.add_argument("--performer")
    p_local.add_argument("--year", type=int)
    p_local.add_argument("--tag", action="append", default=[])
    p_local.add_argument("--verify", help="arrive quarantined with this note until approved")
    p_local.add_argument("--dry-run", action="store_true")
    p_local.add_argument("--jobs", type=int, default=2)
    p_local.add_argument("--no-spectral", action="store_true")

    p_approve = sub.add_parser("approve", help="put a reviewed source's quarantined works on air")
    p_approve.add_argument("source")
    p_approve.add_argument("--reason", action="append",
                           choices=("license", "publication", "audio", "all"),
                           help="which doubts you checked (default: license)")

    sub.add_parser("report", help="hours per source and what is unlocked")

    args = parser.parse_args(argv)
    station = load_station_config(args.station)
    library = (args.library or station.library_dir).expanduser()
    sources = load_sources(args.sources)

    if args.cmd == "sources":
        for name, s in sources.items():
            where = s.archive_item or "local"
            flag = " (arrives quarantined: verify)" if s.verify else ""
            print(f"{name:24s} {s.license:14s} {where:60s} {s.description}{flag}")
        return 0

    with Catalog(args.db or station.catalog_db_path) as catalog:
        if args.cmd == "fetch":
            names = list(sources) if args.names == ["all"] else args.names
            unknown = [n for n in names if n not in sources]
            if unknown:
                parser.error(f"unknown source(s): {', '.join(unknown)}; see `sources`")
            failed = 0
            for name in names:
                try:
                    fetch_source(catalog, sources[name], library, base=args.archive_base,
                                 dry_run=args.dry_run, jobs=args.jobs, spectral=not args.no_spectral)
                except (LibraryError, urllib.error.URLError, cat.CatalogError) as exc:
                    failed += 1
                    print(f"{name}: FAILED: {exc}")
            if not args.dry_run:
                _print_totals(catalog)
            return 1 if failed else 0

        if args.cmd == "local":
            source = Source(
                name=args.source, license=args.license, credit=args.credit,
                license_evidence=args.license_evidence, grouping=args.grouping,
                title_from=args.title_from, verify=args.verify, composer=args.composer,
                performer=args.performer, year=args.year, tags=tuple(args.tag),
                source_url=args.source_url, description=f"local folder {args.folder}",
            )
            tracks = tracks_from_folder(args.folder.expanduser())
            if not tracks:
                parser.error(f"no lossless files (FLAC, WAV, AIFF) under {args.folder}")
            works = group_tracks(tracks, source)
            print_plan(source, tracks, works)
            if args.dry_run:
                return 0
            ingest_all(catalog, source, works, library, jobs=args.jobs,
                       spectral=not args.no_spectral, log=print)
            _print_totals(catalog)
            return 0

        if args.cmd == "approve":
            chosen = args.reason or ["license"]
            if "all" in chosen:
                chosen = ["license", "publication", "audio"]
            count = approve(catalog, args.source, [f"verify-{r}" for r in chosen])
            print(f"approved {count} work(s) from {args.source}")
            _print_totals(catalog)
            return 0

        if args.cmd == "report":
            by_source: dict[str, list[float]] = collections.defaultdict(lambda: [0.0, 0.0, 0])
            for work in catalog.works(kind="classical", status=None):
                row = by_source[work.source]
                row[2] += 1
                row[0] += work.duration_sec / 3600
                if catalog.is_airable(work):
                    row[1] += work.duration_sec / 3600
            for name, (total, cleared, count) in sorted(by_source.items()):
                print(f"{name:24s} {int(count):4d} works {total:6.1f} h  on air {cleared:6.1f} h")
            _print_totals(catalog)
            return 0
    return 0


def _print_totals(catalog: Catalog) -> None:
    hours = catalog.cleared_hours("classical")
    print(f"\nCleared lossless classical on air: {hours:.1f} h")
    for need, blocks in ((100, "Nocturne, Evening Concert"), (200, "Day Shift"), (300, "Matinee, Concert Hall")):
        state = "unlocked" if hours >= need else f"{need - hours:.1f} h to go"
        print(f"  {need:3d} h  {blocks:28s} {state}")


if __name__ == "__main__":
    raise SystemExit(main())
