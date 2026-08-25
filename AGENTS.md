# Agent workflow for Flight Finder

Use the local `./flight` CLI for non-transactional flight research. Do not read, print, log, source, or paste `.env` or any secret. Never book, hold a fare, transfer points, sign in to an airline/loyalty account, or make another irreversible action for a user.

## Product boundary

Flight Finder's default research provider is `manual_import`: local normalized award-offer JSON with **no provider account, key, database, or network request**. It is useful for no-login research and should be the first path for agents.

```bash
cd "$(git rev-parse --show-toplevel)"
./flight providers --json
./flight research '{"origin":"SFO","destination":"CDG","departure_date":"2026-09-01"}' --json
```

The report returns `brief`, `assumptions`, `provenance`, `follow_up_fields`, `award_search`, and per-offer evidence. Preserve those fields rather than presenting an inference, user import, cache record, or source claim as a user fact or verified live availability.

`seats.aero` is an optional, explicitly selected BYO compatibility adapter. Do not select it or load its key by default. Its legacy `search`, `results`, and `trips` commands remain available for an authorized local user; they are not the no-login product source. Future hosted results may use only providers licensed for the applicable production, display, attribution, cache, and anonymous-user use.

## Make the request low-friction; ask only for blockers

- Retain an explicitly supplied airport. For a named metro plus an explicit preferred airport, make that airport primary and use only clearly requested local alternatives; disclose the assumption.
- Resolve a well-known city to a conventional primary airport only when low risk; disclose it. Ask when metro scope is materially ambiguous.
- For a US-origin request, interpret `M/D` as US-style and resolve a missing year to the next occurrence; state the resolved year. Ask if format or intended year remains ambiguous.
- Treat return language as a reversed second one-way leg. Research outbound and return separately.
- Default omitted cabin to business for award research (economy for explicit cash-only), passengers to one, all programs represented by the selected source, and connections allowed with known nonstop options preferred. Ask for party size only when group/family wording makes one unsafe.
- Treat every transferable currency or issuer as optional. Do not ask for a transfer source, account balance, or loyalty login up front. Use a public custom transfer profile only when the user asks for transfer math.
- Ask one consolidated follow-up only for unresolved route/date, multi-city/open-jaw itinerary, ambiguous party size, invalid hard constraint, or a selected provider request outside its documented bounds.

## Award-offer imports and ranking

Use `--award-offer` or `--award-offer-file` for a normalized schema-v1 object, list, or `{offers:[...]}`. Prefer inline JSON when practical. File imports are only regular, non-symlink files below project-controlled `imports/` or `examples/` roots, reject `.env`/`.env.*`, and are limited to 1 MB; never point a file flag at an arbitrary local path. Required fields include provider provenance, provider-scoped IDs, redemption program, route/date/cabin, points, fees/tax currency **and per-passenger scope**, availability confidence, detail level, and route-consistent origin/destination/depart/arrival segments for `flight_level`, plus an offset-aware evidence timestamp.

1. Run `research` with the normalized import first. It makes no external request:

   ```bash
   ./flight research '{"origin":"SFO","destination":"CDG","departure_date":"2026-09-01"}' \
     --award-offer-file imports/award-offers.json --json
   ```

2. Read `award_search.recommendations_by_program`. Preserve provider, redemption program, source mode, manual-import/verification state, cache/freshness if supplied by an adapter, tax currency/scope, and summary-versus-flight detail. Manual-import completeness is unknown/unverified even when a file contains matches; surface its evidence age rather than calling coverage complete.
3. Keep provider, redemption program, operating carrier, and marketing carrier separate. Do not merge two providers' offers merely because their redemption-program ID matches.
4. `seats: 0`, `null`, estimated, or unknown inventory means **unknown**, not sold out. Do not promise a seat count that the evidence does not support.
5. Recommendations are ranked only within a provider/program/tax-currency bucket. Do not claim a global cross-provider, cross-program, or cross-currency winner.
6. An imported `provider_live_offer` or cached mode is a source assertion. It remains `imported_manually` and `not_independently_verified`; never call it independently verified or guaranteed live inventory.

If no imported offer is available, report that local no-login research has no award data yet. Offer the manual Google handoff, ask the user to supply a permitted normalized export, or describe the availability of an explicitly authorized provider; never substitute scraping or account login.

## Optional provider fetches

Only an explicitly selected adapter that advertises `supports_fetch` may fetch. For the legacy compatibility path, an explicit flight-finding request and authorization can justify one bounded request:

```bash
./flight research '{"origin":"SFO","destination":"CDG","departure_date":"2026-09-01"}' \
  --provider seats.aero --fetch --max-results 100 --json
```

Do not make live award endpoint calls, broaden a query beyond adapter bounds, or use a provider that has not been selected and authorized. A manual import adapter never fetches or loads credentials. If an optional adapter fails or has incomplete coverage, preserve cached/manual results and report `unavailable` or `partial`; do not silently fall back to another provider.

## Google Flights and cash comparison

`cash_search.url` and `query_text` are a manual Google Flights browser handoff only. Opening the URL sends the displayed trip query to Google. Do **not** scrape Google Flights, automate a browser, call a Google fare API or undocumented endpoint, or claim live Google fares.

A user-observed cash fare can be imported with `--cash-quote` or `--cash-quote-file`. It needs total/per-passenger scope, ISO currency, route, date, cabin, passenger count, `same_itinerary: true`, `fare_inclusions_match: true`, an offset-aware `observed_at`, and a valid booking/search URL or itinerary evidence before CPP can be calculated. Public output strips userinfo, query parameters, and fragments from user-supplied reference URLs. Any CPP is `user_asserted_comparable`, never independently verified; missing evidence or award tax scope remains `not_comparable`.

Transfer-source entries are optional static or user-supplied references keyed to a redemption program, never an operating airline. Do not request or expose account numbers, balances, credentials, or personal travel data. Verify transfer rules, award space, and final fees with the source before an irreversible transfer.
