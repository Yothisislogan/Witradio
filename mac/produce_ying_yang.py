"""Ying & Yang: the weekday sports debate, and Saturday's Receipts.

Topics come from the sports fact sheet: the model picks three debate-worthy
items from the results and headlines (sensitive stories were already held
back), and each segment sees only its topic's facts, so nothing outside the
feed gets said. Every segment fails closed.

The Receipts system (show bible): the Handshake logs one prediction about a
game in the feed; once that game is final, the prediction is graded from the
score and the Receipts segment reads the verdict.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence

from producers import Airing, Producer, Result, SlotWriter
from script_writer import Segment

MANNERS = ("Rage plus manners: they interrupt each other to say \"please, go on.\" Takes target play, coaching "
           "and front-office decisions only. No betting or odds talk.")


@dataclass(frozen=True)
class Part:
    order: int
    name: str
    title: str
    seconds: float
    source: str  # topic0 | topic1 | topic2 | results | receipts | upcoming
    brief: str
    allow_numbers: tuple[str, ...] = ()


HOUR = [
    Part(0, "cold_open", "Cold Open", 60, "topic0", "The day's hottest take in 60 seconds. Ying fires; Yang breathes."),
    Part(1, "overreaction_meter", "Overreaction Meter", 480, "topic0",
         "Ying rates the panic about this story from 1 to 10. Yang rates the same story 1 to 10 the other way. "
         "They argue it out. " + MANNERS, tuple(str(n) for n in range(0, 11))),
    Part(2, "hot_seat", "Hot Seat", 540, "topic1", "Full debate on this topic. " + MANNERS),
    Part(3, "switch_sides", "Switch Sides", 420, "topic1",
         "Each argues the other's position on this topic for about a minute; whoever breaks first loses."),
    Part(4, "scores_check", "Scores Check", 240, "results",
         "A straight read of the real results in FACTS, taking turns. No bits over facts."),
    Part(5, "topic_three", "Topic Three", 480, "topic2", "Debate this one. " + MANNERS),
    Part(6, "receipts", "Receipts", 300, "receipts",
         "Read a past prediction from FACTS and its verdict. The host who made it owns it, gracefully or not."),
    Part(7, "handshake", "The Handshake", 240, "upcoming",
         "Each says one nice thing about the other's take. Then one of them makes tomorrow's bold prediction about "
         "one game in FACTS. Close: \"We agree to disagree.\" \"And I disagree that we agree.\""),
]
RECEIPTS_HOUR = [
    Part(0, "audit_open", "Receipts", 60, "receipts", "Open the weekly audit: every prediction graded today."),
    Part(1, "audit", "The Audit", 1500, "receipts",
         "Go through every graded prediction in FACTS. Each host defends his record with total confidence."),
    Part(2, "scores_check", "Scores Check", 300, "results", "A straight read of the real results in FACTS."),
    Part(3, "handshake", "The Handshake", 240, "upcoming", HOUR[-1].brief),
]
PREDICTION_FIELD = {"prediction": ('{"host": "ying" or "yang", "claim": "the prediction in one sentence", '
                                   '"team": "the team predicted to win, named as in FACTS", '
                                   '"opponent": "the other team, named as in FACTS"}')}


class Predictions:
    """The Receipts table: date, host, claim, the game it resolves on, and the verdict."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY, made_on TEXT, host TEXT, claim TEXT, team TEXT, opponent TEXT,
            game_date TEXT, status TEXT DEFAULT 'open', result TEXT, aired INTEGER DEFAULT 0)""")
        self.db.commit()

    def add(self, made_on: date, host: str, claim: str, team: str, opponent: str, game_date: date) -> int:
        cur = self.db.execute("INSERT INTO predictions (made_on, host, claim, team, opponent, game_date) "
                              "VALUES (?, ?, ?, ?, ?, ?)",
                              (made_on.isoformat(), host, claim, team, opponent, game_date.isoformat()))
        self.db.commit()
        return int(cur.lastrowid)

    def grade(self, games: Sequence[dict]) -> int:
        graded = 0
        for row in self.db.execute("SELECT * FROM predictions WHERE status = 'open'").fetchall():
            for game in games:
                if game.get("state") != "post":
                    continue
                names = {game["home"]["name"], game["away"]["name"]}
                if row["team"] in names and row["opponent"] in names:
                    winner = game["home"] if (game["home"]["score"] or 0) > (game["away"]["score"] or 0) else game["away"]
                    loser = game["away"] if winner is game["home"] else game["home"]
                    status = "right" if winner["name"] == row["team"] else "wrong"
                    result = f"{winner['name']} {winner['score']}, {loser['name']} {loser['score']}"
                    self.db.execute("UPDATE predictions SET status = ?, result = ? WHERE id = ?", (status, result, row["id"]))
                    graded += 1
                    break
        self.db.commit()
        return graded

    def to_air(self, limit: int = 3) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM predictions WHERE status IN ('right', 'wrong') AND aired = 0 "
                               "ORDER BY game_date LIMIT ?", (limit,)).fetchall()

    def mark_aired(self, ids: Sequence[int]) -> None:
        self.db.executemany("UPDATE predictions SET aired = 1 WHERE id = ?", [(i,) for i in ids])
        self.db.commit()


def _say(day: str) -> str:
    when = date.fromisoformat(day)
    return f"{when:%A %B} {when.day}"


class YingYangProducer(Producer):
    program = "ying_yang"
    lead_hours = 2.0
    fail_closed = True

    def facts(self, airing: Airing):
        build: Callable[..., Any] | None = self.ctx.services.get("sports_facts")
        if build is None:
            import sports_feed

            provider, source, leagues, news_limit = sports_feed.for_config(self.ctx.feeds_config)
            return sports_feed.build_facts(provider, leagues, source, today=airing.start.date(), news_limit=news_limit)
        return build(airing.start.date())

    def predictions(self) -> Predictions:
        return Predictions(self.ctx.state_dir / "predictions.db")

    def pick_topics(self, sheet) -> list[list[str]]:
        candidates = [line for heading, lines in sheet.sections.items()
                      if heading.startswith(("RESULTS", "HEADLINES")) for line in lines]
        if not candidates:
            return []
        numbered = "\n".join(f"{i + 1}. {line}" for i, line in enumerate(candidates))
        prompt = ("Pick the three most debate-worthy topics for a sports debate show from these facts. Topics must "
                  "be about play, coaching or front-office decisions: no injuries, legal matters or personal lives. "
                  "Reply with JSON only: {\"topics\": [{\"title\": \"...\", \"facts\": [numbers of the facts it "
                  "rests on]}]}\n\n" + numbered)
        try:
            reply = self.ctx.writer.llm.chat([{"role": "user", "content": prompt}], json_mode=True, temperature=0.3)
            data = json.loads(reply[reply.find("{"): reply.rfind("}") + 1])
            topics = []
            for topic in data.get("topics", [])[:3]:
                lines = [candidates[int(n) - 1] for n in topic.get("facts", []) if 0 < int(n) <= len(candidates)]
                if lines:
                    topics.append(lines)
            if len(topics) == 3:
                return topics
        except Exception:  # any bad reply falls back to the plain pick
            pass
        headlines = [l for h, ls in sheet.sections.items() if h.startswith("HEADLINES") for l in ls]
        pool = headlines + [l for h, ls in sheet.sections.items() if h.startswith("RESULTS") for l in ls]
        return [[line] for line in pool[:3]]

    def section_text(self, sheet, prefix: str) -> str:
        return "\n".join(f"- {line}" for heading, lines in sheet.sections.items() if heading.startswith(prefix)
                         for line in lines)

    def produce(self, airing: Airing, now: datetime | None = None) -> Result:
        result = Result(show_id=airing.show_id, slot=airing.slot, status="done")
        parts = RECEIPTS_HOUR if airing.params.get("format") == "receipts" else HOUR
        todo = [p for p in parts if not any(SlotWriter(root, airing.show_id, airing.slot).has(p.name)
                                            for root in (self.ctx.buffer_dir, self.ctx.rehearsal_dir))]
        if not todo:
            return result
        sheet = self.facts(airing)
        store = self.predictions()
        store.grade(sheet.data.get("games") or [])
        topics = self.pick_topics(sheet)
        receipts = store.to_air(8 if parts is RECEIPTS_HOUR else 2)
        root = self.root_for(sheet.cleared)
        credit = "; ".join(sheet.credits())
        aired_receipts: list[int] = []
        for part in todo:
            if part.source.startswith("topic"):
                index = int(part.source[-1])
                facts = "\n".join(f"- {line}" for line in topics[index]) if index < len(topics) else ""
            elif part.source == "results":
                facts = self.section_text(sheet, "RESULTS")
            elif part.source == "upcoming":
                facts = self.section_text(sheet, "COMING UP")
            else:
                facts = "\n".join(f"- On {_say(r['made_on'])}, {r['host'].title()} predicted: \"{r['claim']}\" "
                                  f"Final: {r['result']}. {r['host'].title()} was {r['status']}." for r in receipts)
            if not facts.strip():
                result.problems.append(f"{part.name}: nothing in the feed for it")
                continue
            segment = Segment(id=f"ying_yang/{part.name}", brief=part.brief, speakers=["ying", "yang"],
                              seconds=part.seconds, facts=facts, allow_numbers=part.allow_numbers,
                              fields=PREDICTION_FIELD if part.name == "handshake" else {})
            out = self.ctx.work_dir / airing.show_id / airing.slot / f"{part.name}.flac"
            out.parent.mkdir(parents=True, exist_ok=True)
            written, path, problems = self.talk(segment, out)
            result.problems += problems
            if path is None:
                continue
            if part.name == "handshake" and written is not None:
                result.problems += self.log_prediction(written.script.extra.get("prediction"), sheet, airing)
            if part.source == "receipts":
                aired_receipts += [r["id"] for r in receipts]
            SlotWriter(root, airing.show_id, airing.slot).add(part.order, part.name, path, {
                "title": f"Ying & Yang: {part.title}", "artist": "Ying & Yang", "credit": credit,
                "kind": "talk", "show": airing.title})
            result.segments.append(part.name)
        if aired_receipts and root == self.ctx.buffer_dir:
            store.mark_aired(sorted(set(aired_receipts)))
        if root == self.ctx.rehearsal_dir and result.segments:
            result.status = "rehearsed"
        elif result.problems:
            result.status = "partial" if result.segments else "failed"
        result.folder = root / airing.show_id / airing.slot
        return result

    def log_prediction(self, prediction: Any, sheet, airing: Airing) -> list[str]:
        if not isinstance(prediction, dict):
            return ["handshake: no prediction to log"]
        host = str(prediction.get("host", "")).lower()
        team, opponent = str(prediction.get("team", "")), str(prediction.get("opponent", ""))
        for game in sheet.data.get("games") or []:
            names = {game["home"]["name"], game["away"]["name"]}
            if game.get("state") == "pre" and team in names and opponent in names and host in ("ying", "yang"):
                when = datetime.fromisoformat(game["start"].replace("Z", "+00:00")).date()
                self.predictions().add(airing.start.date(), host, str(prediction.get("claim", ""))[:300], team,
                                       opponent, when)
                return []
        return [f"handshake: the prediction ({team} over {opponent}) isn't a game in the feed; not logged"]
