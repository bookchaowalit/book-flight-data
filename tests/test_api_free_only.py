"""Offline tests for book-flight-data free-only API + multi-provider lake."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from book_flight import config, lake, policy
from book_flight.api import DataProductHandler
from book_flight.store import (
    load_csv_projection,
    load_history,
    load_records,
    seed_fixtures,
    seed_lake_from_provider_captures,
)


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures"

SAMPLE_FLIGHTS_KIWI = [
    {
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
]
SAMPLE_FLIGHTS_HTML = [
    {
        "origin": "BKK",
        "destination": "TYO",
        "price_thb": 8900,
        "airline": "JL",
        "departure": "2026-09-05 22:00",
        "return": "",
        "duration_hours": 6.0,
        "stops": 0,
        "url": "https://www.skyscanner.com/transport/flights/bkk/tyo/",
        "source": "Skyscanner",
        "currency": "THB",
        "content_type_hint": "text/html",
    }
]


def _set_data_dir(path: Path):
    return mock.patch.object(config, "DATA_DIR", path)


def _set_lake_uri(uri: str):
    return mock.patch.object(config, "DATA_LAKE_URI", uri)


def _set_read_mode(mode: str, fallback: str = "error"):
    return mock.patch.multiple(
        config,
        LAKE_READ_MODE=mode,
        LAKE_READ_FALLBACK=fallback,
    )


def _lake_deps_available() -> bool:
    try:
        import duckdb  # noqa: F401
        import pyarrow  # noqa: F401
    except ImportError:
        return False
    return lake.find_solo_empire_root() is not None


@unittest.skipUnless(_lake_deps_available(), "pyarrow/duckdb + monorepo required")
class StoreLakeTests(unittest.TestCase):
    def test_multi_provider_landing_not_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            lake_uri = str(Path(tmp) / "lake")
            results = seed_lake_from_provider_captures(
                [
                    {
                        "provider": "kiwi_tequila",
                        "raw": b'{"provider":"kiwi","route":"BKK-SIN"}',
                        "flights": SAMPLE_FLIGHTS_KIWI,
                        "content_type": "application/json",
                    },
                    {
                        "provider": "public_html_experimental",
                        "raw": b"<html><body>BKK TYO THB 8900</body></html>",
                        "flights": SAMPLE_FLIGHTS_HTML,
                        "content_type": "text/html",
                        "input_format": "html",
                    },
                ],
                data_lake_uri=lake_uri,
            )
            # prices + history per provider
            self.assertGreaterEqual(len(results), 2)
            # At least two distinct landings with provider segments
            price_landings = [
                r["raw_key"]
                for r in results
                if r["raw_key"].startswith("landing/")
                and "provider=" in r["raw_key"]
            ]
            self.assertGreaterEqual(len(set(price_landings)), 2)
            self.assertTrue(any("kiwi" in k for k in price_landings))
            self.assertTrue(any("public-html" in k or "html" in k for k in price_landings))

            with _set_lake_uri(lake_uri):
                payload = load_records()
            self.assertEqual(payload["source_kind"], "bronze_parquet")
            providers = {i.get("provider") for i in payload["items"]}
            self.assertIn("kiwi_tequila", providers)
            self.assertIn("public_html_experimental", providers)
            for item in payload["items"]:
                self.assertTrue(item.get("observed_at") or item.get("event_time"))
                self.assertIn("departure_date", item)
                self.assertTrue(item.get("raw_object_key", "").startswith("landing/"))

    def test_csv_does_not_feed_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            lake_uri = str(root / "empty")
            Path(lake_uri).mkdir()
            with mock.patch.object(config, "FIXTURES_DIR", FIXTURE_ROOT):
                seed_fixtures(data_dir)
            self.assertGreaterEqual(
                len(load_csv_projection(kind="records", data_dir=data_dir)["items"]), 1
            )
            with _set_lake_uri(lake_uri), _set_data_dir(data_dir):
                self.assertEqual(load_records()["items"], [])

    def test_iceberg_and_parquet_reads_have_same_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            lake_uri = str(Path(tmp) / "lake")
            seed_lake_from_provider_captures(
                [
                    {
                        "provider": "offline_fixtures",
                        "raw": b'{"fixture":true,"route":"BKK-SIN"}',
                        "flights": SAMPLE_FLIGHTS_KIWI,
                    }
                ],
                data_lake_uri=lake_uri,
            )

            def read_catalog_rows(contract, dataset, *, data_lake_uri=None, sql=None):
                return lake.read_bronze_rows(
                    dataset, data_lake_uri=data_lake_uri, sql=sql
                )

            with _set_lake_uri(lake_uri), _set_read_mode("parquet"):
                expected_prices = load_records()
                expected_history = load_history()
            with _set_lake_uri(lake_uri), _set_read_mode("iceberg"), mock.patch(
                "data_lake.product_store.read_iceberg_rows",
                side_effect=read_catalog_rows,
            ):
                actual_prices = load_records()
                actual_history = load_history()

            self.assertEqual(actual_prices["items"], expected_prices["items"])
            self.assertEqual(actual_history["items"], expected_history["items"])
            self.assertEqual(actual_prices["source_kind"], "iceberg_rest_duckdb")
            self.assertEqual(actual_history["source_kind"], "iceberg_rest_duckdb")
            self.assertEqual(actual_prices["read_mode"], "iceberg")

    def test_iceberg_mode_fallback_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            lake_uri = str(Path(tmp) / "lake")
            seed_lake_from_provider_captures(
                [
                    {
                        "provider": "offline_fixtures",
                        "raw": b'{"fixture":true,"route":"BKK-SIN"}',
                        "flights": SAMPLE_FLIGHTS_KIWI,
                    }
                ],
                data_lake_uri=lake_uri,
            )
            with _set_lake_uri(lake_uri), _set_read_mode("iceberg", "parquet"), mock.patch(
                "data_lake.product_store.read_iceberg_rows",
                side_effect=lake.LakeUnavailable("catalog unavailable"),
            ):
                payload = load_records()

            self.assertGreaterEqual(len(payload["items"]), 1)
            self.assertEqual(payload["source_kind"], "bronze_parquet_fallback")
            self.assertIn("catalog unavailable", payload["fallback_reason"])

    def test_iceberg_mode_fails_closed_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            lake_uri = str(Path(tmp) / "lake")
            seed_lake_from_provider_captures(
                [
                    {
                        "provider": "offline_fixtures",
                        "raw": b'{"fixture":true,"route":"BKK-SIN"}',
                        "flights": SAMPLE_FLIGHTS_KIWI,
                    }
                ],
                data_lake_uri=lake_uri,
            )
            with _set_lake_uri(lake_uri), _set_read_mode("iceberg", "error"), mock.patch(
                "data_lake.product_store.read_iceberg_rows",
                side_effect=lake.LakeUnavailable("catalog credentials unavailable"),
            ):
                payload = load_records()

            self.assertEqual(payload["data_status"], "error")
            self.assertEqual(payload["items"], [])
            self.assertEqual(payload["source_kind"], "iceberg_rest_duckdb")
            self.assertIn("catalog credentials unavailable", payload["error"])


class PolicyTests(unittest.TestCase):
    def test_kiwi_blocked_free_only(self):
        with mock.patch.object(config, "FREE_ONLY", True), mock.patch.object(
            config, "ALLOW_PAID_PROVIDERS", False
        ):
            self.assertFalse(policy.evaluate_provider("kiwi_tequila").allowed)
            self.assertFalse(policy.evaluate_provider("firecrawl").allowed)
            # unknown html blocked while FREE_ONLY
            self.assertFalse(
                policy.evaluate_provider("public_html_experimental").allowed
            )
            self.assertTrue(policy.evaluate_provider("offline_fixtures").allowed)


@unittest.skipUnless(_lake_deps_available(), "pyarrow/duckdb + monorepo required")
class ApiContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data_dir = root / "data"
        self.data_dir.mkdir()
        self.lake_uri = str(root / "lake")
        seed_lake_from_provider_captures(
            [
                {
                    "provider": "offline_fixtures",
                    "raw": b'{"fixture":true,"route":"BKK-SIN"}',
                    "flights": SAMPLE_FLIGHTS_KIWI,
                }
            ],
            data_lake_uri=self.lake_uri,
            data_dir=self.data_dir,
        )
        self.data_patch = _set_data_dir(self.data_dir)
        self.lake_patch = _set_lake_uri(self.lake_uri)
        self.read_patch = _set_read_mode("parquet", "error")
        self.data_patch.start()
        self.lake_patch.start()
        self.read_patch.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), DataProductHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.lake_patch.stop()
        self.data_patch.stop()
        self.read_patch.stop()
        self.tmp.cleanup()

    def _get(self, path: str):
        try:
            with urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_metadata(self):
        status, body = self._get("/v1/metadata")
        self.assertEqual(status, 200)
        meta = body["items"][0]
        self.assertEqual(meta["storage_model"], "lake_first_bronze_duckdb")
        self.assertEqual(meta["capture_mode"], "per_provider_landing")
        self.assertEqual(meta["lake_domain"], "travel")
        self.assertEqual(meta["records_source_kind"], "bronze_parquet")
        self.assertEqual(meta["read_mode"], "parquet")
        self.assertEqual(meta["read_fallback"], "error")

    def test_metadata_reports_iceberg_mode(self):
        def read_catalog_rows(contract, dataset, *, data_lake_uri=None, sql=None):
            return lake.read_bronze_rows(
                dataset, data_lake_uri=data_lake_uri, sql=sql
            )

        with _set_read_mode("iceberg"), mock.patch(
            "data_lake.product_store.read_iceberg_rows",
            side_effect=read_catalog_rows,
        ):
            status, body = self._get("/v1/metadata")

        self.assertEqual(status, 200)
        meta = body["items"][0]
        self.assertEqual(meta["storage_model"], "lake_first_iceberg_rest_duckdb")
        self.assertEqual(meta["records_source_kind"], "iceberg_rest_duckdb")
        self.assertEqual(meta["history_source_kind"], "iceberg_rest_duckdb")
        self.assertEqual(meta["read_mode"], "iceberg")

    def test_api_without_csv(self):
        self.assertFalse(any(self.data_dir.glob("*.csv")))
        status, body = self._get("/v1/records")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(body["items"]), 1)

    def test_refresh_forbidden(self):
        req = Request(
            f"http://127.0.0.1:{self.port}/v1/refresh", data=b"{}", method="POST"
        )
        try:
            urlopen(req, timeout=5)
            self.fail("expected 403")
        except HTTPError as e:
            self.assertEqual(e.code, 403)


class SmokeCompileTests(unittest.TestCase):
    def test_modules_import(self):
        from book_flight import api, config as cfg, store

        self.assertEqual(cfg.SCHEMA_VERSION, "flight.v1")
        self.assertEqual(int(cfg.API_PORT), 8105)
        self.assertEqual(cfg.LAKE_DOMAIN, "travel")
        self.assertEqual(cfg.LAKE_READ_MODE, "parquet")
        self.assertEqual(cfg.LAKE_READ_FALLBACK, "error")
        self.assertTrue(hasattr(store, "load_csv_projection"))
        self.assertTrue(hasattr(api, "main"))


if __name__ == "__main__":
    unittest.main()
