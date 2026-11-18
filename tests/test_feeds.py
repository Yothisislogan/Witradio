"""Sports and market fact sheets, against fake ESPN, FRED, Treasury, BLS and Fed servers."""

import http.server
import json
import threading
import unittest
import urllib.parse
from datetime import date

from mac import fact_check as fc
from mac import market_feed as mf
from mac import sports_feed as sf

TODAY = date(2026, 11, 18)


def competitor(home_away, name, short, score, record, winner, leader=None):
    data = {"homeAway": home_away, "score": str(score) if score is not None else "", "winner": winner,
            "team": {"displayName": name, "shortDisplayName": short}, "records": [{"name": "overall", "summary": record}]}
    if leader:
        data["leaders"] = [{"name": "points", "displayName": "Points",
                            "leaders": [{"displayValue": leader[1], "athlete": {"displayName": leader[0]}}]}]
    return data


def event(event_id, when, state, detail, home, away, venue="Arena", recap=""):
    return {"id": event_id, "date": when, "status": {"type": {"state": state, "shortDetail": detail}},
            "competitions": [{"competitors": [home, away], "venue": {"fullName": venue},
                              "headlines": [{"shortLinkText": recap}] if recap else []}]}


SCOREBOARDS = {
    ("basketball/nba", "20261117"): [event("1", "2026-11-18T00:30Z", "post", "Final",
                                           competitor("home", "Toronto Raptors", "Raptors", 102, "5-9", False),
                                           competitor("away", "New York Knicks", "Knicks", 111, "8-6", True,
                                                      ("Jalen Brunson", "31")),
                                           recap="Brunson scores 31 as Knicks win")],
    ("basketball/nba", "20261118"): [event("2", "2026-11-19T00:00Z", "pre", "7:00 PM EST",
                                           competitor("home", "Cleveland Cavaliers", "Cavaliers", None, "8-7", False),
                                           competitor("away", "Memphis Grizzlies", "Grizzlies", None, "10-4", False),
                                           venue="Rocket Arena")],
}
NEWS = {"basketball/nba": [
    {"headline": "Knicks extend streak over Raptors", "description": "New York has won 15 straight in the series."},
    {"headline": "Star guard arrested after incident", "description": "Police said..."},
]}


class FakeData(http.server.ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), FakeDataHandler)
        self.fred = {
            "SP500": [("2026-11-17", "6712.34"), ("2026-11-16", "6795.91")],
            "DCOILWTICO": [("2026-11-17", "72.15"), ("2026-11-16", "73.40")],
            "CPIAUCSL": [("2026-10-01", "330.0")] + [(f"2026-{m:02d}-01", "325.0") for m in range(9, 0, -1)]
                        + [("2025-12-01", "321.0"), ("2025-11-01", "320.5"), ("2025-10-01", "320.0")],
            "PAYEMS": [("2026-10-01", "160152"), ("2026-09-01", "160000")],
        }
        self.bls_status = "REQUEST_SUCCEEDED"
        self.requests: list[str] = []

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server_address[1]}"


class FakeDataHandler(http.server.BaseHTTPRequestHandler):
    def reply(self, body, kind="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        self.server.requests.append(url.path)
        if url.path.startswith("/espn/"):
            path = url.path[len("/espn/"):].rsplit("/", 1)[0]
            if url.path.endswith("/scoreboard"):
                return self.reply({"events": SCOREBOARDS.get((path, query.get("dates")), [])})
            return self.reply({"articles": NEWS.get(path, [])})
        if url.path == "/fred/series/observations":
            rows = self.server.fred.get(query["series_id"])
            if rows is None:
                return self.reply({"error_code": 400, "error_message": "Bad Request. The series does not exist."})
            return self.reply({"observations": [{"date": d, "value": v} for d, v in rows]})
        if url.path == "/fred/releases/dates":
            return self.reply({"release_dates": [{"date": "2026-11-19", "release_name": "Housing Starts"},
                                                 {"date": "2026-11-19", "release_name": "Jobless Claims"},
                                                 {"date": "2026-11-25", "release_name": "GDP"}]})
        if url.path.startswith("/treasury/"):
            return self.reply(b'Date,"2 Yr","10 Yr","30 Yr"\n11/18/2026,4.80,5.07,5.41\n11/17/2026,4.80,5.10,5.46\n',
                              "text/csv")
        if url.path == "/fed.xml":
            return self.reply(b"<rss><channel><item><title>Minutes of the Federal Open Market Committee</title>"
                              b"<pubDate>Wed, 18 Nov 2026 19:00:00 GMT</pubDate><category>Monetary Policy</category>"
                              b"</item><item><title>Old news</title><pubDate>Mon, 02 Nov 2026 19:00:00 GMT</pubDate>"
                              b"</item></channel></rss>", "application/rss+xml")
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        if self.server.bls_status != "REQUEST_SUCCEEDED":
            return self.reply({"status": self.server.bls_status, "message": ["daily threshold reached"]})
        series = [
            {"seriesID": "CUUR0000SA0", "data": [{"year": "2026", "period": "M10", "value": "331.2"},
                                                 {"year": "2025", "period": "M10", "value": "321.0"}]},
            {"seriesID": "CES0000000001", "data": [{"year": "2026", "period": "M10", "value": "160152"},
                                                   {"year": "2026", "period": "M09", "value": "160000"}]},
            {"seriesID": "LNS14000000", "data": [{"year": "2026", "period": "M10", "value": "4.3"}]},
        ]
        return self.reply({"status": "REQUEST_SUCCEEDED", "Results": {"series": series}})

    def log_message(self, *args):
        pass


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeData()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_sports_sheet(self):
        provider = sf.EspnProvider(base=f"{self.server.base}/espn")
        sheet = sf.build_facts(provider, ["nba", "nhl"], sf.Source("espn", False, "ESPN"), today=TODAY)
        results = sheet.sections["RESULTS, NBA"]
        self.assertEqual(results, ["Tuesday November 17: New York Knicks 111, Toronto Raptors 102 (final). "
                                   "Records now: New York Knicks 8-6; Toronto Raptors 5-9. Points leader for the "
                                   "Knicks: Jalen Brunson, 31. Recap: Brunson scores 31 as Knicks win"])
        self.assertEqual(sheet.sections["COMING UP TODAY, NBA"],
                         ["Memphis Grizzlies at Cleveland Cavaliers, 7:00 PM Eastern, Rocket Arena. "
                          "Records: Memphis Grizzlies 10-4; Cleveland Cavaliers 8-7."])
        headlines = sheet.sections["HEADLINES, NBA (ESPN reporting; restate in your own words)"]
        self.assertEqual(headlines, ["Knicks extend streak over Raptors. New York has won 15 straight in the series."])
        self.assertEqual(sheet.data["held_back"], ["Star guard arrested after incident"])
        self.assertFalse(any("NHL" in h for h in sheet.sections))  # out of season: nothing
        self.assertFalse(sheet.cleared)
        self.assertEqual(sheet.uncleared(), ["espn"])
        final = [g for g in sheet.data["games"] if g["state"] == "post"][0]
        self.assertEqual((final["away"]["score"], final["home"]["score"]), (111, 102))
        script = "The Knicks beat the Raptors 111-102. Jalen Brunson scored 31. They are 8-6."
        self.assertTrue(fc.check(script, sheet.text()).ok)

    def providers(self):
        base = self.server.base
        return mf.Providers(fred=mf.FredProvider("key", base=f"{base}/fred"),
                            treasury=mf.TreasuryProvider(url=f"{base}/treasury/{{year}}/all"),
                            bls=mf.BlsProvider("", v2=f"{base}/bls2", v1=f"{base}/bls1"),
                            fed=mf.FedPressProvider(url=f"{base}/fed.xml"))

    def config(self):
        return {"providers": {"fred": {"cleared_for_air": True, "credit": "FRED"},
                              "fred_third_party": {"cleared_for_air": False},
                              "treasury": {"cleared_for_air": True}, "bls": {"cleared_for_air": True},
                              "fed_press": {"cleared_for_air": True}},
                "series": {"SP500": {"name": "S&P 500", "kind": "index", "third_party": True},
                           "DCOILWTICO": {"name": "WTI crude oil", "kind": "price", "unit": "a barrel"},
                           "CPIAUCSL": {"name": "Consumer price index", "kind": "yoy"},
                           "PAYEMS": {"name": "Nonfarm payrolls", "kind": "monthly_change", "unit": "thousand jobs"},
                           "MISSING": {"name": "Gone", "kind": "price"}}}

    def test_closing_tape_sheet(self):
        sheet = mf.build_facts("closing_tape", self.providers(), self.config(), today=TODAY)
        self.assertEqual(sheet.sections["THE TAPE (latest closes available)"],
                         ["S&P 500 closed at 6,712.34 on Tuesday November 17, 2026, down 1.23% from 6,795.91 on "
                          "Monday November 16, 2026."])
        self.assertEqual(sheet.sections["COMMODITIES"],
                         ["WTI crude oil: $72.15 a barrel on Tuesday November 17, 2026, down $1.25 from Monday "
                          "November 16, 2026."])
        economy = sheet.sections["THE ECONOMY"]
        self.assertIn("Consumer price index: up 3.1% from a year earlier, for October 2026.", economy)
        self.assertIn("Nonfarm payrolls: up 152 thousand jobs in October 2026.", economy)
        self.assertEqual(sheet.sections["CALENDAR (scheduled data releases)"],
                         ["Thursday November 19, 2026: Housing Starts; Jobless Claims."])
        self.assertIn("so it is down 0.03 points", sheet.sections["TREASURY YIELDS"][0])
        self.assertEqual(sheet.sections["FEDERAL RESERVE BOARD (press releases)"],
                         ["Wednesday November 18, 2026: Minutes of the Federal Open Market Committee."])
        self.assertEqual(sheet.uncleared(), ["fred_third_party"])  # the S&P belongs to its owner
        self.assertTrue(any("MISSING" in w for w in sheet.warnings))
        script = "Let's read the tape. The S&P 500 closed at 6,712, down 1.23%. The 10-year is at 5.07%."
        self.assertTrue(fc.check(script, sheet.text()).ok)

    def test_opening_bell_uses_bls_and_reports_its_errors(self):
        sheet = mf.build_facts("opening_bell", self.providers(), self.config(), today=TODAY)
        releases = sheet.sections["THIS MORNING'S RELEASES (Bureau of Labor Statistics)"]
        self.assertEqual(releases, ["Consumer prices for October 2026: up 3.2% from a year earlier.",
                                    "Employers added 152 thousand jobs in October 2026.",
                                    "The unemployment rate for October 2026 was 4.3%."])
        self.server.bls_status = "REQUEST_NOT_PROCESSED"
        sheet = mf.build_facts("opening_bell", self.providers(), self.config(), today=TODAY)
        self.assertTrue(any("daily threshold" in w for w in sheet.warnings))

    def test_fred_needs_a_key(self):
        with self.assertRaises(mf.FeedError):
            mf.FredProvider("")


if __name__ == "__main__":
    unittest.main()
