# book-flight-data

Flight price data product — **multi-provider lake-first pilot**.

Migration status: lake-first via shared `data_lake.product_adapter`. Each
provider response is landed separately before any merge. HTML landings keep
exact bytes; Bronze rows hold structured fields + `raw_object_key`.

## Data contract

| Field | Value |
|---|---|
| `source` | `book-flight-data` |
| `domain` | `travel` |
| `datasets` | `flight_prices`, `flight_prices_history` |
| Bronze schema | `1` |
| envelope | `flight.v1` |
| port | `8105` |

```text
Provider response bytes (Kiwi API | Skyscanner HTML | Firecrawl)
    → landing/source=book-flight-data/provider=<name>/…   (no merge)
    → bronze/domain=travel/dataset=flight_prices|… 
    → manifest + lineage
    → DuckDB API
    → optional CSV projection
```

### Stable record identity

`provider : origin : destination : departure_date : itinerary_id`

- `event_time` / `observed_at` = when the **price was observed**
- `departure_date` = travel date (separate field)

### Provider policy (free-only defaults)

| Provider | Default | Notes |
|---|---|---|
| `offline_fixtures` | free | Fixture / dry-run |
| `public_html_experimental` | unknown → blocked | HTML scrape needs opt-in |
| `kiwi_tequila` | blocked | Requires key |
| `firecrawl` | blocked | Paid |
| `brave_search_api` | blocked | Paid |

Policy is enforced **before** any network call.

## Quick start

```bash
pip install -e .
# Lake writes need the shared data-lake runtime: the [lake] extra installs the
# pinned solo-empire-data-lake (plus pyarrow/duckdb); inside Solo Empire the
# parent checkout's infra/scripts/data_lake is used instead
pip install -e ".[lake]"

# Offline (recommended free-only)
python -m book_flight.ingest --fixture

# Live only after enabling allowed providers
python -m book_flight.ingest --routes BKK-SIN --data-lake-uri /path/to/data/lake

python -m book_flight.api --host 127.0.0.1 --port 8105
```

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/v1/metadata` | `capture_mode=per_provider_landing` |
| GET | `/v1/records` | Latest Bronze (not CSV) |
| GET | `/v1/history` | History Bronze |
| POST | `/v1/refresh` | 403 by default |

## Data quality

- Fares must be finite and positive (`ingest.valid_price`); `NaN` is truthy,
  so the old `if price` check let it through. Bronze records without a valid
  fare are dropped, and `NaN`/negative `duration_hours`/`stops` are blanked.
- Tequila times are rendered with UTC arithmetic (the API encodes airport-local
  wall time as epoch seconds) and airlines are sorted, so itinerary ids are
  stable across processes.
- Price alerts compare with the history from *before* the current run.
- `--routes`/`--destinations` must be `ORIGIN-DEST` IATA pairs (upper-cased,
  de-duplicated, origin != destination); `--days-ahead` >= 1 and
  `--alert-drop-pct` finite and >= 0 (exit 2 otherwise).
- CSV projections are written atomically (temp file + `os.replace`).
- Parsers are tested offline from `tests/pages/` (Tequila JSON, Firecrawl
  markdown JSON, Skyscanner HTML).

## Tests

```bash
# What CI runs: the [lake] extra installs the pinned solo-empire-data-lake
# runtime (plus pyarrow/duckdb), so Bronze/Silver lake tests run standalone
python -m pip install -e ".[lake]" pytest ruff
ruff check .
python -m pytest -q -rs

# Contract/policy only: without the extra, lake tests skip with a reason
python -m pip install -e . pytest
python -m pytest -q -rs

# Inside Solo Empire: SOLO_EMPIRE_ROOT (or walking parents) makes the parent
# checkout's infra/scripts/data_lake take precedence over the installed runtime
SOLO_EMPIRE_ROOT=/path/to/solo-empire python -m pytest -q
```

Shared modules: `product_adapter.py`, `product_store.py`. HTML format support in
`data_lake/ingest.py` (`input_format=html`).

## Optional Iceberg REST catalog read pilot

Flight uses the shared `product_store` feature flag. Direct Bronze Parquet
remains the default:

| Variable | Default | Meaning |
|---|---|---|
| `LAKE_READ_MODE` | `parquet` | Direct Bronze read; `iceberg` resolves the registered catalog table and queries it with DuckDB |
| `LAKE_READ_FALLBACK` | `error` | Fail-closed by default; `parquet` is an explicit fallback |
| `ICEBERG_CATALOG_URI` | unset | REST/SQL catalog URI from the runtime secret provider |
| `ICEBERG_WAREHOUSE_URI` | unset | Provider-issued warehouse URI/name |
| `ICEBERG_CATALOG_TOKEN` | unset | Catalog credential; never store it in the repository or API response |

The matching tables must be registered before enabling Iceberg mode:
`bronze.travel_flight_prices` and `bronze.travel_flight_prices_history`.

```bash
npx --yes @infisical/cli@0.43.120 run \
  --projectId=<solo-empire-project-id> --env=dev --path=/cloudflare -- \
  bash -lc 'export SOLO_EMPIRE_DATA_LAKE_URI=s3://<bucket>/<prefix>; \
    export LAKE_READ_MODE=iceberg; export LAKE_READ_FALLBACK=error; \
    PYTHONPATH=src /path/to/solo-empire/.venv/bin/python \
    -m book_flight.api --host 127.0.0.1 --port 8105'
```

Successful `/v1/metadata` reports
`storage_model=lake_first_iceberg_rest_duckdb` and both source kinds as
`iceberg_rest_duckdb`. CSV remains CLI-only and is not a fallback unless the
explicit `LAKE_READ_FALLBACK=parquet` flag is selected.

### Live R2 parity evidence

Verified on 2026-08-08 with bounded prefix
`portfolio-demo/book-flight-iceberg-001`:

| Check | Result |
|---|---|
| Tables | `bronze.travel_flight_prices`, `bronze.travel_flight_prices_history` |
| Providers | `offline_fixtures`, `public_html_experimental` |
| Rows | 2 prices + 2 history |
| Iceberg vs direct Parquet | semantic parity passed for both datasets |
| Per-provider landing | passed; two Bronze files per dataset |
| HTML exact bytes and lineage | exact / passed |
| Registration retry | `already_registered` for all files |
| Snapshot retry | idempotent for all files |
| API storage model | `lake_first_iceberg_rest_duckdb` on port 8105 |
| CSV dependency | none |

The check used deterministic JSON/HTML fixtures and did not contact Kiwi,
Firecrawl, or another flight provider. The old observation timestamp makes
`data_status=stale` expected; the default `LAKE_READ_MODE=parquet` remains
unchanged.
