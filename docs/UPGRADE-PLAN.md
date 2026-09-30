# Upgrade plan — book-flight-data

Score: 8/10 -> 8.5/10 — every provider capture shape is parsed from saved fixtures, price alerts fire again, ids are stable, bad fares never reach Bronze and projections are atomic; remaining gaps are packaging polish.

## Backlog

- P0: Confirm the first CI run on GitHub is green (it now downloads the pinned
  `solo-empire-data-lake` tarball); keep it required on `main`.
- P1: When `solo-empire-data-lake` moves, bump the pinned commit in `[lake]` together with
  the other book-*-data repos (same SHA everywhere).
- P2: Add a `[project.optional-dependencies] dev` extra and a `[build-system]` table so
  `pip install -e ".[dev]"` is the single documented setup.
- P1: `run_live_ingest` uses host-local `datetime.now()` for the Tequila date window; use UTC
  (or the origin airport's zone) so the window does not shift with the host.
- P2: `load_previous_prices` keeps the last history row per route; compare against the
  cheapest fare of the previous run instead.

## Done in this pass (pass 4: edge cases)

- `config.env_bool` returned False for anything but a true-ish word, so
  `FREE_ONLY=` (blank line in .env/compose) or a typo silently disabled the
  free-only guard (and with it the paid Firecrawl/experimental HTML gates);
  blank/unrecognised values now keep the safe default.
- `quality.finite_number` raised `OverflowError` for a JSON integer beyond
  float range (a Tequila `price`/`dTime`), crashing `valid_price` and
  `_tequila_time`; it now returns `None`.
- Verified: `tests/test_edge_cases.py` (all 3 fail on the old code); full
  suite 43 passed; ruff 0.15.8 + 0.16.9.
- Bumped the `[lake]` pin `68fb5a9` -> `4c24c66` (NDJSON/BOM/U+2028/double-decode fixes); 43 passed with the new package; ruff 0.15.8 + 0.16.9 clean.

## Done in this pass (pass 3)

- Saved captures in `tests/pages/` drive `tests/test_provider_parsers.py`: Tequila JSON
  (new pure `parse_tequila`), Firecrawl markdown JSON and a Skyscanner HTML page through
  `fetch_skyscanner_html_raw` (script/nav/footer prices ignored).
- Fixed: price alerts read history after this run appended to it (compared each price with
  itself); Tequila airlines joined from a `set` made itinerary ids differ between runs;
  Tequila times used host-local conversion; `parse_skyscanner` crashed on a bare `฿,` and
  missed `฿ 12,450` with a space.
- `valid_price` gates landed fares; `lake.flight_records_from_provider` drops non-finite or
  non-positive fares and blanks bad duration/stops (new `quality` module).
- CLI: `parse_routes` validates IATA pairs; `--days-ahead`/`--alert-drop-pct` checked.
- New `fsutil` module: price CSV replaced atomically, history appended via atomic rewrite.
- README Quick start uses `pip install -e ".[lake]"`; new "Data quality" section.
- Verified: `pytest -q -rs` 37 passed, 0 skipped (was 25) with the `[lake]` venv; ruff
  0.15.8 and 0.16.9 clean.
- Refresh auth: `/v1/refresh` compares the bearer token with `hmac.compare_digest`
  (`_refresh_token_ok`) instead of `==`, which leaked the matching prefix
  length through timing; `tests/test_refresh_token_compare.py` pins it.
- CSV projection stamps (`_projection_timestamp`) are UTC; they were host-local
  but `product_store.parse_ts` reads naive stamps as UTC, so freshness was off
  by the host offset (`tests/test_projection_timestamp_utc.py`).

## Done in pass 2

- Added a `[lake]` extra pinning `solo-empire-data-lake` at `68fb5a9` (plus pyarrow/duckdb); CI
  installs `-e ".[lake]"`, asserts `data_lake` imports, and lake tests now run instead of skipping.
- Test guards use `lake.shared_runtime_available()` (`importlib.util.find_spec("data_lake")`,
  then the `SOLO_EMPIRE_ROOT` / parent-checkout fallback) instead of `find_solo_empire_root()`.
- `[tool.ruff.lint] select = ["E4", "E7", "E9", "F"]` pins the classic rule set: unpinned
  ruff 0.16 widened its defaults and would have failed `ruff check .` in CI.
- `tests/test_fixture_headers.py` pins `fixtures/*.csv` headers to the CSV projection;
  the fixtures had drifted (missing `provider`, `departure_date`, `observed_at`, `currency`) and were refreshed.
- Verified: fresh venv `pip install -e ".[lake]"` from the pinned tarball — 0 skipped of 25 with `[lake]` (was 12 of 24 skipped);
  also against `pip install /home/user/solo-empire-data-lake`, with `SOLO_EMPIRE_ROOT=<parent>`
  (parent adapter wins), and without the extra (lake tests skip with a reason). ruff 0.15 and 0.16 clean.

## Done in pass 1

- `lake.find_solo_empire_root()` now returns `None` when the shared `data_lake`
  adapter cannot be imported (it raised `ModuleNotFoundError` before), and the
  adapter loader also honours `SOLO_EMPIRE_ROOT` so a sibling clone of the parent
  repo works without editing `PYTHONPATH`.
- CI: installs `pytest ruff`, runs `ruff check .` and `python -m pytest -q -rs`
  (skip reasons visible) instead of bare `unittest`.
- Fixed the remaining default-rule ruff findings; `.gitignore` covers egg-info/ruff cache.
- README "Tests" section documents the standalone and full-lake commands.
- Verified: clean venv with `pip install -e . pytest` (CI shape) and a full run with
  `SOLO_EMPIRE_ROOT=<parent>` + pyarrow/duckdb (all lake tests execute and pass).
- The fail-closed ingest test passes an explicit temporary lake URI and runs standalone;
  the stable-id test skips cleanly without the adapter.
