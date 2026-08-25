# Flight Finder

A dependency-free Python CLI for researching award flights with the Seats.aero Pro API and a Google Flights manual comparison handoff. It fetches cached availability, stores normalized and raw data in local SQLite, and ranks options by points, taxes, stops, and seat-count confidence.

- **Runtime:** Python 3.10+ standard library only
- **Database:** `data/flights.sqlite3` (created automatically)
- **Secret:** `.env` (ignored by Git and readable only by the local user)
- **API:** Seats.aero cached search and trip-detail endpoints

## Quick start

```bash
cd "$(git rev-parse --show-toplevel)"
./flight init

# Fetch and cache business awards (one API call in the usual case)
./flight search \
  --from SFO,LAX \
  --to LHR,CDG \
  --start 2026-09-01 \
  --end 2026-09-07 \
  --cabin business \
  --seats 2

# Re-rank the latest stored search without making an API call
./flight results --sort best --limit 20

# Machine-readable or report output for an AI/user
./flight results --json
./flight results --markdown
```

The provided Seats.aero key is stored locally in `.env`; the CLI never puts it in command arguments or SQLite. To replace it, edit `.env` or set `SEATS_AERO_API_KEY` in the environment.

## Research: award cache + manual cash comparison

`research` is the AI-facing, cache-first workflow. It accepts a compact JSON brief (preferred for agents) or a deliberately constrained text brief, returns a structured report with field provenance and follow-ups, and **does not make an API call unless `--fetch` is explicit**.

```bash
# JSON brief: useful for an AI/controller
./flight research '{
  "journey": "one_way",
  "legs": [{
    "origin": ["SFO"],
    "destination": ["CDG"],
    "departure": {"start": "2026-09-01", "end": "2026-09-03"}
  }],
  "passengers": {"count": 2},
  "cabin": {"primary": "business"},
  "points": {"programs": ["aeroplan", "flyingblue"], "max_points": 90000},
  "stops": {"preference": "prefer_nonstop_allow_connections"}
}' --json

# Deterministic text form; use IATA codes and ISO dates.
./flight research 'SFO to CDG 2026-09-01 business 2 passengers Aeroplan' --json

# Explicitly refresh a cache miss/stale exact cache with one bounded Seats.aero search.
./flight research '{"origin":"SFO","destination":"CDG","departure_date":"2026-09-01"}' \
  --fetch --max-results 100 --json
```

### Optional Pi `/research` prompt

For a Pi session in this trusted project, `.pi/prompts/research.md` registers `/research <trip request>`. The controller turns a concise request into one explicit JSON brief per leg, transparently resolves routine city/date shorthand, researches return legs separately, and uses a bounded Seats.aero cache fill when needed. It asks only for material ambiguity and never scrapes Google Flights, books, or transfers points. Reload/restart Pi after adding project prompts.

### Brief rules and safe defaults

- The raw CLI deliberately requires origin/destination **IATA codes** and an ISO departure date/range. The Pi `/research` controller handles a concise natural-language request: it can resolve a conventional city airport, keep an explicitly stated airport as the preference, resolve routine US-style dates to an explicit upcoming year, and split a stated return into outbound/return legs. It always discloses those assumptions and asks only when a mapping, date, or party size would materially change the search.
- If omitted, cabin defaults to `business` for award research (`economy` for explicit `cash_only`), passengers to `1`, supported award programs to all available Seats.aero sources, and connections remain allowed. Those defaults are emitted in `brief.assumptions` and `brief.provenance` rather than represented as user facts.
- `--fetch` permits at most three IATA codes on each side, a 14-day departure window, at most 100 summaries, and one logical cached-search response. A fresh exact local cache (24 hours by default; adjust with `--cache-ttl-hours`) is reused only when its stored result cap is not potentially truncated and it included embedded trip details. The report exposes cache coverage; capped or summary-only caches are labeled partial. `/research` treats the user's flight-search request as approval for this one bounded cache fill per leg; direct CLI use still requires explicit `--fetch`.
- Results stay grouped by **redemption program** and tax currency. The report intentionally does not declare a cross-program or cross-currency winner.

### Google Flights handoff and manual cash imports

Every award-first report with a resolved route/date/cabin/passenger brief includes an openable Google Flights browser URL and the visible query text. This is a **manual handoff only**: the CLI does not scrape Google Flights, automate a browser, call a Google fare API, or claim to receive live fares. Confirm the route, date, cabin, passengers, and fare terms in the browser.

To add a fare you observed yourself, import JSON. The CLI labels it as a manual user observation (`manual_verified` or `manual_unverified`) and never promotes an import to a live/API fare; it does not independently verify it. CPP additionally needs a timezone-aware `observed_at` and either a valid `booking_url` or `itinerary_evidence`; any resulting CPP is explicitly labeled **user-asserted**.

```json
{
  "provider": "google_flights",
  "total": 1234.56,
  "currency": "USD",
  "amount_scope": "total",
  "origin": "SFO",
  "destination": "CDG",
  "departure_date": "2026-09-01",
  "cabin": "business",
  "passengers": 1,
  "same_itinerary": true,
  "fare_inclusions_match": true,
  "fare_inclusions": "Comparable baggage and fare rules confirmed",
  "observed_at": "2026-01-01T12:00:00Z",
  "booking_url": "https://www.google.com/travel/flights",
  "itinerary_evidence": "Flight numbers/schedule reviewed in the browser"
}
```

```bash
./flight research '{"origin":"SFO","destination":"CDG","departure_date":"2026-09-01"}' \
  --cash-quote-file cash-quote.json --json
```

CPP is shown only as `user_asserted_comparable` when the imported quote explicitly matches the award route, date, cabin, passenger count, fare inclusions, timestamp, evidence reference, and tax currency. It uses the smallest configured transferable-point amount after its ratio and increment rounding; it never performs an unstated currency conversion or independently validates the fare.

### Configurable transfer sources

Flight Finder is not limited to any card issuer or rewards currency. Award searches work without a transfer profile; add one only when you want transfer math and CPP comparisons. A profile is selected in `transfer_sources` within the brief or supplied with `--transfer-profile` / `--transfer-profile-file`.

Two optional, static convenience profiles are bundled: `chase_ultimate_rewards` and `capital_one_miles`. They are never selected unless the request explicitly names them. Any other transferable currency, loyalty balance, or future provider can use the same public JSON schema:

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
      "source_increment": 1000,
      "source_url": "https://issuer.example/transfer-partners/aeroplan"
    }
  ]
}
```

```bash
./flight research '{"origin":"SFO","destination":"CDG","departure_date":"2026-09-01"}' \
  --transfer-profile-file examples/transfer-profile.example.json --json
```

See [`examples/transfer-profile.example.json`](examples/transfer-profile.example.json). Profiles contain only public transfer terms—never put account numbers, balances, personal identifiers, or credentials in them. Partners, ratios, and timing can change, so verify each rule at its official source before moving points.

## Commands

### Search and store

```bash
./flight search --from JFK --to CDG --start 2026-10-10 --end 2026-10-15 \
  --cabin business,first --seats 2 --direct --max-results 100
```

Useful filters:

- `--programs aeroplan,flyingblue`: Seats.aero mileage-program source names
- `--carriers AF,DL`: operating/marketing carrier filter supported by the API
- `--direct`: request and display known nonstop results
- `--summary-only`: omit embedded flight details for smaller responses
- `--max-results N`: cache at most N availability summaries; over 1,000 may use multiple API calls
- `--json` / `--markdown`: structured output instead of a terminal table

Run `./flight programs` for valid program names. Search is one-way; run separate searches for outbound and return dates.

### Rank the local cache

```bash
./flight searches
./flight results --search-id 3 --cabin business --seats 2 \
  --max-points 80000 --max-stops 1 --programs aeroplan --sort best
```

`results` never calls Seats.aero. Sort choices are `best`, `points`, `taxes`, and `date`. The `best` score combines:

- award points;
- cash taxes converted at `--cpp` (default 1.5 cents per point);
- a 5,000-point penalty per stop;
- small penalties for unknown seat counts or summary-only data.

The score is a ranking aid, not a cash valuation or booking guarantee.

### Fetch exact flight details

A `summary` row has an availability ID in JSON output. Fetch its flights with:

```bash
./flight trips AVAILABILITY_ID
./flight trips AVAILABILITY_ID --json
```

This calls `/partnerapi/trips/{id}`, then stores flight numbers, schedules, aircraft, fare class, stops, seats, taxes, and booking links.

### Inspect storage and API usage

```bash
./flight stats
./flight searches --json
sqlite3 data/flights.sqlite3 '.tables'
```

`stats` counts API attempts made by this CLI today in UTC. Seats.aero states that eligible Pro accounts generally have a 1,000-call daily limit, reset at midnight UTC.

## Optional command from anywhere

Add a shell alias:

```bash
alias flight='/absolute/path/to/flight-finder/flight'
```

Replace the placeholder with your clone location and put it in `~/.zshrc` to keep it across terminal sessions.

## Data and safety notes

- `.env`, local search data, exports, and common credential-file formats are Git-ignored. Never commit account balances, booking details, personal travel history, or API keys.
- A seat count shown as `?` was not reported. Some programs return zero when the count is unknown, so unknown inventory stays visible even with `--seats`.
- Cached award space can be stale or phantom. Confirm on the mileage program's site **before transferring points**.
- Taxes are ranked only when Seats.aero reports them; some programs do not.
- The Seats.aero Pro API is for eligible users' personal, non-commercial use unless Seats.aero gives written commercial permission.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
