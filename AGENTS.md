# Agent workflow for flight research

Use the local `./flight` CLI. Do not read, print, log, or paste `.env` or any secret. Do not call Seats.aero when a local cache can answer the request. Never book, transfer points, or make another irreversible action for a user.

## Start with `research`

For a concise request, first turn it into one explicit JSON brief per leg, then use the local CLI:

```bash
cd "$(git rev-parse --show-toplevel)"
./flight research '{"origin":"SFO","destination":"CDG","departure_date":"2026-09-01"}' --json
```

`research` is cache-only by default. It returns `brief`, `assumptions`, `provenance`, and `follow_up_fields`; preserve those fields in the response rather than presenting an inference as a user fact.

### Infer routine details; ask only for blockers

- Retain an explicitly supplied airport. For a named metro plus an explicit preferred airport, make that airport primary and use only clearly requested local alternatives (for example, Washington, DC plus IAD preference can become `IAD,DCA,BWI`, disclosed to the user).
- Resolve a well-known city to its conventional primary airport when that has a low-risk interpretation (for example, Montreal → YUL); disclose it. Ask when the city/metro scope is materially ambiguous.
- For a US-origin request, interpret `M/D` dates as US-style and resolve a missing year to the next occurrence based on the current date. State the resolved year. Ask if the format or intended year remains materially ambiguous.
- Treat “back”, “return”, or equivalent language as a reversed second one-way leg. Search outbound and return legs separately instead of rejecting the trip.
- Default omitted cabin to business for award research (economy for explicit cash-only), passengers to 1, all supported award programs, and connections allowed with known nonstop options preferred. Ask for passenger count only when group/family wording makes a one-person default unsafe. Do not ask up front for programs, stop preference, point cap, or exact nearby-airport preference.
- Ask one consolidated follow-up only for an unresolved route/date, multi-city/open-jaw itinerary, ambiguous group size, invalid hard constraint, or an API fetch outside airport/date/result bounds.

## Cache and award search sequence

1. Run `research` without `--fetch` first for each leg. It checks exact/compatible completed local searches and reports cache age, freshness, result-cap coverage, and flight-detail coverage.
2. For an explicit flight-finding request (such as `/research`), a cache miss, stale result, or **partial** cache authorizes one bounded `--fetch` per leg. For ordinary planning-only questions, ask before consuming the API quota:

   ```bash
   ./flight research '{"origin":"SFO","destination":"CDG","departure_date":"2026-09-01"}' \
     --fetch --max-results 100 --json
   ```

   The MVP permits one bounded Seats.aero cached-search response: no more than three IATA codes on either side, a 14-day departure window, and 100 summaries. It does not use a live award endpoint.
3. Read `award_search.recommendations_by_program`. Preserve the redemption program/source, cache freshness/coverage, summary-vs-flight detail, tax currency, and unknown-seat status. A current availability response suppresses old trip details, so do not revive them from a prior cache. Do not claim a global cross-program “best” option.
4. Fetch `trips` only for a small, user-approved shortlist when flight-level details are needed:

   ```bash
   ./flight trips AVAILABILITY_ID --json
   ```

## Google Flights and cash comparison

`research` emits `cash_search.url` and `query_text` as a manual browser handoff. The CLI does **not** scrape Google Flights, run a browser, call a Google fare API, or receive live Google fares. Open the URL and confirm the visible search manually.

A user-observed cash fare can be imported with `--cash-quote` or `--cash-quote-file`; it must include total/per-passenger scope, ISO currency, route, date, cabin, passenger count, `same_itinerary: true`, `fare_inclusions_match: true`, a timezone-aware `observed_at`, and a valid booking/search URL or itinerary evidence before CPP can be calculated. Any CPP is `user_asserted_comparable`, never independently verified; missing evidence remains `not_comparable`.

The report maps only the **selected transfer sources** against the redemption program, not the operating airline. The bundled Chase/Capital One profiles are optional static conveniences; use a custom `transfer_sources` brief field or `--transfer-profile-file` for any other transferable currency. Treat every transfer entry as a static or user-supplied reference requiring source-side verification. Transfers can be irreversible; verify current ratio, minimum, increment, target program, timing, award space, and final fees first. Never put account numbers, balances, personal identifiers, or credentials in a profile file.

## Interpretation rules

- `seats: 0` or `null` means unknown when a source does not reliably report inventory; it is not “sold out.”
- Prefer `kind: trip` / `detail_level: flight_level` when comparing schedules. Summary rows need confirmation; a fresh availability response intentionally removes stale trip detail.
- Cached award space can be stale or phantom. Confirm availability and final price on the loyalty-program site before transferring points.
- Taxes in different currencies are not interchangeable. CPP is shown only for explicitly comparable manual cash evidence; no currency conversion or booking guarantee is implied.
- Use `./flight stats --json` to monitor local API attempts. Avoid broad pagination and do not invoke unapproved external tools or providers.
