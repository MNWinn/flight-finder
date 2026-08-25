# Flight Finder

Flight Finder is a dependency-free Python CLI for explainable award-flight research. Its default workflow is **no-login local research**: supply normalized award offers you are allowed to use, rank them locally, and receive a manual Google Flights browser handoff for cash comparison. It does not book travel, transfer points, scrape sites, automate a browser, or guarantee availability.

- **Runtime:** Python 3.10+ standard library only
- **Default award source:** local normalized `manual_import` (no network, key, or third-party account)
- **Optional compatibility source:** explicit `--provider seats.aero` with an eligible local key
- **Cash comparison:** user-observed imports plus a manual Google Flights handoff only

## Quick start: no-login local research

```bash
cd "$(git rev-parse --show-toplevel)"
./flight providers --json

# Cache-free, DB-free, no-key research; it reports how to add local offers.
./flight research '{"origin":"SFO","destination":"CDG","departure_date":"2026-09-01"}' --json

# Put user-approved JSON in the project-controlled import sandbox, then rank it
# locally. No provider request, key, or SQLite database is used.
mkdir -p imports
./flight research '{"origin":"SFO","destination":"CDG","departure_date":"2026-09-01"}' \
  --award-offer-file imports/award-offers.json --json
```

`research` returns a structured brief, assumptions, provenance, source status, grouped recommendations, optional transfer-source math, and a Google Flights **manual** handoff. A resolved manual-import brief can run without a provider key, account, database, or network request. `./flight init` is only needed for the optional legacy SQLite cache commands.

### Optional Pi `/research` workflow

In a trusted Pi project, `.pi/prompts/research.md` registers `/research <trip request>`. Reload/restart Pi after cloning. The prompt normalizes terse requests and runs the no-login local workflow first; it never invents award results, scrapes sites, or selects a BYO/hosted provider without explicit authorization. Until a licensed provider adapter is added, it needs a permitted normalized award-offer import to return award options.

## Provider boundary

Run `./flight providers --json` for the current capability manifest.

| Source | Default | Network/key | Meaning |
| --- | --- | --- | --- |
| `manual_import` | yes | no | Local normalized award-offer JSON; user-supplied and not independently verified. |
| `seats.aero` | no | only on explicit `--fetch`, with an eligible local key | Legacy BYO compatibility adapter for cached award availability. |
| Google Flights handoff | n/a | no data retrieval | Opens a user-operated browser search only. |
| manual cash import | n/a | no | User-asserted cash observation for guarded CPP math. |

The adapter registry is intentionally small and explicit. A future licensed provider or hosted backend implements the normalized adapter contract; ranking, transfer-source comparisons, and response shapes do not need to change. Flight Finder never silently falls back from an unavailable licensed source to scraping or an unlicensed source.

### Optional legacy Seats.aero path

The legacy `search`, `results`, `trips`, `searches`, and `programs` commands remain available. They are Seats.aero-specific compatibility commands. A `SEATS_AERO_API_KEY` is needed only for direct `search`/`trips` or this explicitly selected research refresh:

```bash
./flight research '{"origin":"SFO","destination":"CDG","departure_date":"2026-09-01"}' \
  --provider seats.aero --fetch --max-results 100 --json
```

That adapter reads its local legacy cache without a request, but it is never the default research provider. Its result is cached provider availability, not live inventory. Legacy records without a validated tax scope remain visible but do not produce CPP. Use it only when your account, key, and intended use are authorized by the provider.

## Normalized award-offer import contract

Pass one object, a JSON list, or `{ "offers": [...] }` with `--award-offer` or `--award-offer-file`. Inline JSON avoids file access. Every `--*-file` JSON option is limited to regular, non-symlink files below the project-controlled `imports/` or `examples/` roots, rejects `.env`/`.env.*`, and has a 1 MB limit. The importer validates and allowlists normalized fields; it does not retain or report an arbitrary raw provider payload. Manual imports are per-command local input in this vertical slice; they are not a claimed live cache.

```json
{
  "schema_version": 1,
  "offer_id": "licensed_demo:offer:opaque-id",
  "provider": {
    "id": "licensed_demo",
    "name": "Licensed Demo Provider"
  },
  "provider_offer_id": "opaque-id",
  "redemption_program": {
    "id": "aeroplan",
    "name": "Air Canada Aeroplan",
    "provider_program_id": "provider-program-id"
  },
  "itinerary": {
    "origin": "SFO",
    "destination": "CDG",
    "departure_date": "2026-09-01",
    "segments": [
      {
        "origin": "SFO",
        "destination": "CDG",
        "departs_at": "2026-09-01T10:00:00Z",
        "arrives_at": "2026-09-02T05:00:00Z",
        "operating_carrier": "UA",
        "marketing_carrier": "UA",
        "flight_number": "UA990"
      }
    ],
    "operating_carriers": ["UA"],
    "marketing_carriers": ["UA"]
  },
  "cabin": "business",
  "award": {"points": 50000, "per_passenger": true},
  "taxes": {"cents": 560, "currency": "USD", "symbol": "$", "per_passenger": true},
  "seat_availability": {"count": 2, "confidence": "reported"},
  "detail_level": "flight_level",
  "booking_links": [{"url": "https://provider.example/book", "label": "Provider link"}],
  "evidence": {
    "availability_mode": "manual_import",
    "source_updated_at": "2026-01-01T11:30:00Z",
    "fetched_at": "2026-01-01T12:00:00Z"
  }
}
```

Validation requires provider provenance, provider-scoped identifiers, a separate redemption program, route/date/cabin/award fields, fees/tax currency **and scope**, seat confidence, detail level, and route-consistent `origin`/`destination`/`departs_at`/`arrives_at` segments for `flight_level`, plus an offset-aware evidence timestamp. `fees` is accepted as an input alias for `taxes`.

Safety rules:

- Provider IDs namespace opaque offer IDs, so two sources cannot collide just because they reuse a raw ID.
- Redemption program, operating carrier, marketing carrier, and provider are separate fields.
- `seat_availability.count: 0` or an unknown/estimated count stays **unknown**; it is not displayed or filtered as sold out.
- Tax/fee amounts are grouped by currency. `taxes.per_passenger` is required for imported offers; CPP is suppressed when a provider cannot establish tax scope. Flight Finder never converts currencies or declares a cross-currency winner.
- Manual imports preserve a stated `cached_availability`, `provider_live_offer`, or licensed mode, but are always marked `imported_manually` and `not_independently_verified`. A source claim is not a Flight Finder verification or availability guarantee. Import completeness is always `unknown_unverified`; the report surfaces the newest supplied evidence time/age rather than claiming coverage.
- Recommendations are grouped by **provider, redemption program, and tax currency**. Ranking is only within that bucket.

Use `--fetch` with the default local-import provider only to receive a no-network explanation; it never loads credentials. A fetching adapter must be selected explicitly.

## Briefs, ranking, and transfer sources

A brief accepts JSON (preferred) or constrained IATA/ISO-date text:

```bash
./flight research 'SFO to CDG 2026-09-01 business 2 passengers' \
  --award-offer-file imports/award-offers.json --json
```

If omitted, cabin defaults to business for award research (economy for explicit `cash_only`), passengers to one, all programs represented by the selected source, and connections are allowed with known nonstop options preferred. These are emitted as assumptions/provenance, not fabricated user facts.

Transfer sources are fully optional. Award research works without a card issuer, loyalty balance, or transfer profile. Add a public static/user-supplied profile only when transfer math or CPP comparison is useful:

```json
{
  "id": "my_rewards_currency",
  "name": "My Rewards Currency",
  "reference_version": "local-profile-v1",
  "as_of": "2026-08-25",
  "source_url": "https://issuer.example/transfer-partners",
  "partners": [
    {
      "program": "aeroplan",
      "recipient_name": "Air Canada Aeroplan",
      "recipient_per_1000_source_points": 1000,
      "minimum_source_points": 1000,
      "source_increment": 1000
    }
  ]
}
```

Supply it in `transfer_sources` within the brief or via `--transfer-profile` / `--transfer-profile-file`. Profiles contain public terms only—never account numbers, balances, traveler data, or credentials. Verify partner eligibility, ratios, timing, award space, and fees before any irreversible transfer.

## Google Flights and manual cash imports

`cash_search.url` is a user-operated Google Flights browser handoff. Opening it sends the displayed route/date/cabin/passenger query to Google. Flight Finder does **not** scrape Google Flights, call undocumented endpoints, automate a browser, call a Google fare API, or receive Google fare data.

A user-observed cash quote can be imported with `--cash-quote` or `--cash-quote-file`. It needs total/per-passenger scope, ISO currency, route/date/cabin/passenger evidence, `same_itinerary: true`, `fare_inclusions_match: true`, an offset-aware `observed_at`, and a booking/search URL or itinerary reference before CPP can be calculated. User-supplied booking/reference URLs are projected without userinfo, query parameters, or fragments in public output. Any CPP is `user_asserted_comparable`, never independently verified.

## Hosted-product boundary

A hosted no-login product must use providers licensed for its production/commercial use, downstream display/deep links, attribution, cache/retention, exports/raw payloads, geography, and anonymous end users. It should disclose source availability, freshness, coverage, rate limits, retention, and provider-specific limitations. When a licensed source cannot answer, report `unavailable`, `partial`, or `not_covered`; do not substitute scraping, airline login, browser automation, or an unlicensed provider.

## Data and safety

- Do not read, print, log, source, or commit `.env` or any secret.
- Local search data and imports can contain sensitive travel information; keep them private.
- Confirm award space and final price with the redemption/booking provider before moving points or booking.
- Flight Finder does not book, ticket, hold fares, transfer points, or guarantee price/availability.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
