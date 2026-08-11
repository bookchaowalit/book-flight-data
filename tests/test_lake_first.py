"""Lake-first multi-provider tests for book-flight-data."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from book_flight import config, lake
from book_flight.ingest import run_live_ingest
from book_flight.store import load_records, seed_lake_from_provider_captures


SAMPLE = {
    "origin": "BKK",
    "destination": "SIN",
    "price_thb": 3500,
    "airline": "TG",
    "departure": "2026-09-01 10:00",
    "return": "",
    "duration_hours": 2.5,
    "stops": 0,
    "url": "https://example.com/bkk-sin",
    "source": "Kiwi.com",
    "currency": "THB",
}


class IdentityTests(unittest.TestCase):
    def test_stable_id_includes_route_date_provider_itinerary(self):
        rec = lake.normalize_flight_record(SAMPLE, provider="kiwi_tequila")
        self.assertEqual(rec["provider"], "kiwi_tequila")
        self.assertEqual(rec["origin"], "BKK")
        self.assertEqual(rec["destination"], "SIN")
        self.assertEqual(rec["departure_date"], "2026-09-01")
        self.assertNotEqual(rec["event_time"], rec["departure_date"])
        self.assertIn("kiwi", rec["id"])
        self.assertIn("BKK", rec["id"])
        self.assertIn("SIN", rec["id"])
        # Same inputs → same id
        again = lake.normalize_flight_record(SAMPLE, provider="kiwi_tequila")
        self.assertEqual(rec["id"], again["id"])

    def test_event_time_is_observation_not_travel(self):
        rec = lake.normalize_flight_record(
            SAMPLE, provider="kiwi_tequila", observed_at="2026-08-07T12:00:00Z"
        )
        self.assertEqual(rec["observed_at"], "2026-08-07T12:00:00Z")
        self.assertEqual(rec["event_time"], "2026-08-07T12:00:00Z")
        self.assertEqual(rec["departure_date"], "2026-09-01")


class OrderingTests(unittest.TestCase):
    def test_fail_closed_no_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)

            def boom(**_k):
                raise lake.LakeIngestError("simulated")

            with mock.patch(
                "book_flight.ingest.collect_route_provider_captures",
                return_value=[
                    {
                        "provider": "kiwi_tequila",
                        "raw": b'{"ok":true}',
                        "content_type": "application/json",
                        "flights": [SAMPLE],
                    }
                ],
            ):
                with mock.patch.object(lake, "ingest_provider_payload", side_effect=boom):
                    with self.assertRaises(lake.LakeIngestError):
                        run_live_ingest(
                            routes=[("BKK", "SIN")],
                            output_dir=out,
                            write_history=False,
                            project_csv=True,
                        )
            self.assertFalse((out / "flight_prices.csv").exists())


@unittest.skipUnless(
    lake.find_solo_empire_root() is not None,
    "monorepo data_lake required",
)
class RealLakeTests(unittest.TestCase):
    def test_per_provider_landing_and_html_bytes(self):
        try:
            import pyarrow  # noqa: F401
        except ImportError:
            self.skipTest("pyarrow missing")

        with tempfile.TemporaryDirectory() as tmp:
            lake_uri = str(Path(tmp) / "lake")
            html = b"<html><body>flight prices THB 8900</body></html>"
            json_raw = b'{"data":[{"price":3500}]}'
            results = seed_lake_from_provider_captures(
                [
                    {
                        "provider": "kiwi_tequila",
                        "raw": json_raw,
                        "flights": [SAMPLE],
                        "content_type": "application/json",
                    },
                    {
                        "provider": "public_html_experimental",
                        "raw": html,
                        "flights": [
                            {
                                **SAMPLE,
                                "destination": "TYO",
                                "price_thb": 8900,
                                "departure": "2026-09-05 22:00",
                                "source": "Skyscanner",
                            }
                        ],
                        "content_type": "text/html",
                        "input_format": "html",
                    },
                ],
                data_lake_uri=lake_uri,
            )
            # Find HTML landing
            html_landings = [
                r for r in results if r["raw_key"].endswith(".html") or "html" in r["raw_key"]
            ]
            self.assertTrue(html_landings)
            raw_key = html_landings[0]["raw_key"]
            self.assertIn("provider=", raw_key)
            self.assertEqual(
                lake.landing_object_bytes(raw_key, data_lake_uri=lake_uri), html
            )
            # JSON landing distinct
            json_landings = [
                r
                for r in results
                if r["raw_key"].endswith(".json") and "kiwi" in r["raw_key"]
            ]
            self.assertTrue(json_landings)
            self.assertEqual(
                lake.landing_object_bytes(
                    json_landings[0]["raw_key"], data_lake_uri=lake_uri
                ),
                json_raw,
            )
            # Replay twice identical
            self.assertEqual(
                lake.landing_object_bytes(raw_key, data_lake_uri=lake_uri),
                lake.landing_object_bytes(raw_key, data_lake_uri=lake_uri),
            )

            with mock.patch.object(config, "DATA_LAKE_URI", lake_uri):
                payload = load_records()
            self.assertGreaterEqual(len(payload["items"]), 2)
            for item in payload["items"]:
                self.assertTrue(item["raw_object_key"])
                self.assertTrue(item["provider"])

    def test_bronze_payload_survives_html_landing(self):
        try:
            import duckdb  # noqa: F401
            import pyarrow  # noqa: F401
        except ImportError:
            self.skipTest("deps missing")

        with tempfile.TemporaryDirectory() as tmp:
            lake_uri = str(Path(tmp) / "lake")
            html = b"<html>THB 8900</html>"
            lake.ingest_provider_payload(
                provider="public_html_experimental",
                raw=html,
                flights=[
                    {
                        **SAMPLE,
                        "destination": "TYO",
                        "price_thb": 8900,
                        "departure": "2026-09-05 22:00",
                    }
                ],
                dataset=config.LAKE_DATASET_PRICES,
                content_type="text/html",
                input_format="html",
                data_lake_uri=lake_uri,
            )
            rows = lake.read_bronze_rows(
                config.LAKE_DATASET_PRICES, data_lake_uri=lake_uri
            )
            self.assertEqual(len(rows), 1)
            payload = lake.parse_payload_json(rows[0])
            self.assertEqual(payload.get("destination"), "TYO")
            self.assertEqual(float(payload.get("price_thb")), 8900)
            self.assertTrue(rows[0]["raw_object_key"].endswith(".html"))


class SharedAdapterTests(unittest.TestCase):
    def test_contract(self):
        self.assertEqual(config.LAKE_SOURCE, "book-flight-data")
        self.assertEqual(config.LAKE_DOMAIN, "travel")
        self.assertEqual(config.LAKE_DATASET_PRICES, "flight_prices")
        self.assertEqual(config.SCHEMA_VERSION, "flight.v1")
        root = lake.find_solo_empire_root()
        if root:
            self.assertTrue(
                (root / "infra" / "scripts" / "data_lake" / "product_adapter.py").is_file()
            )


if __name__ == "__main__":
    src = Path(__file__).resolve().parents[1] / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    unittest.main()
