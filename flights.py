#!/usr/bin/env python3
"""Dependency-free provider-neutral award-flight research CLI backed by SQLite."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sqlite3
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable

import award_providers
import research as research_core

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_DB = PROJECT_DIR / "data" / "flights.sqlite3"
# File-based imports are intentionally constrained to project-controlled roots so
# an AI/controller cannot be tricked into reading arbitrary local files.
TRUSTED_IMPORT_ROOTS = (PROJECT_DIR / "imports", PROJECT_DIR / "examples")
MAX_IMPORT_FILE_BYTES = 1_000_000
BASE_URL = "https://seats.aero/partnerapi"
CABINS = ("economy", "premium", "business", "first")
CABIN_FIELDS = {
    "economy": "Y",
    "premium": "W",
    "business": "J",
    "first": "F",
}
PROGRAMS = {
    "aeromexico": "Aeromexico Club Premier",
    "aeroplan": "Air Canada Aeroplan",
    "alaska": "Alaska Atmos/Mileage Plan",
    "american": "American AAdvantage",
    "azul": "Azul TudoAzul",
    "connectmiles": "Copa ConnectMiles",
    "delta": "Delta SkyMiles",
    "emirates": "Emirates Skywards",
    "ethiopian": "Ethiopian ShebaMiles",
    "etihad": "Etihad Guest",
    "eurobonus": "SAS EuroBonus",
    "finnair": "Finnair Plus",
    "flyingblue": "Air France/KLM Flying Blue",
    "frontier": "Frontier Airlines",
    "jetblue": "JetBlue TrueBlue",
    "lufthansa": "Lufthansa Miles & More",
    "qantas": "Qantas Frequent Flyer",
    "qatar": "Qatar Privilege Club",
    "saudia": "Saudi AlFursan",
    "singapore": "Singapore KrisFlyer",
    "smiles": "GOL Smiles",
    "spirit": "Spirit Airlines",
    "turkish": "Turkish Miles & Smiles",
    "united": "United MileagePlus",
    "velocity": "Virgin Australia Velocity",
    "virginatlantic": "Virgin Atlantic Flying Club",
}
SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS search_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requested_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    origins TEXT NOT NULL,
    destinations TEXT NOT NULL,
    start_date TEXT,
    end_date TEXT,
    cabins TEXT,
    sources TEXT,
    carriers TEXT,
    direct_only INTEGER NOT NULL DEFAULT 0,
    min_seats INTEGER NOT NULL DEFAULT 1,
    include_trips INTEGER NOT NULL DEFAULT 1,
    requested_limit INTEGER NOT NULL,
    result_count INTEGER NOT NULL DEFAULT 0,
    cursor TEXT,
    error TEXT,
    response_meta_json TEXT
);

CREATE TABLE IF NOT EXISTS api_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requested_at TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    http_status INTEGER,
    search_id INTEGER REFERENCES search_runs(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS availability (
    id TEXT PRIMARY KEY,
    route_id TEXT,
    origin TEXT NOT NULL,
    origin_region TEXT,
    destination TEXT NOT NULL,
    destination_region TEXT,
    departure_date TEXT NOT NULL,
    source TEXT NOT NULL,
    distance INTEGER,
    source_updated_at TEXT,
    fetched_at TEXT NOT NULL,
    trip_details_fetched_at TEXT,
    booking_links_json TEXT,
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_availability_route_date
    ON availability(origin, destination, departure_date);

CREATE TABLE IF NOT EXISTS availability_cabins (
    availability_id TEXT NOT NULL REFERENCES availability(id) ON DELETE CASCADE,
    cabin TEXT NOT NULL,
    available INTEGER NOT NULL,
    points INTEGER,
    remaining_seats INTEGER,
    airlines TEXT,
    direct INTEGER,
    PRIMARY KEY (availability_id, cabin)
);
CREATE INDEX IF NOT EXISTS idx_cabins_rank
    ON availability_cabins(cabin, available, points);

CREATE TABLE IF NOT EXISTS search_results (
    search_id INTEGER NOT NULL REFERENCES search_runs(id) ON DELETE CASCADE,
    availability_id TEXT NOT NULL REFERENCES availability(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY (search_id, availability_id)
);

CREATE TABLE IF NOT EXISTS trips (
    id TEXT PRIMARY KEY,
    availability_id TEXT NOT NULL REFERENCES availability(id) ON DELETE CASCADE,
    cabin TEXT,
    points INTEGER,
    taxes_cents INTEGER,
    taxes_currency TEXT,
    taxes_symbol TEXT,
    alliance_cost INTEGER,
    remaining_seats INTEGER,
    stops INTEGER,
    duration_minutes INTEGER,
    carriers TEXT,
    flight_numbers TEXT,
    departs_at TEXT,
    arrives_at TEXT,
    source TEXT,
    fetched_at TEXT NOT NULL,
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trips_rank
    ON trips(availability_id, cabin, points, stops);

CREATE TABLE IF NOT EXISTS segments (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    segment_order INTEGER NOT NULL,
    flight_number TEXT,
    origin TEXT,
    destination TEXT,
    departs_at TEXT,
    arrives_at TEXT,
    aircraft_name TEXT,
    aircraft_code TEXT,
    fare_class TEXT,
    distance INTEGER,
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_segments_trip ON segments(trip_id, segment_order);
"""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[0] in "\"'" and value[-1:] == value[0]:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def compact_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def int_or_none(value: Any, zero_is_none: bool = False) -> int | None:
    if value is None or value == "":
        return None
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        return None
    return None if zero_is_none and result <= 0 else result


def bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes"}
    return bool(value)


def normalize_csv(value: str | None, *, upper: bool = False) -> str:
    if not value:
        return ""
    items = [part.strip() for part in value.split(",") if part.strip()]
    if upper:
        items = [part.upper() for part in items]
    else:
        items = [part.lower() for part in items]
    return ",".join(dict.fromkeys(items))


def validate_airports(value: str) -> str:
    normalized = normalize_csv(value, upper=True)
    if not normalized or any(not re.fullmatch(r"[A-Z]{3}", code) for code in normalized.split(",")):
        raise argparse.ArgumentTypeError("use comma-separated 3-letter IATA codes (for example SFO,LAX)")
    return normalized


def validate_date(value: str) -> str:
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def validate_cabins(value: str) -> str:
    aliases = {"premium-economy": "premium", "premium_economy": "premium", "pe": "premium"}
    normalized = normalize_csv(value)
    if not normalized:
        return ""
    values = [aliases.get(item, item) for item in normalized.split(",")]
    bad = [item for item in values if item not in CABINS]
    if bad:
        raise argparse.ArgumentTypeError(f"unknown cabin(s): {','.join(bad)}; choose {','.join(CABINS)}")
    return ",".join(dict.fromkeys(values))


def validate_programs(value: str) -> str:
    normalized = normalize_csv(value)
    if not normalized:
        return ""
    bad = [item for item in normalized.split(",") if item not in PROGRAMS]
    if bad:
        raise argparse.ArgumentTypeError(
            f"unknown program source(s): {','.join(bad)}; run './flight programs'"
        )
    return normalized


class FlightError(RuntimeError):
    """User-facing error without a traceback."""


class ApiError(FlightError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._ensure_schema_column("availability", "trip_details_fetched_at", "TEXT")

    def _ensure_schema_column(self, table: str, column: str, definition: str) -> None:
        """Apply a small additive migration while preserving existing local data."""
        columns = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def create_search(self, args: argparse.Namespace) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO search_runs (
                requested_at, origins, destinations, start_date, end_date, cabins,
                sources, carriers, direct_only, min_seats, include_trips, requested_limit
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(), args.origins, args.destinations, args.start_date, args.end_date,
                args.cabins, args.sources or "", args.carriers or "", int(args.direct),
                args.seats, int(not args.summary_only), args.max_results,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def finish_search(
        self,
        search_id: int,
        *,
        status: str,
        result_count: int = 0,
        cursor: Any = None,
        error: str | None = None,
        response_meta: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE search_runs
            SET completed_at=?, status=?, result_count=?, cursor=?, error=?, response_meta_json=?
            WHERE id=?
            """,
            (
                utc_now(), status, result_count, None if cursor is None else str(cursor), error,
                compact_json(response_meta or {}), search_id,
            ),
        )
        self.conn.commit()

    def log_api_request(self, endpoint: str, status: int | None, search_id: int | None = None) -> None:
        self.conn.execute(
            "INSERT INTO api_requests(requested_at, endpoint, http_status, search_id) VALUES (?, ?, ?, ?)",
            (utc_now(), endpoint, status, search_id),
        )
        self.conn.commit()

    def store_search_payload(self, search_id: int, objects: list[dict[str, Any]]) -> int:
        """Store one response; fresh availability always replaces old trip detail."""
        seen: set[str] = set()
        stored = 0
        for ordinal, obj in enumerate(objects):
            availability_id = self.store_availability(obj)
            if not availability_id or availability_id in seen:
                continue
            seen.add(availability_id)
            self.conn.execute(
                "INSERT OR IGNORE INTO search_results(search_id, availability_id, ordinal) VALUES (?, ?, ?)",
                (search_id, availability_id, ordinal),
            )
            stored += 1
        self.conn.commit()
        return stored

    def store_availability(self, obj: dict[str, Any]) -> str | None:
        availability_id = str(obj.get("ID") or "").strip()
        route = obj.get("Route") or {}
        origin = str(route.get("OriginAirport") or obj.get("OriginAirport") or "").upper()
        destination = str(route.get("DestinationAirport") or obj.get("DestinationAirport") or "").upper()
        departure_date = str(obj.get("Date") or obj.get("ParsedDate") or "")[:10]
        source = str(obj.get("Source") or route.get("Source") or "").lower()
        if not availability_id or not origin or not destination or not departure_date:
            return None
        fetched_at = utc_now()
        self.conn.execute(
            """
            INSERT INTO availability (
                id, route_id, origin, origin_region, destination, destination_region,
                departure_date, source, distance, source_updated_at, fetched_at,
                trip_details_fetched_at, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                route_id=excluded.route_id, origin=excluded.origin,
                origin_region=excluded.origin_region, destination=excluded.destination,
                destination_region=excluded.destination_region,
                departure_date=excluded.departure_date, source=excluded.source,
                distance=excluded.distance, source_updated_at=excluded.source_updated_at,
                fetched_at=excluded.fetched_at, trip_details_fetched_at=NULL,
                raw_json=excluded.raw_json
            """,
            (
                availability_id, obj.get("RouteID") or route.get("ID"), origin,
                route.get("OriginRegion"), destination, route.get("DestinationRegion"),
                departure_date, source, int_or_none(route.get("Distance")), obj.get("UpdatedAt"),
                fetched_at, None, compact_json(obj),
            ),
        )
        for cabin, prefix in CABIN_FIELDS.items():
            available = bool_or_none(obj.get(f"{prefix}Available"))
            self.conn.execute(
                """
                INSERT INTO availability_cabins (
                    availability_id, cabin, available, points, remaining_seats, airlines, direct
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(availability_id, cabin) DO UPDATE SET
                    available=excluded.available, points=excluded.points,
                    remaining_seats=excluded.remaining_seats, airlines=excluded.airlines,
                    direct=excluded.direct
                """,
                (
                    availability_id, cabin, int(bool(available)),
                    int_or_none(obj.get(f"{prefix}MileageCost"), zero_is_none=True),
                    int_or_none(obj.get(f"{prefix}RemainingSeats")),
                    obj.get(f"{prefix}Airlines"),
                    None if obj.get(f"{prefix}Direct") is None else int(bool_or_none(obj.get(f"{prefix}Direct"))),
                ),
            )
        embedded_trips = obj.get("AvailabilityTrips")
        # Any new availability response supersedes prior embedded schedule/pricing
        # detail. Keeping it would make a newer summary look flight-level fresh.
        self.conn.execute("DELETE FROM trips WHERE availability_id=?", (availability_id,))
        if isinstance(embedded_trips, list):
            for trip in embedded_trips:
                if isinstance(trip, dict):
                    self.store_trip(availability_id, trip, fetched_at)
            self.conn.execute(
                "UPDATE availability SET trip_details_fetched_at=? WHERE id=?",
                (fetched_at, availability_id),
            )
        return availability_id

    def store_trip(self, availability_id: str, obj: dict[str, Any], fetched_at: str | None = None) -> str | None:
        trip_id = str(obj.get("ID") or "").strip()
        if not trip_id:
            return None
        fetched_at = fetched_at or utc_now()
        self.conn.execute(
            """
            INSERT INTO trips (
                id, availability_id, cabin, points, taxes_cents, taxes_currency, taxes_symbol,
                alliance_cost, remaining_seats, stops, duration_minutes, carriers,
                flight_numbers, departs_at, arrives_at, source, fetched_at, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                availability_id=excluded.availability_id, cabin=excluded.cabin,
                points=excluded.points, taxes_cents=excluded.taxes_cents,
                taxes_currency=excluded.taxes_currency, taxes_symbol=excluded.taxes_symbol,
                alliance_cost=excluded.alliance_cost, remaining_seats=excluded.remaining_seats,
                stops=excluded.stops, duration_minutes=excluded.duration_minutes,
                carriers=excluded.carriers, flight_numbers=excluded.flight_numbers,
                departs_at=excluded.departs_at, arrives_at=excluded.arrives_at,
                source=excluded.source, fetched_at=excluded.fetched_at, raw_json=excluded.raw_json
            """,
            (
                trip_id, availability_id, str(obj.get("Cabin") or "").lower(),
                int_or_none(obj.get("MileageCost"), zero_is_none=True),
                int_or_none(obj.get("TotalTaxes")), obj.get("TaxesCurrency"),
                obj.get("TaxesCurrencySymbol"), int_or_none(obj.get("AllianceCost")),
                int_or_none(obj.get("RemainingSeats")), int_or_none(obj.get("Stops")),
                int_or_none(obj.get("TotalDuration")), obj.get("Carriers"),
                obj.get("FlightNumbers"), obj.get("DepartsAt"), obj.get("ArrivesAt"),
                str(obj.get("Source") or "").lower(), fetched_at, compact_json(obj),
            ),
        )
        self.conn.execute("DELETE FROM segments WHERE trip_id=?", (trip_id,))
        for index, segment in enumerate(obj.get("AvailabilitySegments") or []):
            if not isinstance(segment, dict):
                continue
            segment_id = str(segment.get("ID") or f"{trip_id}:{index}")
            self.conn.execute(
                """
                INSERT INTO segments (
                    id, trip_id, segment_order, flight_number, origin, destination,
                    departs_at, arrives_at, aircraft_name, aircraft_code, fare_class,
                    distance, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    segment_id, trip_id, int_or_none(segment.get("Order")) or index,
                    segment.get("FlightNumber"), segment.get("OriginAirport"),
                    segment.get("DestinationAirport"), segment.get("DepartsAt"),
                    segment.get("ArrivesAt"), segment.get("AircraftName"),
                    segment.get("AircraftCode"), segment.get("FareClass"),
                    int_or_none(segment.get("Distance")), compact_json(segment),
                ),
            )
        return trip_id

    def store_trip_payload(self, availability_id: str, payload: dict[str, Any]) -> int:
        fetched_at = utc_now()
        count = 0
        self.conn.execute("DELETE FROM trips WHERE availability_id=?", (availability_id,))
        for obj in payload.get("data") or []:
            if isinstance(obj, dict) and self.store_trip(availability_id, obj, fetched_at):
                count += 1
        links = payload.get("booking_links") or []
        self.conn.execute(
            """
            UPDATE availability
            SET booking_links_json=?, fetched_at=?, trip_details_fetched_at=?
            WHERE id=?
            """,
            (compact_json(links), fetched_at, fetched_at, availability_id),
        )
        self.conn.commit()
        return count

    def availability_exists(self, availability_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM availability WHERE id=?", (availability_id,)).fetchone()
        return row is not None

    def latest_search_id(self) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM search_runs WHERE status='complete' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return int(row[0]) if row else None

    def search_row(self, search_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM search_runs WHERE id=?", (search_id,)).fetchone()

    def list_searches(self, limit: int) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM search_runs ORDER BY id DESC LIMIT ?", (limit,)))

    @staticmethod
    def _metadata_bool(value: Any) -> bool | None:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in {"true", "false", "1", "0", "yes", "no"}:
            return value.strip().lower() in {"true", "1", "yes"}
        return None

    def search_coverage(self, search_id: int) -> dict[str, Any] | None:
        """Describe result-limit and current trip-detail coverage for one search."""
        row = self.search_row(search_id)
        if row is None:
            return None
        requested_limit = int(row["requested_limit"] or 0)
        stored_result_count = int(self.conn.execute(
            "SELECT COUNT(*) FROM search_results WHERE search_id=?", (search_id,)
        ).fetchone()[0])
        try:
            response_meta = json.loads(row["response_meta_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            response_meta = {}
        if not isinstance(response_meta, dict):
            response_meta = {}
        provider_has_more: bool | None = None
        for key in ("provider_has_more", "has_more", "hasMore", "HasMore"):
            if key in response_meta:
                provider_has_more = self._metadata_bool(response_meta[key])
                if provider_has_more is not None:
                    break
        if provider_has_more is None and row["cursor"]:
            # Legacy rows do not retain page completion metadata. Treat a cursor
            # conservatively rather than claiming the result cap was exhaustive.
            provider_has_more = True
        result_cap_reached = requested_limit > 0 and stored_result_count >= requested_limit
        may_be_truncated = provider_has_more is True or (
            provider_has_more is not False and result_cap_reached
        )
        flight_level_count = int(self.conn.execute(
            """
            SELECT COUNT(DISTINCT sr.availability_id)
            FROM search_results sr
            JOIN availability a ON a.id=sr.availability_id
            WHERE sr.search_id=?
              AND EXISTS (
                  SELECT 1 FROM trips t
                  WHERE t.availability_id=a.id
                    AND a.trip_details_fetched_at IS NOT NULL
                    AND a.trip_details_fetched_at >= a.fetched_at
                    AND t.fetched_at >= a.trip_details_fetched_at
              )
            """,
            (search_id,),
        ).fetchone()[0])
        return {
            "requested_limit": requested_limit,
            "stored_result_count": stored_result_count,
            "provider_has_more": provider_has_more,
            "result_cap_reached": result_cap_reached,
            "may_be_truncated": may_be_truncated,
            "trip_details_requested": bool(row["include_trips"]),
            "flight_level_availability_count": flight_level_count,
            "summary_only_availability_count": max(0, stored_result_count - flight_level_count),
        }

    @staticmethod
    def _cache_row_with_age(
        row: sqlite3.Row, *, max_age_seconds: float | None, now: dt.datetime | None = None
    ) -> dict[str, Any]:
        result = dict(row)
        timestamp = row["completed_at"] or row["requested_at"]
        age_seconds: int | None = None
        try:
            recorded_at = dt.datetime.fromisoformat(timestamp)
            if recorded_at.tzinfo is None:
                recorded_at = recorded_at.replace(tzinfo=dt.timezone.utc)
            reference = now or dt.datetime.now(dt.timezone.utc)
            if reference.tzinfo is None:
                reference = reference.replace(tzinfo=dt.timezone.utc)
            age_seconds = max(0, int((reference - recorded_at).total_seconds()))
        except (TypeError, ValueError):
            pass
        result["cache_age_seconds"] = age_seconds
        result["cache_is_fresh"] = (
            age_seconds is not None and (max_age_seconds is None or age_seconds <= max_age_seconds)
        )
        return result

    def find_matching_search(
        self,
        *,
        origins: str,
        destinations: str,
        start_date: str,
        end_date: str,
        cabins: str = "",
        sources: str = "",
        carriers: str = "",
        direct: bool = False,
        required_result_limit: int | None = None,
        max_age_seconds: float | None = None,
        now: dt.datetime | None = None,
    ) -> dict[str, Any] | None:
        """Return an exact completed search, preferring one with enough result capacity."""
        params: list[Any] = [
            normalize_csv(origins, upper=True), normalize_csv(destinations, upper=True),
            start_date, end_date, normalize_csv(cabins), normalize_csv(sources),
            normalize_csv(carriers, upper=True), int(direct),
        ]
        order_clause = "ORDER BY id DESC"
        if required_result_limit is not None:
            order_clause = "ORDER BY CASE WHEN requested_limit >= ? THEN 0 ELSE 1 END, id DESC"
            params.append(required_result_limit)
        row = self.conn.execute(
            f"""
            SELECT * FROM search_runs
            WHERE status='complete' AND origins=? AND destinations=?
              AND start_date=? AND end_date=? AND cabins=? AND sources=?
              AND carriers=? AND direct_only=?
            {order_clause} LIMIT 1
            """,
            params,
        ).fetchone()
        if row is None:
            return None
        result = self._cache_row_with_age(row, max_age_seconds=max_age_seconds, now=now)
        if max_age_seconds is not None and not result["cache_is_fresh"]:
            return None
        result["cache_match"] = "exact"
        return result

    def find_compatible_search(
        self,
        *,
        origins: str,
        destinations: str,
        start_date: str,
        end_date: str,
        cabins: str = "",
        sources: str = "",
        carriers: str = "",
        direct: bool = False,
        required_result_limit: int | None = None,
        max_age_seconds: float | None = None,
        now: dt.datetime | None = None,
    ) -> dict[str, Any] | None:
        """Find a broader local search that safely covers a narrower research brief."""
        desired_cabins = set(normalize_csv(cabins).split(",")) if cabins else set()
        desired_sources = set(normalize_csv(sources).split(",")) if sources else set()
        desired_carriers = set(normalize_csv(carriers, upper=True).split(",")) if carriers else set()
        params: list[Any] = [
            normalize_csv(origins, upper=True), normalize_csv(destinations, upper=True),
            start_date, end_date,
        ]
        order_clause = "ORDER BY id DESC"
        if required_result_limit is not None:
            order_clause = "ORDER BY CASE WHEN requested_limit >= ? THEN 0 ELSE 1 END, id DESC"
            params.append(required_result_limit)
        rows = self.conn.execute(
            f"""
            SELECT * FROM search_runs
            WHERE status='complete' AND origins=? AND destinations=?
              AND start_date=? AND end_date=?
            {order_clause}
            """,
            params,
        ).fetchall()
        for row in rows:
            stored_cabins = set(normalize_csv(row["cabins"]).split(",")) if row["cabins"] else set()
            stored_sources = set(normalize_csv(row["sources"]).split(",")) if row["sources"] else set()
            stored_carriers = set(normalize_csv(row["carriers"], upper=True).split(",")) if row["carriers"] else set()
            # Empty stored filters are provider-wide; a narrower stored filter cannot
            # answer an unfiltered brief without silently hiding alternatives.
            if stored_cabins and not desired_cabins.issubset(stored_cabins):
                continue
            if desired_sources:
                if stored_sources and not desired_sources.issubset(stored_sources):
                    continue
            elif stored_sources:
                continue
            if desired_carriers:
                if stored_carriers and not desired_carriers.issubset(stored_carriers):
                    continue
            elif stored_carriers:
                continue
            if direct and not bool(row["direct_only"]):
                continue
            if not direct and bool(row["direct_only"]):
                continue
            result = self._cache_row_with_age(row, max_age_seconds=max_age_seconds, now=now)
            if max_age_seconds is not None and not result["cache_is_fresh"]:
                continue
            result["cache_match"] = "compatible"
            return result
        return None

    def stats(self) -> dict[str, Any]:
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        counts = {}
        for table in ("search_runs", "availability", "trips", "segments"):
            counts[table] = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        counts["api_calls_today_utc"] = self.conn.execute(
            "SELECT COUNT(*) FROM api_requests WHERE substr(requested_at,1,10)=?", (today,)
        ).fetchone()[0]
        counts["database"] = str(self.path)
        return counts

    @staticmethod
    def _trip_is_current(
        trip_fetched_at: Any,
        availability_fetched_at: Any,
        trip_details_fetched_at: Any,
    ) -> bool:
        """Only expose detail tied to the current availability generation."""
        if not trip_fetched_at or not availability_fetched_at or not trip_details_fetched_at:
            return False
        try:
            trip_time = dt.datetime.fromisoformat(str(trip_fetched_at))
            availability_time = dt.datetime.fromisoformat(str(availability_fetched_at))
            detail_time = dt.datetime.fromisoformat(str(trip_details_fetched_at))
            if trip_time.tzinfo is None:
                trip_time = trip_time.replace(tzinfo=dt.timezone.utc)
            if availability_time.tzinfo is None:
                availability_time = availability_time.replace(tzinfo=dt.timezone.utc)
            if detail_time.tzinfo is None:
                detail_time = detail_time.replace(tzinfo=dt.timezone.utc)
        except (TypeError, ValueError):
            return False
        return detail_time >= availability_time and trip_time >= detail_time

    def candidates(
        self,
        *,
        search_id: int | None = None,
        availability_id: str | None = None,
    ) -> list[dict[str, Any]]:
        filters: list[str] = []
        params: list[Any] = []
        if search_id is not None:
            filters.append(
                "EXISTS (SELECT 1 FROM search_results sr WHERE sr.availability_id=a.id AND sr.search_id=?)"
            )
            params.append(search_id)
        if availability_id:
            filters.append("a.id=?")
            params.append(availability_id)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        trip_rows = self.conn.execute(
            f"""
            SELECT t.*, a.origin, a.destination, a.departure_date, a.source AS program,
                   a.source_updated_at, a.fetched_at AS availability_fetched_at,
                   a.trip_details_fetched_at, a.booking_links_json
            FROM trips t JOIN availability a ON a.id=t.availability_id
            {where}
            """,
            params,
        ).fetchall()
        candidates: list[dict[str, Any]] = []
        exact_pairs: set[tuple[str, str]] = set()
        for row in trip_rows:
            if not self._trip_is_current(
                row["fetched_at"], row["availability_fetched_at"], row["trip_details_fetched_at"]
            ):
                continue
            exact_pairs.add((row["availability_id"], row["cabin"] or ""))
            segments = [
                dict(segment)
                for segment in self.conn.execute(
                    """
                    SELECT segment_order, flight_number, origin, destination, departs_at,
                           arrives_at, aircraft_name, aircraft_code, fare_class, distance
                    FROM segments WHERE trip_id=? ORDER BY segment_order
                    """,
                    (row["id"],),
                )
            ]
            candidates.append(
                {
                    "kind": "trip",
                    "id": row["id"],
                    "availability_id": row["availability_id"],
                    "date": row["departure_date"],
                    "origin": row["origin"],
                    "destination": row["destination"],
                    "cabin": row["cabin"],
                    "points": row["points"],
                    "taxes_cents": row["taxes_cents"],
                    "taxes_currency": row["taxes_currency"],
                    "taxes_symbol": row["taxes_symbol"],
                    "seats": row["remaining_seats"],
                    "stops": row["stops"],
                    "direct": row["stops"] == 0 if row["stops"] is not None else None,
                    "duration_minutes": row["duration_minutes"],
                    "program": row["program"],
                    "carriers": row["carriers"],
                    "flight_numbers": row["flight_numbers"],
                    "departs_at": row["departs_at"],
                    "arrives_at": row["arrives_at"],
                    "segments": segments,
                    "booking_links": json.loads(row["booking_links_json"] or "[]"),
                    "source_updated_at": row["source_updated_at"],
                    "fetched_at": row["fetched_at"] or row["availability_fetched_at"],
                }
            )
        summary_rows = self.conn.execute(
            f"""
            SELECT c.*, a.origin, a.destination, a.departure_date, a.source AS program,
                   a.source_updated_at, a.fetched_at AS availability_fetched_at, a.booking_links_json
            FROM availability_cabins c JOIN availability a ON a.id=c.availability_id
            {where}
            """,
            params,
        ).fetchall()
        for row in summary_rows:
            pair = (row["availability_id"], row["cabin"])
            if not row["available"] or pair in exact_pairs:
                continue
            candidates.append(
                {
                    "kind": "summary",
                    "id": row["availability_id"],
                    "availability_id": row["availability_id"],
                    "date": row["departure_date"],
                    "origin": row["origin"],
                    "destination": row["destination"],
                    "cabin": row["cabin"],
                    "points": row["points"],
                    "taxes_cents": None,
                    "taxes_currency": None,
                    "taxes_symbol": None,
                    "seats": row["remaining_seats"],
                    "stops": 0 if row["direct"] else None,
                    "direct": None if row["direct"] is None else bool(row["direct"]),
                    "duration_minutes": None,
                    "program": row["program"],
                    "carriers": row["airlines"],
                    "flight_numbers": None,
                    "departs_at": None,
                    "arrives_at": None,
                    "segments": [],
                    "booking_links": json.loads(row["booking_links_json"] or "[]"),
                    "source_updated_at": row["source_updated_at"],
                    "fetched_at": row["availability_fetched_at"],
                }
            )
        return candidates


class SeatsAeroClient:
    def __init__(
        self,
        api_key: str,
        on_attempt: Callable[[str, int | None], None] | None = None,
        timeout: int = 30,
    ):
        if not api_key:
            raise FlightError(
                "SEATS_AERO_API_KEY is missing. Put it in .env (see .env.example)."
            )
        self.api_key = api_key
        self.on_attempt = on_attempt
        self.timeout = timeout

    def get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        cleaned: dict[str, str] = {}
        for key, value in (params or {}).items():
            if value is None or value == "":
                continue
            if isinstance(value, bool):
                cleaned[key] = str(value).lower()
            else:
                cleaned[key] = str(value)
        query = urllib.parse.urlencode(cleaned)
        url = f"{BASE_URL}/{endpoint.lstrip('/')}"
        if query:
            url += "?" + query
        request = urllib.request.Request(
            url,
            headers={
                "Partner-Authorization": self.api_key,
                "Accept": "application/json",
                "User-Agent": "points-flight-cli/0.1",
            },
        )
        attempts = 3
        for attempt in range(attempts):
            status: int | None = None
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    status = response.status
                    body = response.read()
                if self.on_attempt:
                    self.on_attempt(endpoint, status)
                payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ApiError("Seats.aero returned an unexpected JSON response", status)
                return payload
            except urllib.error.HTTPError as exc:
                status = exc.code
                body = exc.read(1000).decode("utf-8", errors="replace").strip()
                if self.on_attempt:
                    self.on_attempt(endpoint, status)
                if status in {429, 500, 502, 503, 504} and attempt + 1 < attempts:
                    try:
                        retry_after = int(exc.headers.get("Retry-After", "0") or 0)
                    except ValueError:
                        retry_after = 0
                    time.sleep(min(max(retry_after, 1 + attempt * 2), 30))
                    continue
                hint = " Check the key and Seats.aero Pro API access." if status in {401, 403} else ""
                raise ApiError(f"Seats.aero HTTP {status}: {body[:500] or exc.reason}.{hint}", status) from exc
            except urllib.error.URLError as exc:
                if self.on_attempt:
                    self.on_attempt(endpoint, status)
                if attempt + 1 < attempts:
                    time.sleep(1 + attempt * 2)
                    continue
                raise ApiError(f"Could not reach Seats.aero: {exc.reason}") from exc
            except json.JSONDecodeError as exc:
                raise ApiError("Seats.aero returned invalid JSON", status) from exc
        raise ApiError("Seats.aero request failed")


def fetch_search(db: Database, args: argparse.Namespace) -> tuple[int, int, list[dict[str, Any]]]:
    if args.start_date and args.end_date and args.start_date > args.end_date:
        raise FlightError("--start cannot be after --end")
    search_id = db.create_search(args)
    base_params = {
        "origin_airport": args.origins,
        "destination_airport": args.destinations,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "cabins": args.cabins,
        "sources": args.sources,
        "carriers": args.carriers,
        "only_direct_flights": args.direct,
        "include_trips": not args.summary_only,
        "minify_trips": False if not args.summary_only else None,
        "order_by": "lowest_mileage",
    }
    objects: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor: Any = None
    skip = 0
    last_meta: dict[str, Any] = {}
    try:
        client = SeatsAeroClient(
            os.environ.get("SEATS_AERO_API_KEY", ""),
            on_attempt=lambda endpoint, status: db.log_api_request(endpoint, status, search_id),
            timeout=args.timeout,
        )
        while len(objects) < args.max_results:
            remaining = args.max_results - len(objects)
            take = max(10, min(1000, remaining))
            params = dict(base_params)
            params.update({"take": take, "skip": skip or None, "cursor": cursor if skip else None})
            payload = client.get("search", params)
            data = payload.get("data") or []
            if not isinstance(data, list):
                raise ApiError("Seats.aero response did not contain a data array")
            if cursor is None:
                cursor = payload.get("cursor")
            last_meta = {key: value for key, value in payload.items() if key != "data"}
            provider_has_more: bool | None = None
            for key in ("has_more", "hasMore", "HasMore"):
                if key in payload:
                    provider_has_more = Database._metadata_bool(payload[key])
                    if provider_has_more is not None:
                        break
            if provider_has_more is None:
                if len(data) < take:
                    provider_has_more = False
                elif payload.get("cursor"):
                    provider_has_more = True
                # A full page without an explicit continuation signal is unknown,
                # not proof that no additional availability exists.
            last_meta["provider_has_more"] = provider_has_more
            for obj in data:
                if not isinstance(obj, dict):
                    continue
                object_id = str(obj.get("ID") or "")
                if object_id and object_id in seen:
                    continue
                if object_id:
                    seen.add(object_id)
                objects.append(obj)
                if len(objects) >= args.max_results:
                    break
            skip += len(data)
            last_meta["local_result_limit_reached"] = len(objects) >= args.max_results
            last_meta["local_single_page"] = bool(getattr(args, "single_page", False))
            if getattr(args, "single_page", False) or len(data) < take or not data or cursor is None:
                break
        count = db.store_search_payload(search_id, objects)
        db.finish_search(
            search_id, status="complete", result_count=count, cursor=cursor, response_meta=last_meta
        )
        return search_id, count, objects
    except Exception as exc:
        db.finish_search(search_id, status="failed", error=str(exc))
        raise


def seat_is_known(candidate: dict[str, Any]) -> bool:
    return candidate.get("seats") is not None and int(candidate["seats"]) > 0


def rank_candidates(
    candidates: Iterable[dict[str, Any]],
    *,
    cabins: str = "",
    seats: int = 1,
    direct: bool = False,
    max_stops: int | None = None,
    max_points: int | None = None,
    programs: str = "",
    cents_per_point: float = 1.5,
    sort: str = "best",
) -> list[dict[str, Any]]:
    cabin_set = set(normalize_csv(cabins).split(",")) if cabins else set()
    program_set = set(normalize_csv(programs).split(",")) if programs else set()
    result: list[dict[str, Any]] = []
    for original in candidates:
        item = dict(original)
        if cabin_set and item.get("cabin") not in cabin_set:
            continue
        if program_set and item.get("program") not in program_set:
            continue
        if direct and item.get("direct") is not True:
            continue
        if max_stops is not None:
            stops = item.get("stops")
            if stops is None or stops > max_stops:
                continue
        if seat_is_known(item) and item["seats"] < seats:
            continue
        points = item.get("points")
        if max_points is not None and (points is None or points > max_points):
            continue
        score = float(points) if points is not None else 10**12
        taxes = item.get("taxes_cents")
        score += (taxes / cents_per_point) if taxes is not None and cents_per_point > 0 else 1000
        score += (item.get("stops") or 0) * 5000
        if not seat_is_known(item):
            score += 1000
        if item.get("kind") == "summary":
            score += 1500
        item["score"] = round(score, 1)
        result.append(item)

    if sort == "date":
        key = lambda x: (x.get("date") or "9999", x.get("points") or 10**12, x["score"])
    elif sort == "points":
        key = lambda x: (x.get("points") or 10**12, x.get("taxes_cents") or 0, x.get("date") or "")
    elif sort == "taxes":
        key = lambda x: (x.get("taxes_cents") if x.get("taxes_cents") is not None else 10**12, x.get("points") or 10**12)
    else:
        key = lambda x: (x["score"], x.get("date") or "", x.get("points") or 10**12)
    return sorted(result, key=key)


def format_points(value: int | None) -> str:
    return "?" if value is None else f"{value:,}"


def format_taxes(item: dict[str, Any]) -> str:
    cents = item.get("taxes_cents")
    if cents is None:
        return "?"
    symbol = item.get("taxes_symbol") or ""
    currency = item.get("taxes_currency") or ""
    prefix = symbol or (currency + " " if currency else "")
    return f"{prefix}{cents / 100:,.2f}"


def table_text(candidates: list[dict[str, Any]], limit: int) -> str:
    rows = []
    headers = ["#", "Date", "Route", "Cabin", "Points", "Taxes", "Seats", "Stops", "Program", "Flights", "Type"]
    for index, item in enumerate(candidates[:limit], 1):
        seats = str(item["seats"]) if seat_is_known(item) else "?"
        stops = str(item["stops"]) if item.get("stops") is not None else ("0*" if item.get("direct") else "?")
        rows.append(
            [
                str(index), item.get("date") or "?",
                f"{item.get('origin','?')}-{item.get('destination','?')}",
                item.get("cabin") or "?", format_points(item.get("points")),
                format_taxes(item), seats, stops, item.get("program") or "?",
                item.get("flight_numbers") or item.get("carriers") or "?",
                "exact" if item.get("kind") == "trip" else "summary",
            ]
        )
    if not rows:
        return "No matching locally stored results."
    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))]
    render = lambda row: "  ".join(value.ljust(widths[i]) for i, value in enumerate(row))
    return "\n".join([render(headers), render(["-" * width for width in widths]), *(render(row) for row in rows)])


def markdown_text(candidates: list[dict[str, Any]], limit: int) -> str:
    headers = ["Rank", "Date", "Route", "Cabin", "Points", "Taxes", "Seats", "Stops", "Program", "Flights", "Confidence"]
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for index, item in enumerate(candidates[:limit], 1):
        lines.append(
            "| " + " | ".join(
                [
                    str(index), item.get("date") or "?",
                    f"{item.get('origin','?')}-{item.get('destination','?')}",
                    item.get("cabin") or "?", format_points(item.get("points")),
                    format_taxes(item), str(item["seats"]) if seat_is_known(item) else "unknown",
                    str(item["stops"]) if item.get("stops") is not None else "unknown",
                    item.get("program") or "?", item.get("flight_numbers") or item.get("carriers") or "?",
                    "flight-level" if item.get("kind") == "trip" else "summary only",
                ]
            ) + " |"
        )
    return "\n".join(lines) if len(lines) > 2 else "No matching locally stored results."


def output_candidates(candidates: list[dict[str, Any]], args: argparse.Namespace, context: dict[str, Any] | None = None) -> None:
    limit = args.limit
    if getattr(args, "json", False):
        print(json.dumps({**(context or {}), "count": len(candidates), "results": candidates[:limit]}, indent=2))
    elif getattr(args, "markdown", False):
        print(markdown_text(candidates, limit))
        print("\nExact flight data is still cached and should be verified with the mileage program before transferring points.")
    else:
        print(table_text(candidates, limit))
        print(f"\nShowing {min(limit, len(candidates))} of {len(candidates)} ranked options. ? = not reported.")
        summaries = [
            (index, item)
            for index, item in enumerate(candidates[:limit], 1)
            if item.get("kind") == "summary"
        ]
        if summaries:
            print("Summary-only rows can be expanded with:")
            for index, item in summaries:
                print(f"  #{index}: ./flight trips {item['availability_id']}")


def _reject_environment_file(path: Path, label: str) -> None:
    """Never treat an environment file (including a symlink) as research input."""

    candidates = [path]
    try:
        candidates.append(path.resolve())
    except OSError:
        pass
    if any(candidate.name == ".env" or candidate.name.startswith(".env.") for candidate in candidates):
        raise FlightError(f"{label} must not be an environment file")


def _trusted_import_path(path: Path, label: str) -> Path:
    """Return a regular, non-symlink file below a project-controlled import root."""

    raw = Path(path)
    if any(part == ".." for part in raw.parts):
        raise FlightError(f"{label} must not contain parent-directory traversal")
    candidate = raw if raw.is_absolute() else PROJECT_DIR / raw
    candidate = candidate.absolute()
    for root in TRUSTED_IMPORT_ROOTS:
        root_path = Path(root).absolute()
        try:
            relative = candidate.relative_to(root_path)
        except ValueError:
            continue
        if not relative.parts:
            raise FlightError(f"{label} must name a regular JSON file below a trusted import root")
        if root_path.is_symlink():
            raise FlightError(f"trusted import root for {label} must not be a symlink")
        component = root_path
        try:
            for part in relative.parts:
                component = component / part
                if component.is_symlink():
                    raise FlightError(f"{label} must not be a symlink or pass through a symlink")
            resolved_root = root_path.resolve(strict=True)
            resolved = candidate.resolve(strict=True)
            try:
                resolved.relative_to(resolved_root)
            except ValueError as exc:
                raise FlightError(f"{label} must stay below a trusted import root") from exc
            metadata = os.lstat(resolved)
        except FlightError:
            raise
        except OSError as exc:
            raise FlightError(f"could not read {label}: {exc}") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise FlightError(f"{label} must be a regular file")
        if metadata.st_size > MAX_IMPORT_FILE_BYTES:
            raise FlightError(f"{label} exceeds the {MAX_IMPORT_FILE_BYTES}-byte import limit")
        _reject_environment_file(resolved, label)
        return resolved
    roots = ", ".join(str(Path(root)) for root in TRUSTED_IMPORT_ROOTS)
    raise FlightError(f"{label} must be inside a trusted import root ({roots})")


def _read_json_file(path: Path, label: str) -> Any:
    safe_path = _trusted_import_path(path, label)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(safe_path, flags)
        opened_metadata = os.fstat(descriptor)
        if not stat.S_ISREG(opened_metadata.st_mode):
            raise FlightError(f"{label} must be a regular file")
        if opened_metadata.st_size > MAX_IMPORT_FILE_BYTES:
            raise FlightError(f"{label} exceeds the {MAX_IMPORT_FILE_BYTES}-byte import limit")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = None
            return json.load(handle)
    except FlightError:
        raise
    except OSError as exc:
        raise FlightError(f"could not read {label}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise FlightError(f"{label} must contain valid JSON") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _research_brief_input(args: argparse.Namespace) -> str | dict[str, Any]:
    positional_brief = getattr(args, "brief", None)
    option_brief = getattr(args, "brief_option", None)
    brief_file = getattr(args, "brief_file", None)
    inline_brief = option_brief or positional_brief
    if sum(bool(value) for value in (positional_brief, option_brief, brief_file)) > 1:
        raise FlightError("use one positional trip brief, --brief, or --brief-file")
    if brief_file:
        payload = _read_json_file(brief_file, "brief file")
        if not isinstance(payload, dict):
            raise FlightError("brief file must contain one JSON object")
        return payload
    if inline_brief:
        return inline_brief
    raise FlightError("provide a trip brief, --brief, or --brief-file")


def _cash_quote_inputs(args: argparse.Namespace) -> tuple[list[Any], list[dict[str, str]]]:
    payloads: list[Any] = []
    issues: list[dict[str, str]] = []
    for raw in getattr(args, "cash_quote", []):
        try:
            payloads.append(json.loads(raw))
        except json.JSONDecodeError:
            issues.append({
                "field": "cash_quote",
                "reason": "--cash-quote must be valid JSON; use --cash-quote-file for a JSON file.",
            })
    for path in getattr(args, "cash_quote_file", []):
        try:
            payloads.append(_read_json_file(path, "cash quote file"))
        except FlightError as exc:
            issues.append({"field": "cash_quote", "reason": str(exc)})
    return payloads, issues


def _transfer_profile_inputs(args: argparse.Namespace) -> tuple[list[Any], list[dict[str, str]]]:
    """Read optional public transfer-rule configuration, never credentials or balances."""

    payloads: list[Any] = []
    issues: list[dict[str, str]] = []
    for raw in getattr(args, "transfer_profile", []):
        try:
            payloads.append(json.loads(raw))
        except json.JSONDecodeError:
            issues.append({
                "field": "transfer_sources",
                "reason": "--transfer-profile must be valid JSON; use --transfer-profile-file for a JSON file.",
            })
    for path in getattr(args, "transfer_profile_file", []):
        try:
            payloads.append(_read_json_file(path, "transfer profile file"))
        except FlightError as exc:
            issues.append({"field": "transfer_sources", "reason": str(exc)})
    return payloads, issues


def _research_fetch_args(brief: research_core.ResearchBrief, args: argparse.Namespace) -> argparse.Namespace:
    """Compatibility wrapper for the explicit legacy Seats.aero adapter."""

    request = award_providers.AwardSearchRequest(
        origins=brief.origins,
        destinations=brief.destinations,
        start_date=brief.start_date or "",
        end_date=brief.end_date or "",
        cabin=brief.cabin,
        passengers=brief.passengers,
        program_ids=brief.programs,
        direct_only=brief.direct_only,
        max_points=brief.max_points,
        result_limit=args.max_results,
        cache_ttl_hours=args.cache_ttl_hours,
    )
    return _seats_fetch_args(request, args)


def _seats_fetch_args(
    request: award_providers.AwardSearchRequest, args: argparse.Namespace
) -> argparse.Namespace:
    """Adapt a provider-neutral request to the legacy optional compatibility API."""

    return argparse.Namespace(
        origins=",".join(request.origins),
        destinations=",".join(request.destinations),
        start_date=request.start_date,
        end_date=request.end_date,
        cabins=request.cabin,
        sources=",".join(request.program_ids),
        carriers="",
        direct=request.direct_only,
        seats=request.passengers,
        summary_only=False,
        max_results=request.result_limit,
        timeout=args.timeout,
        single_page=True,
    )


def _research_request_summary(
    brief: research_core.ResearchBrief,
    args: argparse.Namespace,
    provider: award_providers.AwardProvider,
) -> dict[str, Any]:
    """Describe a selected adapter without claiming an unperformed live search."""

    unresolved = lambda field: brief.provenance.get(field) == "unresolved"
    limits = provider.limits
    endpoint = (
        "local normalized award-offer import"
        if not limits.network_access
        else "adapter-managed provider search"
    )
    return {
        "provider": provider.id,
        "provider_name": provider.display_name,
        "endpoint": endpoint,
        "origins": list(brief.origins),
        "destinations": list(brief.destinations),
        "start_date": brief.start_date,
        "end_date": brief.end_date,
        "cabin": None if unresolved("cabin") else brief.cabin,
        "passengers": None if unresolved("passengers") else brief.passengers,
        "programs": "unresolved" if unresolved("programs") else (
            list(brief.programs) if brief.programs else "all_supported_by_selected_source"
        ),
        "direct_only": None if unresolved("stops") else brief.direct_only,
        "max_results": args.max_results,
        "one_logical_request": bool(limits.supports_fetch),
        "live_award_search": False,
        "provider_capabilities": {
            "supports_fetch": limits.supports_fetch,
            "requires_credentials": limits.requires_credentials,
            "network_access": limits.network_access,
            "requires_explicit_selection": limits.requires_explicit_selection,
            "max_airports_per_side": limits.max_airports_per_side,
            "max_date_span_days": limits.max_date_span_days,
            "max_results": limits.max_results,
            "strict_program_catalog": limits.strict_program_catalog,
        },
    }


def _cache_summary(
    cache: dict[str, Any] | None,
    ttl_hours: float,
    coverage: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if cache is None:
        return None
    age_seconds = cache.get("cache_age_seconds")
    fresh = age_seconds is not None and age_seconds <= ttl_hours * 3600
    return {
        "search_id": cache["id"],
        "match": cache.get("cache_match", "exact"),
        "requested_at": cache.get("requested_at"),
        "completed_at": cache.get("completed_at"),
        "age_seconds": age_seconds,
        "ttl_hours": ttl_hours,
        "fresh": fresh,
        "result_count": cache.get("result_count"),
        "coverage": coverage if coverage is not None else cache.get("coverage"),
    }


def _unique_research_issues(issues: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    """Keep a concise, stable set of action fields in the AI-facing report."""

    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for issue in issues:
        field = str(issue.get("field") or "brief")
        reason = str(issue.get("reason") or "")
        key = (field, reason)
        if key not in seen:
            seen.add(key)
            result.append({"field": field, "reason": reason})
    return result


def _research_ranked_candidates(
    db: Database, search_id: int, brief: research_core.ResearchBrief
) -> list[dict[str, Any]]:
    """Legacy helper retained for callers of the direct Seats.aero cache path."""

    return rank_candidates(
        db.candidates(search_id=search_id),
        cabins=brief.cabin,
        seats=brief.passengers,
        direct=brief.direct_only,
        max_points=brief.max_points,
        programs=brief.sources,
        sort="points",
    )


def _rank_normalized_offers(
    offers: Iterable[award_providers.AwardOffer], brief: research_core.ResearchBrief
) -> list[dict[str, Any]]:
    """The one shared ranking boundary for every provider adapter."""

    return rank_candidates(
        (offer.to_candidate() for offer in offers),
        cabins=brief.cabin,
        seats=brief.passengers,
        direct=brief.direct_only,
        max_points=brief.max_points,
        programs=brief.sources,
        sort="points",
    )


class SeatsAeroResearchAdapter:
    """Explicit optional bridge from legacy Seats.aero storage to normalized offers.

    New adapters must implement ``AwardProvider`` directly and must not read the
    legacy Seat-shaped SQLite tables.  This adapter is the only compatibility path
    allowed to use them.
    """

    id = "seats.aero"
    display_name = "Seats.aero (bring your own eligible key)"
    program_catalog = PROGRAMS
    limits = award_providers.ProviderLimits(
        supports_fetch=True,
        requires_credentials=True,
        network_access=True,
        requires_explicit_selection=True,
        max_airports_per_side=research_core.MAX_AIRPORTS_PER_SIDE,
        max_date_span_days=research_core.MAX_FETCH_DATE_SPAN_DAYS,
        max_results=research_core.MAX_FETCH_RESULTS,
        strict_program_catalog=True,
        description=(
            "Optional local compatibility adapter. Cached reads use local legacy data; "
            "an explicit --fetch uses the user's eligible local key."
        ),
    )

    def __init__(
        self,
        db: Database,
        args: argparse.Namespace,
        *,
        fetcher: Callable[[Database, argparse.Namespace], tuple[int, int, list[dict[str, Any]]]],
        prepare_api: Callable[[], None] | None,
    ):
        self.db = db
        self.args = args
        self._fetcher = fetcher
        self._prepare_api = prepare_api

    def _offers_for_search(self, search_id: int) -> tuple[award_providers.AwardOffer, ...]:
        offers: list[award_providers.AwardOffer] = []
        for candidate in self.db.candidates(search_id=search_id):
            program = str(candidate.get("program") or "").lower()
            offers.append(award_providers.legacy_candidate_to_award_offer(
                candidate,
                provider_id=self.id,
                provider_name="Seats.aero",
                program_name=PROGRAMS.get(program, program or "Unknown redemption program"),
            ))
        return tuple(offers)

    def find_cached(
        self, request: award_providers.AwardSearchRequest
    ) -> award_providers.AwardSearchSnapshot:
        lookup = {
            "origins": ",".join(request.origins),
            "destinations": ",".join(request.destinations),
            "start_date": request.start_date,
            "end_date": request.end_date,
            "cabins": request.cabin,
            "sources": ",".join(request.program_ids),
            "carriers": "",
            "direct": request.direct_only,
        }
        cache = self.db.find_matching_search(**lookup, required_result_limit=request.result_limit)
        if cache is None:
            cache = self.db.find_compatible_search(**lookup, required_result_limit=request.result_limit)
        coverage = self.db.search_coverage(int(cache["id"])) if cache else None
        cache_summary = _cache_summary(cache, request.cache_ttl_hours, coverage)
        offers: tuple[award_providers.AwardOffer, ...] = ()
        if cache:
            offers = self._offers_for_search(int(cache["id"]))
        cache_is_fresh = bool(cache_summary and cache_summary["fresh"])
        cache_may_be_truncated = bool(coverage and coverage["may_be_truncated"])
        cache_summary_only = bool(coverage and not coverage["trip_details_requested"])
        can_suppress_fetch = bool(
            cache
            and cache_is_fresh
            and cache.get("cache_match") == "exact"
            and not cache_may_be_truncated
            and not cache_summary_only
        )
        if cache is None:
            status = "cache_miss"
        elif can_suppress_fetch:
            status = "cache_hit"
        elif cache_is_fresh:
            status = "cache_partial"
        else:
            status = "cache_stale"
        return award_providers.AwardSearchSnapshot(
            provider_id=self.id,
            provider_name=self.display_name,
            offers=offers,
            status=status,
            coverage=coverage or {},
            cache=cache_summary,
            can_suppress_fetch=can_suppress_fetch,
            fetch_supported=True,
        )

    def fetch(
        self, request: award_providers.AwardSearchRequest
    ) -> award_providers.AwardSearchSnapshot:
        if self._prepare_api is not None:
            self._prepare_api()
        search_id, count, _ = self._fetcher(self.db, _seats_fetch_args(request, self.args))
        stored_row = self.db.search_row(search_id)
        if stored_row is not None:
            fetched_cache = Database._cache_row_with_age(stored_row, max_age_seconds=None)
            fetched_cache["cache_match"] = "fresh_fetch"
            coverage = self.db.search_coverage(search_id) or {}
            cache_summary = _cache_summary(fetched_cache, request.cache_ttl_hours, coverage)
            offers = self._offers_for_search(search_id)
        else:
            # Test/dry adapters can return a search id without storing it. Do not
            # claim exhaustive coverage or provider verification in that case.
            coverage = {
                "requested_limit": request.result_limit,
                "stored_result_count": count,
                "provider_has_more": None,
                "result_cap_reached": count >= request.result_limit,
                "may_be_truncated": count >= request.result_limit,
                "trip_details_requested": True,
                "flight_level_availability_count": None,
                "summary_only_availability_count": None,
            }
            cache_summary = {
                "search_id": search_id,
                "match": "fresh_fetch",
                "age_seconds": 0,
                "ttl_hours": request.cache_ttl_hours,
                "fresh": True,
                "result_count": count,
                "coverage": coverage,
            }
            offers = ()
        partial = bool(coverage.get("may_be_truncated") or not coverage.get("trip_details_requested"))
        return award_providers.AwardSearchSnapshot(
            provider_id=self.id,
            provider_name=self.display_name,
            offers=offers,
            status="fetched_partial" if partial else "fetched",
            coverage=coverage,
            cache=cache_summary,
            can_suppress_fetch=not partial,
            fetch_supported=True,
            note=(
                "The bounded response may be result-limited or lack flight-level detail; "
                "narrow the brief or verify a shortlist before transferring points."
                if partial else None
            ),
        )


def _award_offer_inputs(args: argparse.Namespace) -> tuple[list[Any], list[dict[str, str]]]:
    """Read public normalized offer imports while rejecting environment-file paths."""

    payloads: list[Any] = []
    issues: list[dict[str, str]] = []
    for raw in getattr(args, "award_offer", []):
        try:
            payloads.append(json.loads(raw))
        except json.JSONDecodeError:
            issues.append({
                "field": "award_offer",
                "reason": "--award-offer must be valid normalized offer JSON; use --award-offer-file for a JSON file.",
            })
    for path in getattr(args, "award_offer_file", []):
        try:
            payloads.append(_read_json_file(path, "award-offer file"))
        except FlightError as exc:
            issues.append({"field": "award_offer", "reason": str(exc)})
    return payloads, issues


def build_research_provider_registry(
    db: Database | None,
    args: argparse.Namespace,
    *,
    manual_offers: Iterable[award_providers.AwardOffer] = (),
    fetcher: Callable[[Database, argparse.Namespace], tuple[int, int, list[dict[str, Any]]]] | None = None,
    prepare_api: Callable[[], None] | None = None,
) -> award_providers.ProviderRegistry:
    """Register local imports and only construct legacy storage adapters when needed."""

    providers: list[award_providers.AwardProvider] = [
        award_providers.ManualAwardImportProvider(tuple(manual_offers))
    ]
    if db is not None:
        providers.append(SeatsAeroResearchAdapter(
            db,
            args,
            fetcher=fetcher or fetch_search,
            prepare_api=prepare_api,
        ))
    return award_providers.ProviderRegistry(tuple(providers))


def provider_manifest() -> list[dict[str, Any]]:
    """Public capability manifest; it does not open a database or load credentials."""

    manual = award_providers.ManualAwardImportProvider()
    seats = SeatsAeroResearchAdapter
    manifest = award_providers.ProviderRegistry((manual,)).manifest()
    manifest.append({
        "id": seats.id,
        "name": seats.display_name,
        "program_catalog_size": len(seats.program_catalog),
        "supports_fetch": seats.limits.supports_fetch,
        "requires_credentials": seats.limits.requires_credentials,
        "network_access": seats.limits.network_access,
        "requires_explicit_selection": seats.limits.requires_explicit_selection,
        "max_airports_per_side": seats.limits.max_airports_per_side,
        "max_date_span_days": seats.limits.max_date_span_days,
        "max_results": seats.limits.max_results,
        "strict_program_catalog": seats.limits.strict_program_catalog,
        "description": seats.limits.description,
    })
    manifest.extend([
        {
            "id": "google_flights_handoff",
            "name": "Google Flights manual browser handoff",
            "program_catalog_size": 0,
            "supports_fetch": False,
            "requires_credentials": False,
            "network_access": False,
            "requires_explicit_selection": False,
            "max_airports_per_side": None,
            "max_date_span_days": None,
            "max_results": None,
            "strict_program_catalog": False,
            "description": "Manual browser handoff only; no scraping, browser automation, or fare-data ingestion.",
        },
        {
            "id": "manual_cash_import",
            "name": "Manual cash quote import",
            "program_catalog_size": 0,
            "supports_fetch": False,
            "requires_credentials": False,
            "network_access": False,
            "requires_explicit_selection": False,
            "max_airports_per_side": None,
            "max_date_span_days": None,
            "max_results": None,
            "strict_program_catalog": False,
            "description": "User-asserted cash observations only; never represented as a Google fare API result.",
        },
    ])
    return manifest


def run_research(
    db: Database | None,
    args: argparse.Namespace,
    *,
    fetcher: Callable[[Database, argparse.Namespace], tuple[int, int, list[dict[str, Any]]]] | None = None,
    prepare_api: Callable[[], None] | None = None,
    provider_registry: award_providers.ProviderRegistry | None = None,
) -> dict[str, Any]:
    """Run one selected provider adapter without ever querying Google Flights.

    The default adapter is a local normalized award-offer import and therefore needs
    neither a provider account nor a key.  Seats.aero remains available only when a
    caller explicitly selects ``--provider seats.aero``.
    """

    award_payloads, award_input_issues = _award_offer_inputs(args)
    manual_offers, award_validation_issues = award_providers.parse_award_offers(award_payloads)
    award_import_issues = [*award_input_issues, *award_validation_issues]
    if provider_registry is None:
        registry = build_research_provider_registry(
            db,
            args,
            manual_offers=manual_offers,
            fetcher=fetcher,
            prepare_api=prepare_api,
        )
    else:
        registry = provider_registry
    try:
        provider = registry.get(getattr(args, "provider", "manual_import"))
    except award_providers.ProviderError as exc:
        raise FlightError(str(exc)) from exc
    if provider.id != award_providers.ManualAwardImportProvider.id and manual_offers:
        award_import_issues.append({
            "field": "award_offer",
            "reason": "--award-offer imports use --provider manual_import; provider adapters are selected one at a time.",
        })

    raw_brief = _research_brief_input(args)
    program_catalog = dict(provider.program_catalog)
    # Imported offers carry their own canonical program names; use those names only
    # on the local import adapter so one selected provider cannot alter another
    # adapter's program filters.
    if provider.id == award_providers.ManualAwardImportProvider.id:
        for offer in manual_offers:
            program_catalog.setdefault(offer.program_id, offer.program_name)
    brief = research_core.parse_trip_brief(
        raw_brief,
        program_catalog,
        allow_unknown_program_ids=not provider.limits.strict_program_catalog,
    )

    transfer_payloads, transfer_profile_issues = _transfer_profile_inputs(args)
    parsed_transfer_groups: list[tuple[research_core.TransferProfile, ...]] = []
    for payload in transfer_payloads:
        profiles, profile_error = research_core.parse_transfer_profiles(payload, program_catalog)
        if profile_error or profiles is None:
            transfer_profile_issues.append({
                "field": "transfer_sources",
                "reason": profile_error or "The transfer-source profile is invalid.",
            })
        else:
            parsed_transfer_groups.append(profiles)
    if parsed_transfer_groups:
        combined_profiles, combine_error = research_core.merge_transfer_profiles(
            brief.transfer_profiles, *parsed_transfer_groups
        )
        if combine_error or combined_profiles is None:
            transfer_profile_issues.append({
                "field": "transfer_sources",
                "reason": combine_error or "The transfer-source profile is invalid.",
            })
        else:
            brief.transfer_profiles = combined_profiles
            brief.provenance["transfer_sources"] = "user"

    cash_payloads, cash_input_issues = _cash_quote_inputs(args)
    cash_quotes, cash_quote_issues = research_core.parse_manual_cash_quotes(cash_payloads)
    cash_quote_issues = cash_input_issues + cash_quote_issues
    fetch_issues = (
        brief.fetch_follow_ups(
            args.max_results,
            max_airports_per_side=provider.limits.max_airports_per_side,
            max_date_span_days=provider.limits.max_date_span_days,
            provider_max_results=provider.limits.max_results,
        )
        if provider.limits.supports_fetch else []
    )
    request = _research_request_summary(brief, args, provider)
    award: dict[str, Any] = {
        "provider": provider.id,
        "provider_name": provider.display_name,
        "provider_mode": "adapter_selected",
        "request": request,
        "status": "not_started",
        "cache": None,
        "coverage": None,
        "candidate_count": 0,
        "recommendations_by_program": [],
        "provenance": {
            "selected_adapter": provider.id,
            "requires_explicit_selection": provider.limits.requires_explicit_selection,
            "credentials_required_for_fetch": provider.limits.requires_credentials,
            "network_access": provider.limits.network_access,
            "network_used": False,
        },
    }
    candidates: list[dict[str, Any]] = []
    fetch_blocked = False

    if brief.follow_up_fields:
        award["status"] = "needs_clarification"
        if args.fetch and provider.limits.supports_fetch:
            fetch_blocked = True
            award["fetch"] = {"status": "blocked", "follow_up_fields": fetch_issues}
    elif brief.intent == "cash_only":
        award["status"] = "skipped_cash_only"
    else:
        try:
            snapshot = provider.find_cached(award_providers.AwardSearchRequest(
                origins=brief.origins,
                destinations=brief.destinations,
                start_date=brief.start_date or "",
                end_date=brief.end_date or "",
                cabin=brief.cabin,
                passengers=brief.passengers,
                program_ids=brief.programs,
                direct_only=brief.direct_only,
                max_points=brief.max_points,
                result_limit=args.max_results,
                cache_ttl_hours=args.cache_ttl_hours,
            ))
        except award_providers.ProviderError as exc:
            snapshot = award_providers.AwardSearchSnapshot(
                provider_id=provider.id,
                provider_name=provider.display_name,
                status="provider_unavailable",
                note=str(exc),
                fetch_supported=provider.limits.supports_fetch,
            )
        candidates = _rank_normalized_offers(snapshot.offers, brief)
        award.update({
            "status": snapshot.status,
            "cache": dict(snapshot.cache) if snapshot.cache is not None else None,
            "coverage": dict(snapshot.coverage),
            "candidate_count": len(candidates),
        })
        if snapshot.note:
            award["note"] = snapshot.note

        if args.fetch and provider.limits.supports_fetch and not snapshot.can_suppress_fetch:
            if fetch_issues:
                fetch_blocked = True
                award["fetch"] = {"status": "blocked", "follow_up_fields": fetch_issues}
            else:
                try:
                    fetched_snapshot = provider.fetch(award_providers.AwardSearchRequest(
                        origins=brief.origins,
                        destinations=brief.destinations,
                        start_date=brief.start_date or "",
                        end_date=brief.end_date or "",
                        cabin=brief.cabin,
                        passengers=brief.passengers,
                        program_ids=brief.programs,
                        direct_only=brief.direct_only,
                        max_points=brief.max_points,
                        result_limit=args.max_results,
                        cache_ttl_hours=args.cache_ttl_hours,
                    ))
                    candidates = _rank_normalized_offers(fetched_snapshot.offers, brief)
                    award.update({
                        "status": fetched_snapshot.status,
                        "cache": dict(fetched_snapshot.cache) if fetched_snapshot.cache is not None else None,
                        "coverage": dict(fetched_snapshot.coverage),
                        "candidate_count": len(candidates),
                        "fetch": {
                            "status": "completed",
                            "search_id": (fetched_snapshot.cache or {}).get("search_id"),
                            "availability_count": (fetched_snapshot.cache or {}).get("result_count"),
                            "coverage": dict(fetched_snapshot.coverage),
                        },
                    })
                    award["provenance"]["network_used"] = bool(
                        fetched_snapshot.coverage.get("network_used", provider.limits.network_access)
                    )
                    if fetched_snapshot.note:
                        award["fetch"]["note"] = fetched_snapshot.note
                except (FlightError, award_providers.ProviderError) as exc:
                    status = getattr(exc, "status", None)
                    error = f"{provider.display_name} search failed"
                    if status is not None:
                        error += f" (HTTP {status})"
                    award.update({
                        "status": "fetch_failed_using_cache" if candidates else "fetch_failed",
                        "fetch": {
                            "status": "failed",
                            "error": error + ".",
                            "note": "Any local results and the manual Google Flights handoff remain available; no live fare is claimed.",
                        },
                    })
        elif args.fetch and provider.limits.supports_fetch and snapshot.can_suppress_fetch:
            award["fetch"] = {"status": "skipped_fresh_exact_cache"}
        elif args.fetch and not provider.limits.supports_fetch:
            award["fetch"] = {
                "status": "not_supported",
                "note": "This local import adapter never fetches or loads credentials. Supply normalized --award-offer data or select an explicitly authorized provider adapter.",
            }
        elif not args.fetch and provider.id == award_providers.ManualAwardImportProvider.id and not snapshot.offers:
            award["next_action"] = "Supply a normalized --award-offer or --award-offer-file for local no-login ranking."
        elif not args.fetch and provider.limits.supports_fetch and snapshot.status == "cache_miss":
            award["next_action"] = (
                f"No compatible local cache was found for {provider.display_name}. "
                f"Re-run with --provider {provider.id} --fetch only if you are authorized to use that provider."
            )
        elif not args.fetch and snapshot.status == "cache_partial":
            award["next_action"] = (
                "The selected provider's local cache is partial. An explicit authorized provider refresh may be available; "
                "do not treat partial cached coverage as exhaustive."
            )

    if brief.intent == "award_only":
        cash_handoff: dict[str, Any] = {
            "provider": "google_flights",
            "status": "skipped_award_only",
            "mode": "manual_handoff",
            "scraped": False,
            "live_fares_obtained": False,
        }
    else:
        cash_handoff = research_core.google_flights_handoff(brief)
        cash_handoff["manual_import_schema"] = research_core.manual_cash_quote_schema()

    groups = research_core.group_award_recommendations(
        candidates, brief, program_catalog, cash_quotes, args.limit, brief.transfer_profiles
    )
    award["candidate_count"] = len(candidates)
    award["recommendations_by_program"] = groups
    comparison = research_core.comparison_summary(groups)
    transfer_profile_issues = _unique_research_issues(transfer_profile_issues)
    award_import_issues = _unique_research_issues(award_import_issues)
    report_follow_ups = _unique_research_issues(
        [*brief.follow_up_fields, *transfer_profile_issues, *(fetch_issues if fetch_blocked else [])]
    )
    report_status = "needs_clarification" if report_follow_ups else "ready"
    if not report_follow_ups and (
        award["status"] in {
            "cache_partial", "fetch_failed", "fetch_failed_using_cache", "fetched_partial",
            "provider_unavailable", "unavailable", "not_covered", "partial",
        }
        or cash_quote_issues
        or transfer_profile_issues
        or award_import_issues
    ):
        report_status = "partial"
    report = {
        "status": report_status,
        "brief": brief.to_dict(),
        "follow_up_fields": report_follow_ups,
        "award_search": award,
        "award_imports": {
            "mode": "manual_normalized_import",
            "schema_version": award_providers.AWARD_OFFER_SCHEMA_VERSION,
            "schema": award_providers.award_offer_schema(),
            "accepted_count": len(manual_offers),
            "issues": award_import_issues,
            "note": "Imported offers retain stated provider provenance but are user-supplied and not independently verified by Flight Finder.",
        },
        "cash_search": cash_handoff,
        "cash_quotes": {
            "mode": "manual_import_only",
            "quotes": cash_quotes,
            "issues": cash_quote_issues,
        },
        "transfer_reference": {
            "mode": "configured_transfer_sources",
            "selected_profiles": [
                research_core.transfer_profile_summary(profile) for profile in brief.transfer_profiles
            ],
            "issues": transfer_profile_issues,
            "live_source_lookup": False,
            "note": "Transfer profiles are optional static or user-supplied references. Partners, ratios, and timing can change; each recommendation requires source-side verification.",
        },
        "comparison": comparison,
        "limitations": [
            "Award results identify their selected adapter and per-offer provenance; imported records are not independently verified or a live-availability guarantee.",
            "This command does not scrape Google Flights and does not obtain live Google fares.",
            "Manual cash CPP is user-asserted evidence, not independently verified fare data.",
            "No cross-provider, cross-program, or cross-currency global winner is calculated.",
            "A hosted product must use providers licensed for its production, display, attribution, cache, and anonymous no-login use; it must report unavailable or partial coverage rather than silently falling back to an unlicensed source.",
            "Transfers are source-side and can be irreversible; verify current partner eligibility, ratio, timing, award space, and final fees before moving points.",
        ],
    }
    return report


def research_text(report: dict[str, Any], markdown: bool = False) -> str:
    """Compact human rendering; JSON carries the full AI-facing evidence."""
    brief = report["brief"]
    leg = brief["legs"][0]
    route = f"{','.join(leg['origin']) or '?'} → {','.join(leg['destination']) or '?'}"
    departure = leg["departure"]
    date_text = departure["start"] or "?"
    if departure["end"] and departure["end"] != departure["start"]:
        date_text += f"–{departure['end']}"
    cabin = brief["cabin"]["primary"] or "?"
    passengers = brief["passengers"]["count"] or "?"
    lines: list[str] = []
    if markdown:
        lines.extend(["# Flight research", "", f"**Brief:** {route}; {date_text}; {cabin}; {passengers} passenger(s)"])
    else:
        lines.append(f"Research brief: {route}; {date_text}; {cabin}; {passengers} passenger(s)")
    provenance = brief.get("provenance", {})
    if provenance:
        visible_fields = ("intent", "journey", "origin", "destination", "departure", "cabin", "passengers", "programs", "stops", "transfer_sources")
        sources = [f"{field}={provenance[field]}" for field in visible_fields if field in provenance]
        if sources:
            lines.append("Field sources: " + ", ".join(sources) + ".")
    if brief.get("assumptions"):
        lines.append("Assumptions: " + "; ".join(brief["assumptions"]))
    transfer_sources = brief.get("transfer_sources", {}).get("selected", [])
    if transfer_sources:
        lines.append(
            "Transfer sources: " + ", ".join(source["name"] for source in transfer_sources)
            + " (static/user-supplied references; verify before moving points)."
        )
    else:
        lines.append("Transfer sources: none configured; awards remain visible without a point-currency comparison.")

    follow_ups = report.get("follow_up_fields", [])
    if follow_ups:
        lines.append("\nFollow-up required before award search:")
        for issue in follow_ups:
            lines.append(f"- {issue['field']}: {issue['reason']}")

    award = report["award_search"]
    cache = award.get("cache")
    provider_label = award.get("provider_name") or award.get("provider") or "Award source"
    lines.append(f"\n{provider_label}: {award['status'].replace('_', ' ')}")
    if cache:
        age = cache.get("age_seconds")
        age_text = "unknown age" if age is None else f"{age}s old"
        lines.append(f"Cache: search #{cache['search_id']} ({cache['match']}, {age_text}, {'fresh' if cache['fresh'] else 'stale'}).")
        coverage = cache.get("coverage") or {}
        if coverage:
            detail_text = (
                f"{coverage.get('flight_level_availability_count')} flight-level / "
                f"{coverage.get('summary_only_availability_count')} summary-only"
                if coverage.get("flight_level_availability_count") is not None
                else "trip-detail coverage unavailable"
            )
            cap_text = "may be result-limited" if coverage.get("may_be_truncated") else "not known to be result-limited"
            lines.append(
                f"Coverage: {coverage.get('stored_result_count')} stored (requested cap {coverage.get('requested_limit')}); "
                f"{cap_text}; {detail_text}."
            )
    else:
        coverage = award.get("coverage") or {}
        if coverage.get("mode") == "manual_import":
            newest = coverage.get("newest_evidence_at") or "unknown evidence time"
            age = coverage.get("newest_evidence_age_seconds")
            age_text = "unknown age" if age is None else f"{age}s old"
            lines.append(
                "Import coverage: unknown/unverified (a local import can be incomplete); "
                f"{coverage.get('stored_result_count', 0)} matching record(s), newest evidence {newest} ({age_text})."
            )
    if award.get("note"):
        lines.append(str(award["note"]))
    if award.get("next_action"):
        lines.append(award["next_action"])
    fetch = award.get("fetch")
    if fetch and fetch.get("status") == "blocked":
        lines.append("Fetch is blocked until: " + "; ".join(issue["field"] for issue in fetch["follow_up_fields"]))
    elif fetch and fetch.get("status") == "failed":
        lines.append(f"{provider_label} fetch failed; cached results, if any, are shown below.")
    elif fetch and fetch.get("note"):
        lines.append(str(fetch["note"]))

    for group in award.get("recommendations_by_program", []):
        group_source = group.get("provider_name") or group.get("provider") or "Unknown provider"
        heading = (
            f"## {group_source} — {group['program_name']}"
            if markdown else f"\n{group_source} — {group['program_name']} ({group['program']})"
        )
        lines.append("\n" + heading if markdown else heading)
        for bucket in group.get("tax_currency_buckets", []):
            currency = bucket["tax_currency"] or "tax currency unknown"
            lines.append(f"Tax bucket: {currency}")
            for item in bucket.get("recommendations", []):
                seat_text = item["seats"] if item.get("seats") not in (None, 0) else "unknown"
                stops = item["stops"] if item.get("stops") is not None else "unknown"
                evidence = item["research_evidence"]
                provenance_suffix = ""
                if evidence.get("imported_manually"):
                    provenance_suffix = (
                        f" Source state: {evidence.get('provider_mode', 'unknown')} "
                        f"({evidence.get('verification_status', 'not independently verified')})."
                    )
                lines.append(
                    f"- {item.get('date')} {item.get('origin')}-{item.get('destination')}: "
                    f"{format_points(item.get('points'))} points, {format_taxes(item)}, seats {seat_text}, stops {stops} "
                    f"({evidence['detail_level']}).{provenance_suffix}"
                )
                for transfer in item.get("transfer_access", []):
                    if transfer.get("status") == "direct_reference" and transfer.get("source_points_to_transfer"):
                        lines.append(
                            f"  Transfer reference — {transfer['point_source_name']}: "
                            f"{format_points(transfer['source_points_to_transfer'])} source points → "
                            f"{format_points(transfer.get('recipient_points_received'))} {transfer.get('recipient_program', 'recipient points')} "
                            f"({transfer.get('ratio')}; verify source rules)."
                        )
                    else:
                        lines.append(
                            f"  Transfer reference — {transfer.get('point_source_name', transfer.get('point_source'))}: "
                            f"{transfer.get('reason', 'not configured; verify manually')}"
                        )
                comparisons = item.get("cash_comparison", [])
                if not comparisons:
                    lines.append("  Cash comparison: no matching manual cash quote was imported.")
                for comparison in comparisons:
                    if comparison.get("state") == "user_asserted_comparable":
                        evidence_reference = (
                            comparison.get("cash_booking_url")
                            or comparison.get("cash_itinerary_evidence")
                            or "manual itinerary evidence"
                        )
                        lines.append(
                            f"  User-asserted CPP — {comparison['point_source_name']}: {comparison['cpp']:.3f} cents/point "
                            f"from cash quote {comparison.get('cash_quote_id')} observed {comparison.get('cash_observed_at')} "
                            f"({evidence_reference}); verify itinerary and fare terms."
                        )
                    else:
                        lines.append(
                            f"  Cash comparison unavailable: {comparison.get('reason', 'not comparable.')}"
                        )

    handoff = report["cash_search"]
    lines.append(f"\nGoogle Flights: {handoff['status'].replace('_', ' ')} (manual handoff; no scraping or live fare retrieval).")
    if handoff.get("url"):
        lines.append("Opening this URL shares the displayed trip query with Google.")
        lines.append(handoff["url"])
    award_import_issues = report.get("award_imports", {}).get("issues", [])
    if award_import_issues:
        lines.append("Award-offer import follow-up: " + "; ".join(issue["reason"] for issue in award_import_issues))
    cash_quote_issues = report.get("cash_quotes", {}).get("issues", [])
    if cash_quote_issues:
        lines.append("Cash import follow-up: " + "; ".join(issue["reason"] for issue in cash_quote_issues))
    if report["comparison"]["comparable_pair_count"]:
        lines.append(
            f"User-asserted CPP pairs: {report['comparison']['comparable_pair_count']} (not independently verified)."
        )
    else:
        lines.append("No guarded CPP comparison is available until matching manual cash evidence is imported.")
    lines.append("Verify award space and final pricing on the redemption program site before moving points.")
    return "\n".join(lines)


def output_research(report: dict[str, Any], args: argparse.Namespace) -> None:
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(research_text(report, markdown=args.markdown))


def candidate_args(parser: argparse.ArgumentParser, include_search_id: bool = True) -> None:
    if include_search_id:
        parser.add_argument("--search-id", type=int, help="stored search ID (defaults to latest)")
    parser.add_argument("--cabin", dest="cabins", type=validate_cabins, default="", help="comma-separated cabins")
    parser.add_argument("--seats", type=int, default=None, help="required seats; unknown counts remain visible")
    parser.add_argument("--direct", action="store_true", help="require known nonstop options")
    parser.add_argument("--max-stops", type=int)
    parser.add_argument("--max-points", type=int)
    parser.add_argument("--programs", type=validate_programs, default="")
    parser.add_argument("--cpp", type=float, default=1.5, help="cents-per-point valuation for best ranking (default: 1.5)")
    parser.add_argument("--sort", choices=("best", "points", "taxes", "date"), default="best")
    parser.add_argument("--limit", type=int, default=20)
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="machine-readable output")
    output.add_argument("--markdown", action="store_true", help="report-friendly Markdown")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flight",
        description="Research normalized award offers locally, with optional explicitly selected provider adapters.",
    )
    parser.add_argument("--db", type=Path, help="SQLite path (or set FLIGHTS_DB_PATH)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="initialize the local SQLite database")

    search = subparsers.add_parser("search", help="fetch cached award availability and store it")
    search.add_argument("--from", dest="origins", required=True, type=validate_airports)
    search.add_argument("--to", dest="destinations", required=True, type=validate_airports)
    search.add_argument("--start", dest="start_date", required=True, type=validate_date)
    search.add_argument("--end", dest="end_date", type=validate_date, help="defaults to --start")
    search.add_argument("--cabin", dest="cabins", type=validate_cabins, default="business")
    search.add_argument("--programs", dest="sources", type=validate_programs, default="")
    search.add_argument("--carriers", type=lambda value: normalize_csv(value, upper=True), default="")
    search.add_argument("--direct", action="store_true")
    search.add_argument("--seats", type=int, default=1, help="used for local ranking; API seat counts can be unknown")
    search.add_argument("--max-results", type=int, default=100, help="10 or more; may paginate and use multiple API calls")
    search.add_argument("--summary-only", action="store_true", help="skip embedded flight-level trips for a smaller response")
    search.add_argument("--timeout", type=int, default=30)
    search.add_argument("--limit", type=int, default=15, help="ranked rows to display after fetching")
    search.add_argument("--cpp", type=float, default=1.5)
    output = search.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true")
    output.add_argument("--markdown", action="store_true")

    research = subparsers.add_parser(
        "research",
        help="plan/cache an explainable award-first brief and create a Google Flights browser handoff",
    )
    research.add_argument(
        "brief", nargs="?",
        help="JSON brief or constrained text, for example 'SFO to CDG 2026-09-01 business 2 passengers'",
    )
    research.add_argument("--brief", dest="brief_option", help="explicit alternative to the positional trip brief")
    research.add_argument("--brief-file", type=Path, help="JSON brief below imports/ or examples/ (regular file, 1 MB max)")
    research.add_argument(
        "--provider", default="manual_import",
        help="selected award adapter (default: manual_import; run './flight providers --json' for capabilities)",
    )
    research.add_argument(
        "--fetch", action="store_true",
        help="explicitly request one bounded fetch only from the selected adapter when it supports authorized fetching",
    )
    research.add_argument("--cache-ttl-hours", type=float, default=24.0, help="fresh-cache threshold for adapters that expose a cache (default: 24)")
    research.add_argument("--max-results", type=int, default=100, help="provider fetch cap where supported (default: 100; Seats.aero requires 10-100)")
    research.add_argument("--timeout", type=int, default=30)
    research.add_argument("--limit", type=int, default=5, help="recommendations per provider/program/tax-currency bucket")
    research.add_argument(
        "--award-offer", action="append", default=[],
        help="normalized manual award-offer JSON object/list; default no-login source, never fetched",
    )
    research.add_argument(
        "--award-offer-file", action="append", default=[], type=Path,
        help="normalized offer JSON below imports/ or examples/ (regular non-symlink file, 1 MB max)",
    )
    research.add_argument(
        "--cash-quote", action="append", default=[],
        help="manual cash-quote JSON object/list; never fetched from Google Flights",
    )
    research.add_argument(
        "--cash-quote-file", action="append", default=[], type=Path,
        help="cash-quote JSON below imports/ or examples/ (regular file, 1 MB max)",
    )
    research.add_argument(
        "--transfer-profile", action="append", default=[],
        help="JSON transfer-source profile/object; supports any transferable-point currency",
    )
    research.add_argument(
        "--transfer-profile-file", action="append", default=[], type=Path,
        help="transfer-profile JSON below imports/ or examples/ (regular file, 1 MB max)",
    )
    research_output = research.add_mutually_exclusive_group()
    research_output.add_argument("--json", action="store_true", help="machine-readable research report")
    research_output.add_argument("--markdown", action="store_true", help="human-readable Markdown report")

    results = subparsers.add_parser("results", help="rank locally cached results without an API call")
    candidate_args(results)

    trips = subparsers.add_parser("trips", help="fetch and store flight-level details for one availability ID")
    trips.add_argument("availability_id")
    trips.add_argument("--include-filtered", action="store_true")
    trips.add_argument("--timeout", type=int, default=30)
    candidate_args(trips, include_search_id=False)

    searches = subparsers.add_parser("searches", help="list prior searches")
    searches.add_argument("--limit", type=int, default=20)
    searches.add_argument("--json", action="store_true")

    stats = subparsers.add_parser("stats", help="show local cache and API-call counts")
    stats.add_argument("--json", action="store_true")

    programs = subparsers.add_parser("programs", help="list accepted legacy Seats.aero program source names")
    programs.add_argument("--json", action="store_true")

    providers = subparsers.add_parser("providers", help="list local and optional provider capability boundaries")
    providers.add_argument("--json", action="store_true")

    return parser


def ensure_positive(args: argparse.Namespace) -> None:
    for name in ("seats", "limit", "max_results", "timeout"):
        value = getattr(args, name, None)
        if value is not None and value < 1:
            raise FlightError(f"--{name.replace('_', '-')} must be positive")
    selected_provider_id = award_providers.normalize_provider_id(
        getattr(args, "provider", "manual_import")
    )
    seats_minimum_applies = (
        getattr(args, "command", None) != "research" or selected_provider_id == "seats.aero"
    )
    if getattr(args, "max_results", 10) < 10 and seats_minimum_applies:
        raise FlightError("--max-results must be at least 10 (Seats.aero API minimum)")
    cache_ttl_hours = getattr(args, "cache_ttl_hours", 0)
    if cache_ttl_hours is not None and (
        not isinstance(cache_ttl_hours, (int, float))
        or not math.isfinite(cache_ttl_hours)
        or cache_ttl_hours < 0
    ):
        raise FlightError("--cache-ttl-hours must be a finite non-negative number")
    if getattr(args, "max_stops", 0) is not None and getattr(args, "max_stops", 0) < 0:
        raise FlightError("--max-stops cannot be negative")
    cpp = getattr(args, "cpp", 1.5)
    if not isinstance(cpp, (int, float)) or not math.isfinite(cpp) or cpp <= 0:
        raise FlightError("--cpp must be a finite number greater than zero")


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    ensure_positive(args)
    # Provider capability inspection and default manual research are entirely
    # DB/key-free. The legacy storage adapter is opened only when explicitly selected.
    if args.command == "providers":
        manifest = provider_manifest()
        if args.json:
            print(json.dumps(manifest, indent=2))
        else:
            for item in manifest:
                credential_note = "credentials required" if item["requires_credentials"] else "no credentials"
                print(f"{item['id']:<24} {credential_note}; {item['description']}")
        return 0
    selected_provider_id = award_providers.normalize_provider_id(
        getattr(args, "provider", "manual_import")
    )
    if args.command == "research" and selected_provider_id != "seats.aero":
        try:
            report = run_research(None, args)
            output_research(report, args)
            return 0
        except FlightError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2

    # Legacy command credentials are only relevant to the two commands that can
    # contact the optional Seats.aero compatibility API. Cache inspection/init does
    # not need to load an environment file.
    if args.command in {"search", "trips"}:
        load_dotenv(PROJECT_DIR / ".env")
    env_db = os.environ.get("FLIGHTS_DB_PATH")
    db_path = (args.db or (Path(env_db).expanduser() if env_db else DEFAULT_DB)).resolve()
    db = Database(db_path)
    try:
        if args.command == "init":
            print(f"Initialized {db.path}")
            return 0

        if args.command == "programs":
            if args.json:
                print(json.dumps(PROGRAMS, indent=2, sort_keys=True))
            else:
                for source, name in PROGRAMS.items():
                    print(f"{source:<18} {name}")
            return 0

        if args.command == "stats":
            stats = db.stats()
            if args.json:
                print(json.dumps(stats, indent=2))
            else:
                for key, value in stats.items():
                    print(f"{key:<22} {value}")
            return 0

        if args.command == "searches":
            rows = [dict(row) for row in db.list_searches(args.limit)]
            if args.json:
                print(json.dumps(rows, indent=2))
            elif not rows:
                print("No stored searches.")
            else:
                print("ID  Status    Requested (UTC)           Route              Dates                    Cabins             Results")
                print("--  --------  ------------------------  -----------------  -----------------------  -----------------  -------")
                for row in rows:
                    dates = row["start_date"] + ((".." + row["end_date"]) if row["end_date"] != row["start_date"] else "")
                    print(
                        f"{row['id']:<3} {row['status']:<9} {row['requested_at']:<25} "
                        f"{row['origins']+'-'+row['destinations']:<18} {dates:<24} "
                        f"{(row['cabins'] or 'all'):<18} {row['result_count']}"
                    )
            return 0

        if args.command == "research":
            report = run_research(
                db, args,
                prepare_api=lambda: load_dotenv(PROJECT_DIR / ".env"),
            )
            output_research(report, args)
            return 0

        if args.command == "search":
            args.end_date = args.end_date or args.start_date
            search_id, count, _ = fetch_search(db, args)
            ranked = rank_candidates(
                db.candidates(search_id=search_id), cabins=args.cabins, seats=args.seats,
                direct=args.direct, cents_per_point=args.cpp,
            )
            output_candidates(
                ranked, args,
                {"search_id": search_id, "availability_count": count, "api_note": "cached search"},
            )
            if not args.json:
                print(f"Stored search #{search_id} with {count} availability objects in {db.path}")
            return 0

        if args.command == "results":
            search_id = args.search_id or db.latest_search_id()
            if search_id is None:
                raise FlightError("no completed searches; run './flight search ...' first")
            search = db.search_row(search_id)
            if not search:
                raise FlightError(f"search #{search_id} does not exist")
            seats = args.seats if args.seats is not None else int(search["min_seats"])
            cabins = args.cabins or search["cabins"] or ""
            ranked = rank_candidates(
                db.candidates(search_id=search_id), cabins=cabins, seats=seats,
                direct=args.direct, max_stops=args.max_stops, max_points=args.max_points,
                programs=args.programs, cents_per_point=args.cpp, sort=args.sort,
            )
            output_candidates(ranked, args, {"search_id": search_id})
            return 0

        if args.command == "trips":
            if not db.availability_exists(args.availability_id):
                raise FlightError(
                    f"availability ID {args.availability_id!r} is not in the local database"
                )
            client = SeatsAeroClient(
                os.environ.get("SEATS_AERO_API_KEY", ""),
                on_attempt=lambda endpoint, status: db.log_api_request(endpoint, status),
                timeout=args.timeout,
            )
            payload = client.get(f"trips/{urllib.parse.quote(args.availability_id, safe='')}", {
                "include_filtered": args.include_filtered,
            })
            trip_count = db.store_trip_payload(args.availability_id, payload)
            seats = args.seats or 1
            ranked = rank_candidates(
                db.candidates(availability_id=args.availability_id), cabins=args.cabins,
                seats=seats, direct=args.direct, max_stops=args.max_stops,
                max_points=args.max_points, programs=args.programs,
                cents_per_point=args.cpp, sort=args.sort,
            )
            output_candidates(
                ranked, args,
                {"availability_id": args.availability_id, "fetched_trip_count": trip_count},
            )
            if not args.json:
                print(f"Fetched and stored {trip_count} flight-level trips.")
            return 0

        parser.error("unknown command")
        return 2
    except FlightError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
