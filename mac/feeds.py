"""Shared pieces of the data feeds (show bible: "Every fact comes from a feed").

A feed builds a FactSheet: the lines a script may draw facts from, the
structured data behind them, and the providers they came from. Each provider
is marked cleared_for_air in config/feeds.yaml only once its terms allow
on-air use; a fact sheet with any uncleared source can be rehearsed but must
not reach the air (the producers enforce that for the shows that fail
closed).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FEEDS_PATH = PROJECT_ROOT / "config" / "feeds.yaml"
USER_AGENT = "data-pull/1.0"


class FeedError(RuntimeError):
    pass


@dataclass
class Source:
    name: str
    cleared: bool
    credit: str = ""
    note: str = ""


@dataclass
class FactSheet:
    name: str
    fetched_at: str
    sections: dict[str, list[str]] = field(default_factory=dict)  # heading -> fact lines
    sources: dict[str, Source] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def add(self, heading: str, line: str, source: Source) -> None:
        self.sections.setdefault(heading, []).append(line)
        self.sources[source.name] = source

    @property
    def cleared(self) -> bool:
        return bool(self.sources) and all(s.cleared for s in self.sources.values())

    def uncleared(self) -> list[str]:
        return sorted(name for name, s in self.sources.items() if not s.cleared)

    def text(self, *headings: str) -> str:
        chosen = headings or tuple(self.sections)
        blocks = [f"{h}:\n" + "\n".join(f"- {line}" for line in self.sections[h]) for h in chosen if self.sections.get(h)]
        return "\n\n".join(blocks)

    def credits(self) -> list[str]:
        return sorted({s.credit for s in self.sources.values() if s.credit})

    def to_json(self) -> dict:
        return {"name": self.name, "fetched_at": self.fetched_at, "sections": self.sections,
                "sources": {k: asdict(v) for k, v in self.sources.items()}, "data": self.data,
                "warnings": self.warnings}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "FactSheet":
        return cls(name=data["name"], fetched_at=data["fetched_at"], sections=dict(data.get("sections") or {}),
                   sources={k: Source(**v) for k, v in (data.get("sources") or {}).items()},
                   data=dict(data.get("data") or {}), warnings=list(data.get("warnings") or []))

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(self.to_json(), indent=1, ensure_ascii=False))
        tmp.replace(path)
        return path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_feeds_config(path: Path = FEEDS_PATH) -> dict[str, Any]:
    import yaml

    return yaml.safe_load(Path(path).read_text()) or {}


def source_from(config: Mapping[str, Any], name: str) -> Source:
    raw = dict(config.get(name) or {})
    return Source(name=name, cleared=bool(raw.get("cleared_for_air", False)), credit=str(raw.get("credit", "")),
                  note=" ".join(str(raw.get("note", "")).split()))


def get(url: str, *, params: Mapping[str, Any] | None = None, headers: Mapping[str, str] | None = None,
        data: bytes | None = None, timeout: float = 30, retries: int = 2) -> bytes:
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    last = ""
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})},
                                         method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read()[:300].decode(errors="replace")
            last = f"HTTP {exc.code}: {body}"
            if exc.code < 500 and exc.code != 429:
                break
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last = str(getattr(exc, "reason", exc))
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    host = urllib.parse.urlparse(url).netloc
    raise FeedError(f"{host}: {last}")


def get_json(url: str, **kwargs: Any) -> Any:
    raw = get(url, **kwargs)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FeedError(f"{urllib.parse.urlparse(url).netloc} sent something that isn't JSON") from exc
