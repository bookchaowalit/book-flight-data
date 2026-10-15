# Flight provider and budget decision packet

Review date: **2026-10-15**. Status: **draft for owner review; no provider approved**.
This completes a comparison packet, not provider activation or a pause decision.
Current external request allowance is **0** and authorized monthly spend is **0 THB**.
No signup, credentials, provider queries, subscriptions or outreach were performed.

## Recommendation and alternatives

Keep collection blocked and `flight.v1` frozen. Prefer evaluating the **Aviasales
cached Data API** only if its account terms permit this local research/lake use.
Do not substitute its live Search API. If permission or required fields cannot
be established, retain offline fixtures; do not scrape an alternative site.

| Candidate | Free tier / quota evidence as reviewed | Credential/access | Proposed cost ceiling and disposition |
| --- | --- | --- | --- |
| Aviasales/Travelpayouts **Data API** | Cached observations, not live quotes. Public method documentation does not establish a contractual free monthly allowance for this project. `prices_for_dates` has a published ceiling of 600 requests/minute; a rate limit is not a price/license grant. [Data API][data], [rate limits][limits] | Affiliate account token; prefer header transport. No account inspected. | **0 THB/month**; no paid tier/overage. Conditional research candidate, not integrated. Require confirmation of no-charge access and permitted persistence before any call. |
| Travelpayouts **Search API** | User-initiated searches required; automatic collection of results prohibited. [Search rules][search] | Separately granted access/token | **0 THB/month, 0 calls**. Reject for scheduled lake ingestion even if an account calls it free. |
| Skyscanner official Travel API | Public docs do not promise this project a free monthly tier. Key quotas depend on partner agreement; pricing-only use is directed to Data Products. [FAQ][skyfaq], [limits][skylimits] | Partnership application and API key; commercial eligibility restrictions apply. [Access][skyaccess], [eligibility][eligibility] | **0 THB/month, 0 calls**. Not a fit for an unapproved personal data-lake collector; a separate licensed Data Products proposal would need its own quote. |
| Existing Kiwi/Tequila adapter | Current portal terms/free allowance and pricing could not be verified from the public portal in this review. Existing parser fixtures are not access evidence. [Portal][kiwi] | Current code requires a Tequila key; no credential presence checked | **0 THB/month, 0 calls**. Keep blocked until a current provider agreement establishes eligibility, exact quotas and storage rights. |
| Existing Skyscanner HTML / Firecrawl paths | HTML availability or a scraper subscription does not grant underlying fare-data reuse rights | Existing opt-in/paid paths are blocked by policy | **0 THB/month, 0 calls**. Excluded; do not bypass an API restriction with HTML or a scraping service. |

Historical Amadeus Self-Service free-quota documentation was not used as a
current availability/price promise: the official landing page yielded no
readable current offer during this review. It is not an approved fallback.

## Proposed bounded trial, only after a separate approval

These are **local proposed limits**, not provider entitlements, and are not yet
implemented by the collector:

- One provider only: Aviasales **cached Data API**, subject to rights/field review.
- Routes: **BKK–SIN, BKK–KUL, BKK–NRT**, outbound only. Airport-code exactness must
  be verified: reject a city-wide result that silently includes other airports.
- One specified departure day per run: Bangkok local calendar date **+30 days**,
  one-way. No month-wide scans, date grids, return-leg searches or extra routes.
- Proposed method: `/aviasales/v3/prices_for_dates`; `departure_at=YYYY-MM-DD`,
  `one_way=true`, `currency=THB`, `limit=10`, `page=1`, no pagination. The method
  groups one-way results and may yield just one result, not ten bookable offers.
  Passenger/cabin assumptions are **not proven configurable for this method**;
  do not label results as a verified adult/economy fare without evidence.
- One manually started run per week, at most **5 runs per calendar month**.
  Three route queries × five runs = **15 initial requests/month**.
  At most one retry per failed query gives **30 total HTTP attempts/month**;
  count every attempt before dispatch, including failures. Never make up missed runs.
- Single concurrency; at most **1 request/minute** and **6 attempts/run**.
  On 401/403 or a pricing/terms change, stop the trial. On 429, end that run;
  do not consume the retry immediately or switch providers. No redirects,
  background refresh, booking-link crawling, polling or currency-rate requests.
- Monthly budget proposal: **0 THB**, no card/paid plan, no automatic top-up.
  If no-cost access cannot be confirmed, the allowed request budget remains **0**.
  Stop before exceeding the lower of local caps and the account's actual limits.

Budget arithmetic: `3 routes × 5 runs × (1 initial + 1 possible retry) = 30`.
A full request counter and fail-closed cap must be implemented/tested before the
trial; the current CLI route/day options do not enforce this monthly budget.

## Fit to this repository and remaining rights questions

Actual code: `src/book_flight/ingest.py`, `lake.py`, `policy.py`, `config.py`.
The current CLI defaults to **eight** routes and tries existing provider capture
paths; running it is not the proposed trial. Aviasales has no adapter/policy
entry. Do not enable a policy flag and assume the packet is implemented.

The normalized record needs provider, origin/destination, departure date,
price/currency and a stable itinerary identity. It also exposes departure/return,
airline, duration, stops, URL, observed time, and raw-object lineage. Check each
field against an approved sample/schema; preserve unknowns rather than fabricate
itinerary detail. Currency must match the returned value; never store a RUB fare
as `price_thb`. Distinguish fetch time from the older cached quote's age.

For the cached Data API, the provider describes a general seven-day cache and
this date-specific method uses fares found within 48 hours. Those are upstream
cache descriptions, **not permission for our retention**. Sparse Thailand/Asia
route coverage, airport vs city grouping and stale/missing offers require review.
No BKK/SIN/KUL/NRT result availability was tested. [Data API][data]

Before a trial, resolve these questions in a dated provider/account-terms record:

1. Is automated, private route-price research permitted without a booking funnel?
2. May exact responses be retained in landing, normalized into Bronze/history,
   backed up, and later exposed through our read-only API? Is redistribution or
   combining providers allowed? What attribution/deeplink requirements apply?
3. What exact free allowance, charging mechanism and account-specific limits
   apply? Is a no-card/no-overage mode available? Do not infer these from RPM.
4. What retention/deletion/export conditions cover raw payloads, derived rows,
   URLs and backups? Proposed maximum raw/derived trial retention is **7 days or
   a shorter contractual limit**, with no redistribution. This is not an
   implemented TTL or a statement that the provider licenses seven days.
5. Can currency, quote age, route scope and identity be represented honestly
   without changing the frozen `flight.v1` contract? If not, use separate
   versioning review before integration.

No production data currently has a new retention guarantee from this document.
Do not start collection before these rights and deletion controls are resolved.

## Coverage permission alignment and review outcome

The owning coverage policy is in `architecture-docs`:
`systems/ai-data/scraping-data-coverage.yaml`, product `flights`:
`permission.status=blocked`, `quality_status=blocked`, consumer travel-planning,
retention class short-lived-operational, capture bound 192 hours, contract
`flight.v1` on port 8105. The `data-lake-pipelines` copy has the same flight row.
This packet supports review of its next action; it changes neither registry.

The 192-hour monitoring bound is not a vendor cache license or a claimed fresh
capture. No Bronze capture, schedule, public hosting or approval is recorded here.
Owner choices after reviewing this concrete packet:

- Keep blocked and prepare the unresolved Data API terms/schema evidence;
- Authorize a separately bounded zero-cost trial only after prerequisites pass;
- Record a dated pause in the owning registry if no suitable provider is approved.

Only the owner can record that decision. No response or elapsed time is approval.

[data]: https://support.travelpayouts.com/hc/en-us/articles/203956163-Aviasales-Data-API
[limits]: https://support.travelpayouts.com/hc/en-us/articles/4402565416594-API-rate-limits
[search]: https://support.travelpayouts.com/hc/en-us/articles/34788165535250-Search-API-usage-rules
[skyfaq]: https://developers.skyscanner.net/docs/faqs
[skylimits]: https://developers.skyscanner.net/docs/getting-started/rate-limits
[skyaccess]: https://developers.skyscanner.net/docs/getting-started/authentication
[eligibility]: https://www.partners.skyscanner.net/contact/travel-api
[kiwi]: https://tequila.kiwi.com/portal
