#!/usr/bin/env python3
"""
Flight prices with multi-provider capture (lake-first).

Each provider response is written to its own landing object before any merge.
Bronze rows point at that landing via raw_object_key. HTML landings keep exact
bytes (input_format=html). Paid/experimental providers require policy approval.

Flow:
    Provider bytes → landing/provider=… → Bronze → manifest
                 → DuckDB API → optional CSV
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = PROJECT_ROOT / "data"

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass


def _require_httpx():
    try:
        import httpx as _httpx
    except ImportError:
        print("ERROR: httpx required for live ingestion. Install: pip install httpx")
        raise SystemExit(1)
    return _httpx


class _HttpxProxy:
    def __getattr__(self, name):
        return getattr(_require_httpx(), name)


httpx = _HttpxProxy()

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None  # type: ignore

try:
    from . import config as _dp_config
    from .policy import evaluate_provider, require_provider, external_writes_allowed
    from .store import seed_fixtures
    from . import lake as _lake
    from .fsutil import atomic_append_csv, atomic_write_csv
    from .quality import finite_number
except ImportError:  # pragma: no cover
    _dp_config = None
    _lake = None

    def evaluate_provider(name):
        class D:
            allowed = True
            status = "free"
            reason = ""

        return D()

    def require_provider(name):
        return evaluate_provider(name)

    def external_writes_allowed():
        return False

    def seed_fixtures(data_dir=None):
        return None


TEQUILA_BASE = "https://api.tequila.kiwi.com"
DEFAULT_ROUTES = [
    ("BKK", "SIN"),
    ("BKK", "TYO"),
    ("BKK", "HKG"),
    ("BKK", "ICN"),
    ("BKK", "KUL"),
    ("BKK", "SGN"),
    ("BKK", "TPE"),
    ("BKK", "MNL"),
]


def _tequila_key() -> str:
    if _dp_config is not None:
        return getattr(_dp_config, "TEQUILA_API_KEY", "") or os.environ.get(
            "TEQUILA_API_KEY", ""
        )
    return os.environ.get("TEQUILA_API_KEY", "")


def _firecrawl_key() -> str:
    if _dp_config is not None:
        return getattr(_dp_config, "FIRECRAWL_API_KEY", "") or os.environ.get(
            "FIRECRAWL_API_KEY", ""
        )
    return os.environ.get("FIRECRAWL_API_KEY", "")


# ---------------------------------------------------------------------------
# Provider captures: always return (raw_bytes, content_type, flights)
# ---------------------------------------------------------------------------


def fetch_kiwi_tequila_raw(
    origin: str, destination: str, date_from: str, date_to: str
) -> tuple[bytes, str, list[dict[str, Any]]]:
    """Kiwi/Tequila API — blocked under free-only unless explicitly allowed."""
    decision = evaluate_provider("kiwi_tequila")
    if not decision.allowed:
        print(f"  Kiwi/Tequila blocked ({decision.status}): {decision.reason}")
        return b"", "application/json", []
    api_key = _tequila_key()
    if not api_key:
        print("  Kiwi/Tequila skipped: missing TEQUILA_API_KEY")
        return b"", "application/json", []
    try:
        resp = httpx.get(
            f"{TEQUILA_BASE}/v2/search",
            headers={"apikey": api_key},
            params={
                "fly_from": origin,
                "fly_to": destination,
                "date_from": date_from,
                "date_to": date_to,
                "curr": "THB",
                "locale": "en",
                "sort": "price",
                "limit": 10,
                "one_for_city": 1,
            },
            timeout=30,
        )
        resp.raise_for_status()
        raw = getattr(resp, "content", None) or json.dumps(resp.json()).encode("utf-8")
        return raw, "application/json", parse_tequila(resp.json(), origin, destination)
    except Exception as e:  # noqa: BLE001
        print(f"  Warning: Tequila API failed for {origin}-{destination}: {e}")
        return b"", "application/json", []


def _tequila_time(value: Any) -> str:
    """Render a Tequila ``dTime``/``aTime`` epoch as ``YYYY-MM-DD HH:MM``.

    Tequila encodes the airport-local wall time as epoch seconds, so it is
    rendered with UTC arithmetic; a host-local conversion shifted it by the
    host's offset.
    """
    seconds = finite_number(value)
    if not seconds:
        return ""
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


def parse_tequila(data: Any, origin: str, destination: str) -> list[dict[str, Any]]:
    """Normalize a Tequila ``/v2/search`` payload into flight dicts.

    Airlines are sorted so the itinerary fingerprint (and record id) is stable
    across processes (``set`` iteration order is not).
    """
    rows = data.get("data") if isinstance(data, dict) else None
    flights = []
    for flight in rows if isinstance(rows, list) else []:
        if not isinstance(flight, dict):
            continue
        route = [r for r in flight.get("route") or [] if isinstance(r, dict)]
        duration = finite_number(flight.get("fly_duration"))
        flights.append(
            {
                "origin": origin,
                "destination": destination,
                "price_thb": flight.get("price", 0),
                "currency": "THB",
                "airline": ",".join(sorted({str(r.get("airline") or "") for r in route} - {""})),
                "departure": _tequila_time(flight.get("dTime")),
                "return": _tequila_time(flight.get("aTime")),
                "duration_hours": round(duration / 3600, 1) if duration and duration > 0 else 0,
                "stops": max(len(route) - 1, 0),
                "url": flight.get("deep_link", ""),
                "source": "Kiwi.com",
            }
        )
    return flights


def parse_skyscanner(markdown: str, origin: str, destination: str) -> list:
    flights = []
    price_matches = re.findall(r"(?:฿|THB)\s*(\d[\d,]*)", markdown)
    if price_matches:
        prices = [
            int(p.replace(",", ""))
            for p in price_matches
            if int(p.replace(",", "")) > 500
        ]
        if prices:
            flights.append(
                {
                    "origin": origin,
                    "destination": destination,
                    "price_thb": min(prices),
                    "currency": "THB",
                    "airline": "",
                    "departure": "",
                    "return": "",
                    "duration_hours": 0,
                    "stops": 0,
                    "url": (
                        f"https://www.skyscanner.com/transport/flights/"
                        f"{origin.lower()}/{destination.lower()}/"
                    ),
                    "source": "Skyscanner",
                    "content_type_hint": "text/html",
                }
            )
    return flights


def fetch_skyscanner_html_raw(
    origin: str, destination: str
) -> tuple[bytes, str, list[dict[str, Any]]]:
    """Public HTML adapter — experimental; blocked while FREE_ONLY=true by default."""
    decision = evaluate_provider("public_html_experimental")
    if not decision.allowed:
        print(
            f"  Public HTML adapter blocked ({decision.status}): {decision.reason}"
        )
        return b"", "text/html", []
    if BeautifulSoup is None:
        print("  Public HTML skipped: beautifulsoup4 not installed")
        return b"", "text/html", []
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }
    url = (
        f"https://www.skyscanner.com/transport/flights/"
        f"{origin.lower()}/{destination.lower()}/"
    )
    try:
        resp = httpx.get(url, headers=headers, timeout=15, follow_redirects=True)
        raw = getattr(resp, "content", None) or resp.text.encode("utf-8", errors="replace")
        if resp.status_code != 200:
            return raw, "text/html", []
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup.find_all(["script", "style", "nav", "footer", "aside"]):
            tag.decompose()
        main = soup.find("main") or soup.find("article") or soup.body
        text = main.get_text(separator=" ", strip=True) if main else resp.text
        flights = parse_skyscanner(text, origin, destination)
        return raw, "text/html", flights
    except Exception as e:  # noqa: BLE001
        print(f"  Warning: Skyscanner scrape failed for {origin}-{destination}: {e}")
        return b"", "text/html", []


def fetch_firecrawl_raw(
    origin: str, destination: str
) -> tuple[bytes, str, list[dict[str, Any]]]:
    """Firecrawl paid scrape — policy-gated."""
    decision = evaluate_provider("firecrawl")
    if not decision.allowed:
        print(f"  Firecrawl blocked ({decision.status}): {decision.reason}")
        return b"", "application/json", []
    api_key = _firecrawl_key()
    if not api_key:
        print("  Firecrawl skipped: missing FIRECRAWL_API_KEY")
        return b"", "application/json", []
    skyscanner_url = (
        f"https://www.skyscanner.com/transport/flights/"
        f"{origin.lower()}/{destination.lower()}/"
    )
    try:
        resp = httpx.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"url": skyscanner_url, "formats": ["markdown"]},
            timeout=45,
        )
        raw = getattr(resp, "content", None) or resp.text.encode("utf-8", errors="replace")
        if resp.status_code != 200:
            return raw, "application/json", []
        data = resp.json()
        md = data.get("data", {}).get("markdown", "") or ""
        flights = parse_skyscanner(md, origin, destination) if md else []
        return raw, "application/json", flights
    except Exception as e:  # noqa: BLE001
        print(f"  Firecrawl scrape failed: {e}")
        return b"", "application/json", []


def _projection_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _projection_rows(flights: list[dict], fieldnames: list[str], scraped: str) -> list[dict]:
    return [{k: f.get(k, "") for k in fieldnames} | {"scraped_at": scraped} for f in flights]


def valid_price(value: Any) -> bool:
    """True for a finite, positive fare (NaN is truthy, so ``if price`` let it through)."""
    number = finite_number(value)
    return number is not None and number > 0


def project_prices_csv(flights: list[dict], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / "flight_prices.csv"
    fieldnames = [
        "origin",
        "destination",
        "price_thb",
        "airline",
        "departure",
        "return",
        "duration_hours",
        "stops",
        "url",
        "source",
        "provider",
        "departure_date",
        "observed_at",
        "currency",
        "scraped_at",
    ]
    scraped = _projection_timestamp()
    atomic_write_csv(filepath, fieldnames, _projection_rows(flights, fieldnames, scraped))
    print(f"  Projected {len(flights)} flights → {filepath}")
    return filepath


def project_history_csv(flights: list[dict], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / "flight_prices_history.csv"
    fieldnames = [
        "origin",
        "destination",
        "price_thb",
        "airline",
        "departure",
        "return",
        "duration_hours",
        "stops",
        "url",
        "source",
        "provider",
        "departure_date",
        "observed_at",
        "currency",
        "scraped_at",
    ]
    scraped = _projection_timestamp()
    atomic_append_csv(filepath, fieldnames, _projection_rows(flights, fieldnames, scraped))
    print(f"  Projected +{len(flights)} history rows → {filepath}")
    return filepath


def load_previous_prices(output_dir: Optional[Path] = None) -> dict:
    history_file = Path(output_dir or OUTPUT_DIR) / "flight_prices_history.csv"
    prices = {}
    if history_file.exists():
        with open(history_file, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                key = f"{row.get('origin', '')}-{row.get('destination', '')}"
                try:
                    prices[key] = float(row.get("price_thb", 0))
                except (ValueError, TypeError):
                    pass
    return prices


def print_alerts(flights: list, prev_prices: dict, drop_pct: float):
    print("\n  Alerts:")
    alerted = False
    for flight in flights:
        key = f"{flight['origin']}-{flight['destination']}"
        try:
            price = float(flight.get("price_thb") or 0)
        except (TypeError, ValueError):
            continue
        if key in prev_prices and prev_prices[key] > 0:
            change = (price - prev_prices[key]) / prev_prices[key] * 100
            if change <= -drop_pct:
                print(
                    f"    *** {key}: PRICE DROP {change:+.1f}% "
                    f"(฿{prev_prices[key]:,.0f} → ฿{price:,.0f}) ***"
                )
                alerted = True
            elif change >= drop_pct:
                print(
                    f"    *** {key}: PRICE INCREASE {change:+.1f}% "
                    f"(฿{prev_prices[key]:,.0f} → ฿{price:,.0f}) ***"
                )
                alerted = True
    if not alerted:
        print(f"    No significant price changes (alert threshold: {drop_pct}%)")


def _land_provider(
    *,
    provider: str,
    raw: bytes,
    flights: list[dict],
    content_type: str,
    dataset: str,
    data_lake_uri: Optional[str],
    output_dir: Path,
    extra_meta: Optional[dict] = None,
) -> dict[str, Any]:
    if _lake is None or _dp_config is None:
        raise RuntimeError("Lake adapter unavailable")
    if not raw:
        raise _lake.LakeIngestError(f"Empty raw payload from {provider}")
    input_format = "html" if content_type.startswith("text/html") else "json"
    result = _lake.ingest_provider_payload(
        provider=provider,
        raw=raw,
        flights=flights,
        dataset=dataset,
        content_type=content_type,
        input_format=input_format,
        data_lake_uri=data_lake_uri,
        metadata=extra_meta or {},
    )
    _lake.write_lineage(
        result, dataset=dataset, data_dir=output_dir, provider=provider
    )
    print(
        f"  Lake {dataset}/{provider}: run_id={result.get('run_id')} "
        f"records={result.get('record_count')} raw={result.get('raw_key')}"
    )
    return result


def collect_route_provider_captures(
    origin: str,
    destination: str,
    date_from: str,
    date_to: str,
) -> list[dict[str, Any]]:
    """Try allowed providers; never merge raw bytes across providers."""
    captures: list[dict[str, Any]] = []

    raw, ctype, flights = fetch_kiwi_tequila_raw(
        origin, destination, date_from, date_to
    )
    if flights and raw:
        captures.append(
            {
                "provider": "kiwi_tequila",
                "raw": raw,
                "content_type": ctype,
                "flights": flights,
            }
        )
        return captures  # prefer Kiwi when available

    raw, ctype, flights = fetch_skyscanner_html_raw(origin, destination)
    if flights and raw:
        captures.append(
            {
                "provider": "public_html_experimental",
                "raw": raw,
                "content_type": ctype,
                "flights": flights,
            }
        )
        return captures

    raw, ctype, flights = fetch_firecrawl_raw(origin, destination)
    if flights and raw:
        captures.append(
            {
                "provider": "firecrawl",
                "raw": raw,
                "content_type": ctype,
                "flights": flights,
            }
        )
    return captures


def run_live_ingest(
    *,
    routes: list[tuple[str, str]],
    output_dir: Path,
    days_ahead: int = 30,
    alert_drop_pct: float = 15.0,
    write_history: bool = True,
    data_lake_uri: Optional[str] = None,
    project_csv: bool = True,
) -> dict[str, Any]:
    date_from = datetime.now().strftime("%d/%m/%Y")
    date_to = (datetime.now() + timedelta(days=days_ahead)).strftime("%d/%m/%Y")
    print(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
        "Flight Price Scraper (lake-first multi-provider)"
    )
    print(f"  Routes: {len(routes)} | Days ahead: {days_ahead}")
    if data_lake_uri:
        print(f"  Data lake: {data_lake_uri}")
    elif _lake is not None:
        print(f"  Data lake: {_lake.default_data_lake_uri()}")

    all_projection_rows: list[dict[str, Any]] = []
    lake_results: list[dict[str, Any]] = []
    providers_used: list[str] = []

    for orig, dest in routes:
        route_key = f"{orig}-{dest}"
        print(f"\n  {route_key}...", end=" ")
        captures = collect_route_provider_captures(orig, dest, date_from, date_to)
        if not captures:
            print("No data (providers blocked or empty)")
            continue
        for cap in captures:
            provider = cap["provider"]
            flights = cap["flights"]
            # Prefer cheapest for projection UX, but land all returned flights.
            priced = [f for f in flights if valid_price(f.get("price_thb"))]
            if not priced:
                print(f"{provider}: no prices")
                continue
            cheapest = min(priced, key=lambda x: float(x.get("price_thb") or 0))
            print(f"{provider}: ฿{float(cheapest['price_thb']):,.0f}")
            result = _land_provider(
                provider=provider,
                raw=cap["raw"],
                flights=priced,
                content_type=cap["content_type"],
                dataset=_dp_config.LAKE_DATASET_PRICES,
                data_lake_uri=data_lake_uri,
                output_dir=output_dir,
                extra_meta={"route": route_key},
            )
            lake_results.append(result)
            providers_used.append(provider)

            if write_history:
                hist_raw = json.dumps(
                    {
                        "provider": provider,
                        "dataset": _dp_config.LAKE_DATASET_HISTORY,
                        "parent_run_id": result.get("run_id"),
                        "route": route_key,
                        "flights": priced,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
                hist = _land_provider(
                    provider=provider,
                    raw=hist_raw,
                    flights=priced,
                    content_type="application/json",
                    dataset=_dp_config.LAKE_DATASET_HISTORY,
                    data_lake_uri=data_lake_uri,
                    output_dir=output_dir,
                    extra_meta={"route": route_key, "dataset_role": "history"},
                )
                lake_results.append(hist)

            # Projection rows (after lake success only for this capture).
            for f in priced:
                row = {
                    "origin": f.get("origin"),
                    "destination": f.get("destination"),
                    "price_thb": f.get("price_thb"),
                    "airline": f.get("airline", ""),
                    "departure": f.get("departure", ""),
                    "return": f.get("return", ""),
                    "duration_hours": f.get("duration_hours", 0),
                    "stops": f.get("stops", 0),
                    "url": f.get("url", ""),
                    "source": f.get("source", provider),
                    "provider": provider,
                    "departure_date": str(f.get("departure") or "")[:10],
                    "observed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "currency": f.get("currency") or "THB",
                }
                all_projection_rows.append(row)

    if not lake_results:
        raise RuntimeError(
            "No flight data landed. Free-only mode blocks paid/experimental "
            "providers; use --fixture or enable providers with policy approval."
        )

    if project_csv and all_projection_rows:
        # Read prior prices before this run appends to history; reading them
        # afterwards compared each price with itself and alerts never fired.
        prev = load_previous_prices(output_dir)
        project_prices_csv(all_projection_rows, output_dir)
        if write_history:
            project_history_csv(all_projection_rows, output_dir)
        print_alerts(all_projection_rows, prev, alert_drop_pct)

    print(f"\n  Landed batches: {len(lake_results)} | Providers: {sorted(set(providers_used))}")
    print("  Done (per-provider landing; CSV is projection only).")
    return {
        "flights": len(all_projection_rows),
        "lake_batches": len(lake_results),
        "providers": sorted(set(providers_used)),
        "projected_csv": project_csv,
    }


def main(
    routes=None,
    origin="BKK",
    days_ahead=30,
    alert_drop_pct=15.0,
    no_history=False,
    output_dir=None,
    data_lake_uri=None,
    project_csv=True,
):
    if routes is None:
        routes = DEFAULT_ROUTES
    elif isinstance(routes, str):
        routes = [
            (r.split("-")[0], r.split("-")[1]) for r in routes.split(",") if "-" in r
        ]
    elif isinstance(routes, list):
        normalized = []
        for r in routes:
            if isinstance(r, str) and "-" in r:
                a, b = r.split("-", 1)
                normalized.append((a, b))
            elif isinstance(r, (list, tuple)) and len(r) == 2:
                normalized.append((r[0], r[1]))
        routes = normalized

    return run_live_ingest(
        routes=routes,
        output_dir=Path(output_dir or OUTPUT_DIR),
        days_ahead=days_ahead,
        alert_drop_pct=alert_drop_pct,
        write_history=not no_history,
        data_lake_uri=data_lake_uri,
        project_csv=project_csv,
    )


class FlightPriceScraper:
    def __init__(
        self, routes=None, origin="BKK", days_ahead=30, alert_drop_pct=15.0, **kwargs
    ):
        raw = routes or DEFAULT_ROUTES
        if isinstance(raw, list):
            self.routes = []
            for r in raw:
                if isinstance(r, str) and "-" in r:
                    parts = r.split("-")
                    self.routes.append((parts[0], parts[1]))
                elif isinstance(r, (list, tuple)) and len(r) == 2:
                    self.routes.append(tuple(r))
        else:
            self.routes = raw
        self.origin = origin
        self.days_ahead = days_ahead
        self.alert_drop_pct = alert_drop_pct
        self.data_lake_uri = kwargs.get("data_lake_uri")
        self.output_dir = Path(kwargs.get("output_dir") or OUTPUT_DIR)

    async def run(self, **kwargs):
        result = run_live_ingest(
            routes=self.routes,
            output_dir=Path(kwargs.get("output_dir") or self.output_dir),
            days_ahead=self.days_ahead,
            alert_drop_pct=self.alert_drop_pct,
            write_history=True,
            data_lake_uri=kwargs.get("data_lake_uri", self.data_lake_uri),
            project_csv=True,
        )
        return [{"source": "flights", "count": result["flights"], "lake": True}]


_IATA_RE = re.compile(r"[A-Z]{3}")


def parse_routes(text: str) -> list[tuple[str, str]]:
    """Parse ``BKK-NRT,bkk-icn`` into unique ``(ORIGIN, DEST)`` IATA pairs.

    Raises ``ValueError`` for malformed pairs, non-IATA codes, identical
    origin/destination, or an empty list.
    """
    routes: list[tuple[str, str]] = []
    for part in str(text or "").split(","):
        part = part.strip().upper()
        if not part:
            continue
        origin, sep, dest = part.partition("-")
        origin, dest = origin.strip(), dest.strip()
        if not sep or not _IATA_RE.fullmatch(origin) or not _IATA_RE.fullmatch(dest):
            raise ValueError(f"invalid route {part!r}: expected ORIGIN-DEST IATA codes, e.g. BKK-NRT")
        if origin == dest:
            raise ValueError(f"invalid route {part!r}: origin and destination are the same")
        if (origin, dest) not in routes:
            routes.append((origin, dest))
    if not routes:
        raise ValueError("no routes given")
    return routes


def _cli_main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scrape flight prices (lake-first multi-provider)"
    )
    parser.add_argument("--routes", default=None)
    parser.add_argument("--origin", default="BKK")
    parser.add_argument("--destinations", default=None)
    parser.add_argument("--days-ahead", type=int, default=30)
    parser.add_argument("--alert-drop-pct", type=float, default=15.0)
    parser.add_argument("--no-history", action="store_true")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    parser.add_argument("--data-lake-uri", default=None)
    parser.add_argument("--no-project-csv", action="store_true")
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    out = Path(args.output_dir)
    if _dp_config is not None:
        _dp_config.DATA_DIR = out

    if args.fixture or args.dry_run:
        out.mkdir(parents=True, exist_ok=True)
        seed_fixtures(out)
        print(
            "["
            + datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            + "] Fixture mode: seeded local projection under "
            + str(out)
        )
        print("  No upstream providers contacted; no lake write.")
        return 0

    if args.days_ahead < 1:
        parser.error("--days-ahead must be >= 1")
    if not math.isfinite(args.alert_drop_pct) or args.alert_drop_pct < 0:
        parser.error("--alert-drop-pct must be a finite, non-negative number")
    routes = None
    try:
        if args.routes:
            routes = parse_routes(args.routes)
        elif args.destinations:
            origin = args.origin.strip().upper()
            routes = parse_routes(
                ",".join(f"{origin}-{d.strip()}" for d in args.destinations.split(",") if d.strip())
            )
    except ValueError as exc:
        parser.error(str(exc))

    try:
        main(
            routes=routes,
            origin=args.origin,
            days_ahead=args.days_ahead,
            alert_drop_pct=args.alert_drop_pct,
            no_history=args.no_history,
            output_dir=str(out),
            data_lake_uri=args.data_lake_uri,
            project_csv=not args.no_project_csv,
        )
    except Exception as exc:  # noqa: BLE001
        if _lake is not None and isinstance(
            exc, (_lake.LakeUnavailable, _lake.LakeIngestError)
        ):
            print(
                f"ERROR: lake ingest failed before projection: {exc}",
                file=sys.stderr,
            )
            return 2
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())
