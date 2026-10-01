"""Offline provider parser tests from saved captures (no network)."""
from __future__ import annotations

import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from book_flight import ingest, lake

PAGES = Path(__file__).resolve().parent / "pages"
OBSERVED = "2026-08-01T12:00:00Z"


def _load(name: str) -> str:
    return (PAGES / name).read_text(encoding="utf-8")


class TequilaParserTests(unittest.TestCase):
    def test_saved_search_payload(self):
        flights = ingest.parse_tequila(json.loads(_load("tequila_search.json")), "BKK", "NRT")
        self.assertEqual(len(flights), 3)  # non-object row skipped
        first = flights[0]
        self.assertEqual(first["airline"], "NH,TG")  # sorted, de-duplicated
        # dTime 1788775200 is 2026-09-07 10:00 airport-local (UTC arithmetic).
        self.assertEqual(first["departure"], "2026-09-07 10:00")
        self.assertEqual(first["return"], "2026-09-07 16:20")
        self.assertEqual((first["duration_hours"], first["stops"]), (6.3, 2))
        self.assertEqual(first["price_thb"], 12450)
        self.assertEqual(flights[2]["departure"], "")

    def test_ids_stable_and_invalid_prices_dropped(self):
        flights = ingest.parse_tequila(json.loads(_load("tequila_search.json")), "BKK", "NRT")
        a = lake.flight_records_from_provider(flights, provider="kiwi_tequila", observed_at=OBSERVED)
        b = lake.flight_records_from_provider(
            ingest.parse_tequila(json.loads(_load("tequila_search.json")), "BKK", "NRT"),
            provider="kiwi_tequila",
            observed_at=OBSERVED,
        )
        self.assertEqual([r["id"] for r in a], [r["id"] for r in b])
        self.assertEqual([r["price"] for r in a], [12450, 9800])

    def test_malformed_payloads(self):
        for payload in (None, [], {"data": None}, {"data": "x"}):
            with self.subTest(payload=payload):
                self.assertEqual(ingest.parse_tequila(payload, "BKK", "NRT"), [])


class SkyscannerParserTests(unittest.TestCase):
    def test_firecrawl_markdown_capture(self):
        markdown = json.loads(_load("firecrawl_skyscanner.json"))["data"]["markdown"]
        flights = ingest.parse_skyscanner(markdown, "BKK", "NRT")
        # Cheapest fare > 500, "฿ 12,450" (with a space) is recognised, and the
        # lone "฿," token no longer crashes int("").
        self.assertEqual([f["price_thb"] for f in flights], [12450])
        self.assertEqual(flights[0]["url"], "https://www.skyscanner.com/transport/flights/bkk/nrt/")

    def test_no_prices(self):
        self.assertEqual(ingest.parse_skyscanner("no fares ฿, THB", "BKK", "NRT"), [])

    @unittest.skipIf(ingest.BeautifulSoup is None, "beautifulsoup4 not installed")
    def test_saved_html_page_ignores_script_nav_and_footer(self):
        html = _load("skyscanner_page.html")
        response = SimpleNamespace(status_code=200, text=html, content=html.encode("utf-8"))
        allowed = SimpleNamespace(allowed=True, status="free", reason="")
        with mock.patch.object(ingest, "evaluate_provider", return_value=allowed), \
                mock.patch.object(ingest.httpx, "get", return_value=response, create=True):
            raw, ctype, flights = ingest.fetch_skyscanner_html_raw("BKK", "NRT")
        self.assertEqual((raw, ctype), (html.encode("utf-8"), "text/html"))
        self.assertEqual([f["price_thb"] for f in flights], [11980])


class PriceQualityTests(unittest.TestCase):
    def test_valid_price(self):
        self.assertTrue(ingest.valid_price("9800"))
        for bad in (None, "", 0, -5, float("nan"), float("inf"), "cheap"):
            with self.subTest(bad=bad):
                self.assertFalse(ingest.valid_price(bad))

    def test_records_blank_bad_duration_and_stops(self):
        rec = lake.normalize_flight_record(
            {"origin": "bkk", "destination": "nrt", "price_thb": 100, "duration_hours": float("nan"), "stops": -1},
            provider="kiwi_tequila",
            observed_at=OBSERVED,
        )
        self.assertEqual((rec["duration_hours"], rec["stops"]), ("", ""))


class RouteParsingTests(unittest.TestCase):
    def test_parse_routes(self):
        self.assertEqual(
            ingest.parse_routes(" bkk-nrt,BKK-NRT, CNX-icn ,"), [("BKK", "NRT"), ("CNX", "ICN")]
        )
        for bad in ("", "BKK", "BKK-", "BKK-NRTX", "BKK-BKK", "B1K-NRT"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ingest.parse_routes(bad)

    def test_cli_rejects_bad_arguments(self):
        for argv in (
            ["--routes", "BKK-"],
            ["--destinations", "NRT,TOKYO"],
            ["--days-ahead", "0"],
            ["--alert-drop-pct", "nan"],
        ):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    ingest._cli_main(argv)
                self.assertEqual(ctx.exception.code, 2)

    def test_cli_normalizes_destinations(self):
        with mock.patch.object(ingest, "main") as run:
            self.assertEqual(ingest._cli_main(["--origin", "bkk", "--destinations", "nrt, icn,nrt"]), 0)
        self.assertEqual(run.call_args.kwargs["routes"], [("BKK", "NRT"), ("BKK", "ICN")])


@unittest.skipUnless(
    lake.shared_runtime_available(),
    "shared data_lake runtime not installed (pip install -e .[lake])",
)
class PriceAlertTests(unittest.TestCase):
    """Alerts compare with the history from *before* this run."""

    def _run(self, out: Path, price: float) -> str:
        flights = [{"origin": "BKK", "destination": "NRT", "price_thb": price, "source": "Kiwi.com"}]
        capture = {
            "provider": "kiwi_tequila",
            "raw": json.dumps({"price": price}).encode(),
            "content_type": "application/json",
            "flights": flights,
        }
        buf = io.StringIO()
        with mock.patch.object(ingest, "collect_route_provider_captures", return_value=[capture]), \
                contextlib.redirect_stdout(buf):
            ingest.run_live_ingest(
                routes=[("BKK", "NRT")], output_dir=out, data_lake_uri=str(out / "lake"), alert_drop_pct=10
            )
        return buf.getvalue()

    def test_price_drop_alert_fires(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            self._run(out, 10000)
            second = self._run(out, 8000)
            self.assertIn("PRICE DROP -20.0%", second)
            with (out / "flight_prices_history.csv").open(newline="", encoding="utf-8") as f:
                self.assertEqual([r["price_thb"] for r in csv.DictReader(f)], ["10000", "8000"])
            self.assertFalse([p for p in out.iterdir() if p.name.endswith(".tmp")])


if __name__ == "__main__":
    unittest.main()
