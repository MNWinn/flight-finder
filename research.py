"""Pure planning and reporting helpers for the local award-flight research CLI.

This module deliberately does not call airline, transferable-point-source, or Google services.  It parses a
small, deterministic trip brief, builds a browser handoff to Google Flights, and makes
local award/cash comparisons explainable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import datetime as dt
import json
import math
import re
import urllib.parse
from typing import Any, Mapping, Sequence


CABINS = ("economy", "premium", "business", "first")
CABIN_ALIASES = {
    "premium economy": "premium",
    "premium-economy": "premium",
    "premium_economy": "premium",
    "pe": "premium",
    "economy": "economy",
    "coach": "economy",
    "business": "business",
    "business class": "business",
    "first": "first",
    "first class": "first",
}
DEFAULT_CABIN = "business"
DEFAULT_PASSENGERS = 1
MAX_AIRPORTS_PER_SIDE = 3
MAX_FETCH_DATE_SPAN_DAYS = 14
MAX_FETCH_RESULTS = 100
GOOGLE_FLIGHTS_URL = "https://www.google.com/travel/flights"

CHASE_TRANSFER_URL = "https://www.chase.com/personal/credit-cards/education/basics/how-to-transfer-chase-ultimate-rewards-points"
CAPITAL_ONE_TRANSFER_URL = "https://www.capitalone.com/learn-grow/money-management/venture-miles-transfer-partnerships/"
# The bundled profiles are optional convenience references, not runtime issuer data.
BUILTIN_TRANSFER_REFERENCE_VERSION = "builtin-static-v1"
BUILTIN_TRANSFER_REFERENCE_AS_OF = "2026-08-25"


@dataclass(frozen=True)
class TransferRule:
    """One recipient-program rule for a configurable transferable-point source."""

    program: str
    recipient_name: str
    recipient_per_1000_source_points: int
    source_url: str | None = None
    minimum_source_points: int = 1000
    source_increment: int = 1000
    transfer_time_note: str = "Verify the transfer time with the source before moving points."
    requires_manual_confirmation: bool = False


@dataclass(frozen=True)
class TransferProfile:
    """A user-selected or user-supplied transferable-point source profile."""

    id: str
    name: str
    rules: tuple[TransferRule, ...] = ()
    reference_version: str = "user-supplied"
    as_of: str | None = None
    source_url: str | None = None


# These profiles are deliberately opt-in. The tool can also consume any user-supplied
# profile with the documented JSON schema, so it is not limited to a particular issuer.
BUILTIN_TRANSFER_PROFILES: dict[str, TransferProfile] = {
    "chase_ultimate_rewards": TransferProfile(
        id="chase_ultimate_rewards",
        name="Chase Ultimate Rewards",
        reference_version=BUILTIN_TRANSFER_REFERENCE_VERSION,
        as_of=BUILTIN_TRANSFER_REFERENCE_AS_OF,
        source_url=CHASE_TRANSFER_URL,
        rules=(
            TransferRule("aeroplan", "Air Canada Aeroplan", 1000, CHASE_TRANSFER_URL),
            TransferRule("flyingblue", "Air France/KLM Flying Blue", 1000, CHASE_TRANSFER_URL),
            TransferRule("jetblue", "JetBlue TrueBlue", 1000, CHASE_TRANSFER_URL),
            TransferRule("singapore", "Singapore KrisFlyer", 1000, CHASE_TRANSFER_URL),
            TransferRule("united", "United MileagePlus", 1000, CHASE_TRANSFER_URL),
            TransferRule("virginatlantic", "Virgin Atlantic Flying Club", 1000, CHASE_TRANSFER_URL),
        ),
    ),
    "capital_one_miles": TransferProfile(
        id="capital_one_miles",
        name="Capital One Miles",
        reference_version=BUILTIN_TRANSFER_REFERENCE_VERSION,
        as_of=BUILTIN_TRANSFER_REFERENCE_AS_OF,
        source_url=CAPITAL_ONE_TRANSFER_URL,
        rules=(
            TransferRule("aeromexico", "Aeromexico Rewards (formerly Club Premier)", 1000, CAPITAL_ONE_TRANSFER_URL),
            TransferRule("aeroplan", "Air Canada Aeroplan", 1000, CAPITAL_ONE_TRANSFER_URL),
            TransferRule("emirates", "Emirates Skywards", 750, CAPITAL_ONE_TRANSFER_URL),
            TransferRule("etihad", "Etihad Guest", 1000, CAPITAL_ONE_TRANSFER_URL),
            TransferRule("finnair", "Finnair Plus", 1000, CAPITAL_ONE_TRANSFER_URL),
            TransferRule("flyingblue", "Air France/KLM Flying Blue", 1000, CAPITAL_ONE_TRANSFER_URL),
            TransferRule("jetblue", "JetBlue TrueBlue", 600, CAPITAL_ONE_TRANSFER_URL),
            TransferRule("qantas", "Qantas Frequent Flyer", 1000, CAPITAL_ONE_TRANSFER_URL),
            TransferRule("qatar", "Qatar Privilege Club", 1000, CAPITAL_ONE_TRANSFER_URL),
            TransferRule("singapore", "Singapore KrisFlyer", 1000, CAPITAL_ONE_TRANSFER_URL),
            TransferRule("turkish", "Turkish Miles & Smiles", 1000, CAPITAL_ONE_TRANSFER_URL),
            # Capital One's target is Virgin Red, while Seats.aero reports Virgin Atlantic.
            TransferRule(
                "virginatlantic", "Virgin Red", 1000, CAPITAL_ONE_TRANSFER_URL,
                requires_manual_confirmation=True,
                transfer_time_note="Capital One targets Virgin Red; confirm whether it can fund this Virgin Atlantic award before transferring.",
            ),
        ),
    ),
}
BUILTIN_TRANSFER_ALIASES = {
    "chase": "chase_ultimate_rewards",
    "chase ultimate rewards": "chase_ultimate_rewards",
    "capital one": "capital_one_miles",
    "capital one miles": "capital_one_miles",
}


@dataclass
class ResearchBrief:
    origins: tuple[str, ...] = ()
    destinations: tuple[str, ...] = ()
    start_date: str | None = None
    end_date: str | None = None
    cabin: str = DEFAULT_CABIN
    passengers: int = DEFAULT_PASSENGERS
    programs: tuple[str, ...] = ()
    direct_only: bool = False
    max_points: int | None = None
    intent: str = "award_first"
    journey: str = "one_way"
    transfer_profiles: tuple[TransferProfile, ...] = ()
    provenance: dict[str, str] = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)
    follow_up_fields: list[dict[str, str]] = field(default_factory=list)
    input_format: str = "text"

    @property
    def ready(self) -> bool:
        return not self.follow_up_fields

    @property
    def date_span_days(self) -> int | None:
        if not self.start_date or not self.end_date:
            return None
        try:
            return (dt.date.fromisoformat(self.end_date) - dt.date.fromisoformat(self.start_date)).days + 1
        except ValueError:
            return None

    @property
    def sources(self) -> str:
        return ",".join(self.programs)

    def fetch_follow_ups(self, max_results: int) -> list[dict[str, str]]:
        issues = list(self.follow_up_fields)
        if len(self.origins) > MAX_AIRPORTS_PER_SIDE:
            issues.append({
                "field": "origin", "reason": f"Limit an API fetch to at most {MAX_AIRPORTS_PER_SIDE} origin IATA codes."
            })
        if len(self.destinations) > MAX_AIRPORTS_PER_SIDE:
            issues.append({
                "field": "destination", "reason": f"Limit an API fetch to at most {MAX_AIRPORTS_PER_SIDE} destination IATA codes."
            })
        span = self.date_span_days
        if span is not None and span > MAX_FETCH_DATE_SPAN_DAYS:
            issues.append({
                "field": "departure", "reason": f"Limit an API fetch to a {MAX_FETCH_DATE_SPAN_DAYS}-day departure window or less."
            })
        if max_results > MAX_FETCH_RESULTS:
            issues.append({
                "field": "max_results", "reason": f"Limit an API fetch to {MAX_FETCH_RESULTS} availability summaries or fewer."
            })
        return _unique_follow_ups(issues)

    def to_dict(self) -> dict[str, Any]:
        programs: list[str] | str = list(self.programs) if self.programs else "all_supported"
        passenger_source = self.provenance.get("passengers", "unresolved")
        cabin_source = self.provenance.get("cabin", "unresolved")
        program_source = self.provenance.get("programs", "default")
        # A dataclass default is useful internally while parsing, but is never emitted
        # as a resolved user fact after an ambiguity/error has made that field blocked.
        passenger_count = None if passenger_source == "unresolved" else self.passengers
        cabin_primary = None if cabin_source == "unresolved" else self.cabin
        if program_source == "unresolved":
            programs = "unresolved"
        stops_source = self.provenance.get("stops", "default")
        max_points_source = self.provenance.get("max_points", "not_set")
        return {
            "intent": self.intent if self.provenance.get("intent") != "unresolved" else "unresolved",
            "journey": self.journey if self.provenance.get("journey") != "unresolved" else "unresolved",
            "legs": [{
                "origin": list(self.origins),
                "destination": list(self.destinations),
                "departure": {"start": self.start_date, "end": self.end_date},
            }],
            "passengers": {"count": passenger_count, "source": passenger_source},
            "cabin": {"primary": cabin_primary, "source": cabin_source},
            "stops": {
                "direct_only": None if stops_source == "unresolved" else self.direct_only,
                "preference": "unresolved" if stops_source == "unresolved" else (
                    "nonstop_only" if self.direct_only else "prefer_nonstop_allow_connections"
                ),
                "source": stops_source,
            },
            "points": {
                "programs": programs,
                "source": program_source,
                "max_points": self.max_points,
                "max_points_source": max_points_source,
            },
            "transfer_sources": {
                "selected": [transfer_profile_summary(profile) for profile in self.transfer_profiles],
                "source": self.provenance.get("transfer_sources", "not_configured"),
            },
            "provenance": dict(self.provenance),
            "assumptions": list(self.assumptions),
            "follow_up_fields": list(self.follow_up_fields),
            "input_format": self.input_format,
        }


def _unique_follow_ups(items: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, str]] = []
    for item in items:
        key = (item.get("field", "brief"), item.get("reason", ""))
        if key not in seen:
            seen.add(key)
            result.append({"field": key[0], "reason": key[1]})
    return result


def _append_issue(brief: ResearchBrief, field: str, reason: str) -> None:
    brief.follow_up_fields.append({"field": field, "reason": reason})
    brief.provenance[field] = "unresolved"


def _normalize_airports(value: Any) -> tuple[str, ...] | None:
    if isinstance(value, str):
        values = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple)):
        values = []
        for item in value:
            if not isinstance(item, str):
                return None
            values.extend(part.strip() for part in item.split(",") if part.strip())
    else:
        return None
    if not values or any(not re.fullmatch(r"[A-Za-z]{3}", item) for item in values):
        return None
    return tuple(dict.fromkeys(item.upper() for item in values))


def _parse_iso_date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return None


def _normalize_cabin(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace("_", " ")
    return CABIN_ALIASES.get(normalized)


def _normalize_program(value: str, programs: Mapping[str, str]) -> str | None:
    needle = re.sub(r"[^a-z0-9]+", "", value.lower())
    for source, name in programs.items():
        aliases = (source, name, name.replace("/", " "))
        if any(needle == re.sub(r"[^a-z0-9]+", "", alias.lower()) for alias in aliases):
            return source
    return None


def _normalize_programs(value: Any, programs: Mapping[str, str]) -> tuple[str, ...] | None:
    if value is None or value == "" or value == "all" or value == "all_supported":
        return ()
    if isinstance(value, str):
        values = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple)):
        values = [str(item).strip() for item in value if str(item).strip()]
    else:
        return None
    normalized: list[str] = []
    for item in values:
        source = _normalize_program(item, programs)
        if source is None:
            return None
        normalized.append(source)
    return tuple(dict.fromkeys(normalized))


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        result = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        result = int(value)
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        result = int(value.strip())
    else:
        return None
    return result if result > 0 else None


def _normalise_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false", "yes", "no", "1", "0"}:
        return value.strip().lower() in {"true", "yes", "1"}
    return None


_TRANSFER_PROFILE_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}")


def _normalise_transfer_profile_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace(" ", "_")
    return normalized if _TRANSFER_PROFILE_ID.fullmatch(normalized) else None


def _normalise_reference_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    url = value.strip()
    parsed = urllib.parse.urlparse(url)
    return url if parsed.scheme in {"http", "https"} and parsed.netloc else None


def transfer_profile_summary(profile: TransferProfile) -> dict[str, Any]:
    """Expose configuration metadata without claiming a live transfer lookup."""

    return {
        "id": profile.id,
        "name": profile.name,
        "profile_kind": "builtin_static" if profile.id in BUILTIN_TRANSFER_PROFILES else "user_supplied",
        "reference_version": profile.reference_version,
        "as_of": profile.as_of,
        "source_url": profile.source_url,
        "configured_partner_count": len(profile.rules),
        "verification_required": True,
    }


def _builtin_transfer_profile(value: str) -> TransferProfile | None:
    normalized = _normalise_transfer_profile_id(value)
    if normalized in BUILTIN_TRANSFER_PROFILES:
        return BUILTIN_TRANSFER_PROFILES[normalized]
    alias = BUILTIN_TRANSFER_ALIASES.get(value.strip().lower())
    return BUILTIN_TRANSFER_PROFILES.get(alias) if alias else None


def _parse_custom_transfer_profile(
    value: Mapping[str, Any], programs: Mapping[str, str]
) -> tuple[TransferProfile | None, str | None]:
    profile_id = _normalise_transfer_profile_id(value.get("id"))
    if profile_id is None:
        return None, "Each custom transfer source needs an id using lowercase letters, numbers, underscores, or hyphens."
    if profile_id in BUILTIN_TRANSFER_PROFILES:
        return None, f"'{profile_id}' is a built-in profile id; select it by id instead of redefining it."
    name = value.get("name")
    if not isinstance(name, str) or not name.strip():
        return None, "Each custom transfer source needs a non-empty name."
    partners_value = value.get("partners", value.get("transfer_partners", []))
    if partners_value is None:
        partners_value = []
    if not isinstance(partners_value, list):
        return None, f"Transfer source '{profile_id}' partners must be a JSON list."

    rules: list[TransferRule] = []
    seen_programs: set[str] = set()
    for index, partner in enumerate(partners_value, 1):
        if not isinstance(partner, Mapping):
            return None, f"Transfer source '{profile_id}' partner #{index} must be a JSON object."
        program_value = partner.get("program", partner.get("recipient_program"))
        program = _normalize_program(str(program_value), programs) if program_value is not None else None
        if program is None:
            return None, f"Transfer source '{profile_id}' partner #{index} needs a supported redemption program."
        if program in seen_programs:
            return None, f"Transfer source '{profile_id}' defines '{program}' more than once."
        recipient_name = partner.get("recipient_name", programs[program])
        if not isinstance(recipient_name, str) or not recipient_name.strip():
            return None, f"Transfer source '{profile_id}' partner #{index} needs a recipient_name."
        rate = _positive_int(
            partner.get("recipient_per_1000_source_points", partner.get("recipient_per_1000_points"))
        )
        minimum = _positive_int(partner.get("minimum_source_points", 1000))
        increment = _positive_int(partner.get("source_increment", partner.get("increment", 1000)))
        if rate is None or minimum is None or increment is None:
            return None, (
                f"Transfer source '{profile_id}' partner #{index} needs positive whole-number "
                "recipient_per_1000_source_points, minimum_source_points, and source_increment values."
            )
        source_url_value = partner.get("source_url")
        source_url = _normalise_reference_url(source_url_value) if source_url_value is not None else None
        if source_url_value is not None and source_url is None:
            return None, f"Transfer source '{profile_id}' partner #{index} source_url must be an http(s) URL."
        manual_value = partner.get("requires_manual_confirmation", False)
        manual = _normalise_bool(manual_value)
        if manual is None:
            return None, f"Transfer source '{profile_id}' partner #{index} requires_manual_confirmation must be boolean."
        note = partner.get("transfer_time_note", "Verify the transfer time with the source before moving points.")
        if not isinstance(note, str) or not note.strip():
            return None, f"Transfer source '{profile_id}' partner #{index} transfer_time_note must be text."
        seen_programs.add(program)
        rules.append(TransferRule(
            program=program,
            recipient_name=recipient_name.strip(),
            recipient_per_1000_source_points=rate,
            source_url=source_url,
            minimum_source_points=minimum,
            source_increment=increment,
            transfer_time_note=note.strip(),
            requires_manual_confirmation=manual,
        ))

    reference_version = value.get("reference_version", "user-supplied")
    if not isinstance(reference_version, str) or not reference_version.strip():
        return None, f"Transfer source '{profile_id}' reference_version must be text."
    as_of_value = value.get("as_of")
    as_of = _parse_iso_date(as_of_value) if as_of_value not in (None, "") else None
    if as_of_value not in (None, "") and as_of is None:
        return None, f"Transfer source '{profile_id}' as_of must use YYYY-MM-DD."
    profile_url_value = value.get("source_url")
    profile_url = _normalise_reference_url(profile_url_value) if profile_url_value is not None else None
    if profile_url_value is not None and profile_url is None:
        return None, f"Transfer source '{profile_id}' source_url must be an http(s) URL."
    return TransferProfile(
        id=profile_id,
        name=name.strip(),
        rules=tuple(rules),
        reference_version=reference_version.strip(),
        as_of=as_of,
        source_url=profile_url,
    ), None


def parse_transfer_profiles(
    value: Any, programs: Mapping[str, str]
) -> tuple[tuple[TransferProfile, ...] | None, str | None]:
    """Parse selected built-ins and arbitrary user-supplied transfer-source profiles."""

    if value is None:
        return (), None
    if isinstance(value, Mapping):
        if "profiles" in value:
            value = value["profiles"]
        elif "transfer_sources" in value:
            value = value["transfer_sources"]
        else:
            value = [value]
    if isinstance(value, str):
        items: list[Any] = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return None, "transfer_sources must be a profile object, a list of profiles, or selected built-in ids."

    profiles: list[TransferProfile] = []
    seen_ids: set[str] = set()
    for item in items:
        if isinstance(item, str):
            profile = _builtin_transfer_profile(item)
            if profile is None:
                return None, (
                    f"No built-in transfer source named '{item}'. Supply a custom profile object with its transfer partners."
                )
        elif isinstance(item, Mapping):
            profile, error = _parse_custom_transfer_profile(item, programs)
            if error or profile is None:
                return None, error or "The custom transfer source is invalid."
        else:
            return None, "Each transfer source must be a built-in id or a JSON profile object."
        if profile.id in seen_ids:
            return None, f"Transfer source '{profile.id}' was supplied more than once."
        seen_ids.add(profile.id)
        profiles.append(profile)
    return tuple(profiles), None


def merge_transfer_profiles(*groups: Sequence[TransferProfile]) -> tuple[tuple[TransferProfile, ...] | None, str | None]:
    profiles: list[TransferProfile] = []
    seen_ids: set[str] = set()
    for group in groups:
        for profile in group:
            if profile.id in seen_ids:
                return None, f"Transfer source '{profile.id}' was supplied more than once."
            seen_ids.add(profile.id)
            profiles.append(profile)
    return tuple(profiles), None


_TEXT_COUNT_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


def _parse_text_count(value: str) -> int | None:
    normalized = value.strip().lower()
    if normalized in _TEXT_COUNT_WORDS:
        return _TEXT_COUNT_WORDS[normalized]
    if re.fullmatch(r"[+-]?\d+", normalized):
        # A signed party size is ambiguous rather than a basis for silently
        # changing the requested count.
        if normalized.startswith(("+", "-")):
            return None
        return int(normalized)
    return None


def _parse_text_point_limit(value: str) -> int | None:
    normalized = value.strip().lower().replace(",", "")
    multiplier = Decimal(1000) if normalized.endswith("k") else Decimal(1)
    if normalized.endswith("k"):
        normalized = normalized[:-1]
    try:
        amount = Decimal(normalized)
    except InvalidOperation:
        return None
    points = amount * multiplier
    if not points.is_finite() or points <= 0 or points != points.to_integral_value():
        return None
    return int(points)


def _set_dates(brief: ResearchBrief, start_value: Any, end_value: Any, source: str) -> None:
    start = _parse_iso_date(start_value)
    end = _parse_iso_date(end_value) if end_value not in (None, "") else start
    if start_value in (None, ""):
        _append_issue(brief, "departure", "Provide a departure date or date range in YYYY-MM-DD format.")
        return
    if not start:
        _append_issue(brief, "departure", "Use YYYY-MM-DD for the departure date.")
        return
    if not end:
        _append_issue(brief, "departure", "Use YYYY-MM-DD for the end of the departure range.")
        return
    if start > end:
        _append_issue(brief, "departure", "The departure range start must be on or before its end.")
        return
    brief.start_date = start
    brief.end_date = end
    brief.provenance["departure"] = source
    if end_value in (None, ""):
        brief.assumptions.append("The single departure date is treated as an exact one-day search.")


def _apply_common_defaults(brief: ResearchBrief) -> None:
    intent_unresolved = brief.provenance.get("intent") == "unresolved"
    if "cabin" not in brief.provenance:
        if intent_unresolved:
            # Do not present an award-first cabin default when the user has supplied
            # an invalid intent that could instead mean a cash-only search.
            brief.provenance["cabin"] = "unresolved"
        else:
            brief.cabin = "economy" if brief.intent == "cash_only" else DEFAULT_CABIN
            brief.provenance["cabin"] = "default"
            brief.assumptions.append(
                "Cabin defaults to economy for cash-only research."
                if brief.intent == "cash_only"
                else "Cabin defaults to business for award research."
            )
    if "passengers" not in brief.provenance:
        brief.passengers = DEFAULT_PASSENGERS
        brief.provenance["passengers"] = "default"
        brief.assumptions.append("Passenger count defaults to 1.")
    if "programs" not in brief.provenance:
        if intent_unresolved:
            brief.provenance["programs"] = "unresolved"
        else:
            brief.provenance["programs"] = "default"
            brief.assumptions.append("All supported Seats.aero redemption programs are in scope.")
    if "stops" not in brief.provenance:
        brief.provenance["stops"] = "default"
        brief.assumptions.append("Connections are allowed; known nonstop options are preferred in each local ranking.")
    if "transfer_sources" not in brief.provenance:
        # Award availability is useful without a transfer profile. Never silently
        # inject a specific issuer or currency into another user's research.
        brief.provenance["transfer_sources"] = "not_configured"


def _parse_structured_brief(data: Mapping[str, Any], programs: Mapping[str, str]) -> ResearchBrief:
    brief = ResearchBrief(input_format="json")
    values: dict[str, Any] = dict(data)
    # Also accept the compact controller shape used in AI-facing workflows.
    passengers_value = values.get("passengers")
    if isinstance(passengers_value, Mapping):
        values["passenger_count"] = passengers_value.get("count")
    cabin_value = values.get("cabin")
    if isinstance(cabin_value, Mapping):
        values["cabin"] = cabin_value.get("primary")
    points_value = values.get("points")
    if isinstance(points_value, Mapping):
        values.setdefault("programs", points_value.get("programs"))
        values.setdefault("max_points", points_value.get("max_points"))
    legs = values.get("legs")
    if legs is not None:
        if not isinstance(legs, list) or len(legs) != 1 or not isinstance(legs[0], Mapping):
            _append_issue(brief, "legs", "This MVP researches one one-way leg. Provide exactly one leg, or research each return/multi-city leg separately.")
        else:
            leg = legs[0]
            for key, value in leg.items():
                values.setdefault(key, value)
            departure = leg.get("departure")
            if isinstance(departure, Mapping):
                values.setdefault("start_date", departure.get("start") or departure.get("date"))
                values.setdefault("end_date", departure.get("end"))

    journey = str(values.get("journey") or values.get("trip_type") or "one_way").strip().lower().replace("-", "_")
    if journey in {"roundtrip", "round_trip", "return", "multi_city", "multicity", "open_jaw"} or values.get("return_date"):
        brief.journey = "round_trip" if journey in {"roundtrip", "round_trip", "return"} or values.get("return_date") else "multi_city"
        _append_issue(brief, "return_leg", "This MVP searches one-way only. Provide each outbound/return leg separately with its own airports and dates.")
    elif journey not in {"", "oneway", "one_way"}:
        _append_issue(brief, "journey", "Use journey: one_way, or provide one leg at a time.")
    else:
        brief.provenance["journey"] = "user" if "journey" in values or "trip_type" in values else "default"

    intent = str(values.get("intent") or "award_first").strip().lower().replace("-", "_")
    if intent in {"cash", "cash_only"}:
        brief.intent = "cash_only"
    elif intent in {"award", "award_only"}:
        brief.intent = "award_only"
    elif intent in {"", "award_first", "points"}:
        brief.intent = "award_first"
    else:
        _append_issue(brief, "intent", "Use intent: award_first, award_only, or cash_only.")
    if "intent" not in brief.provenance:
        brief.provenance["intent"] = "user" if "intent" in values else "default"

    origin_value = values.get("origins", values.get("origin", values.get("from")))
    destination_value = values.get("destinations", values.get("destination", values.get("to")))
    origins = _normalize_airports(origin_value)
    destinations = _normalize_airports(destination_value)
    if origins is None:
        _append_issue(brief, "origin", "Provide one or more 3-letter origin IATA codes (for example, SFO).")
    else:
        brief.origins = origins
        brief.provenance["origin"] = "user"
    if destinations is None:
        _append_issue(brief, "destination", "Provide one or more 3-letter destination IATA codes (for example, CDG).")
    else:
        brief.destinations = destinations
        brief.provenance["destination"] = "user"
    if origins and destinations and set(origins) & set(destinations):
        _append_issue(brief, "route", "Origin and destination airport lists must not overlap.")

    departure = values.get("departure")
    if isinstance(departure, Mapping):
        start_value = departure.get("start") or departure.get("date")
        end_value = departure.get("end")
    else:
        start_value = values.get("start_date", values.get("departure_date", values.get("date")))
        end_value = values.get("end_date")
    _set_dates(brief, start_value, end_value, "user")

    if "cabin" in values or "cabins" in values:
        cabin = _normalize_cabin(values.get("cabin", values.get("cabins")))
        if cabin is None:
            _append_issue(brief, "cabin", "Use one cabin: economy, premium, business, or first.")
        else:
            brief.cabin = cabin
            brief.provenance["cabin"] = "user"

    passenger_value = values.get("passenger_count", values.get("passengers", values.get("seats")))
    if passenger_value is not None:
        passenger_count = _positive_int(passenger_value)
        if passenger_count is None or passenger_count > 9:
            _append_issue(brief, "passengers", "Provide a passenger count from 1 through 9.")
        else:
            brief.passengers = passenger_count
            brief.provenance["passengers"] = "user"

    program_value = values.get("programs", values.get("points_programs"))
    if program_value is not None:
        selected_programs = _normalize_programs(program_value, programs)
        if selected_programs is None:
            _append_issue(brief, "programs", "Use supported Seats.aero redemption program names or source IDs; run './flight programs' for the list.")
        else:
            brief.programs = selected_programs
            brief.provenance["programs"] = "user"

    transfer_value: Any = None
    for key in ("transfer_sources", "transfer_profiles", "point_sources"):
        if key in values:
            transfer_value = values[key]
            break
    if transfer_value is not None:
        transfer_profiles, transfer_error = parse_transfer_profiles(transfer_value, programs)
        if transfer_error or transfer_profiles is None:
            _append_issue(brief, "transfer_sources", transfer_error or "The transfer-source profile is invalid.")
        else:
            brief.transfer_profiles = transfer_profiles
            brief.provenance["transfer_sources"] = "user"

    stop_values: list[bool] = []
    stop_input_invalid = False
    stops_value = values.get("stops")
    if stops_value is not None:
        if not isinstance(stops_value, Mapping):
            stop_input_invalid = True
        else:
            if "preference" in stops_value:
                preference = stops_value.get("preference")
                if isinstance(preference, str):
                    normalized_preference = preference.strip().lower().replace("-", "_")
                    preference_values = {
                        "nonstop_only": True,
                        "direct_only": True,
                        "prefer_nonstop_allow_connections": False,
                        "prefer_nonstop": False,
                        "allow_connections": False,
                    }
                    if normalized_preference in preference_values:
                        stop_values.append(preference_values[normalized_preference])
                    else:
                        stop_input_invalid = True
                else:
                    stop_input_invalid = True
            if "direct_only" in stops_value:
                nested_direct = _normalise_bool(stops_value.get("direct_only"))
                if nested_direct is None:
                    stop_input_invalid = True
                else:
                    stop_values.append(nested_direct)

    for key in ("direct_only", "nonstop", "direct"):
        if key not in values or values[key] is None:
            continue
        direct_value = _normalise_bool(values[key])
        if direct_value is None:
            stop_input_invalid = True
        else:
            stop_values.append(direct_value)

    if stop_input_invalid:
        _append_issue(
            brief,
            "stops",
            "Use stops.preference: prefer_nonstop_allow_connections or nonstop_only, or direct_only: true/false.",
        )
    elif stop_values:
        if len(set(stop_values)) != 1:
            _append_issue(brief, "stops", "Conflicting stop preferences were supplied; choose one preference.")
        else:
            brief.direct_only = stop_values[0]
            brief.provenance["stops"] = "user"

    max_points_value = values.get("max_points")
    if max_points_value is not None:
        maximum = _positive_int(max_points_value)
        if maximum is None:
            _append_issue(brief, "max_points", "Provide max_points as a positive whole number.")
        else:
            brief.max_points = maximum
            brief.provenance["max_points"] = "user"

    _apply_common_defaults(brief)
    brief.follow_up_fields = _unique_follow_ups(brief.follow_up_fields)
    return brief


def _text_programs(text: str, programs: Mapping[str, str]) -> tuple[str, ...]:
    lowered = text.lower()
    found: list[str] = []
    for source, name in programs.items():
        aliases = [source.replace("_", " "), name.lower()]
        # A few short, unambiguous forms make a brief less verbose.
        aliases.extend({
            "aeroplan": ["air canada"],
            "flyingblue": ["flying blue"],
            "virginatlantic": ["virgin atlantic"],
            "jetblue": ["jet blue"],
        }.get(source, []))
        if any(re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", lowered) for alias in aliases):
            found.append(source)
    return tuple(dict.fromkeys(found))


def _text_transfer_profiles(text: str) -> tuple[TransferProfile, ...]:
    lowered = text.lower()
    found: list[TransferProfile] = []
    for profile_id, profile in BUILTIN_TRANSFER_PROFILES.items():
        aliases = [profile_id.replace("_", " "), profile.name.lower()]
        aliases.extend(alias for alias, target in BUILTIN_TRANSFER_ALIASES.items() if target == profile_id)
        if any(re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", lowered) for alias in aliases):
            found.append(profile)
    return tuple({profile.id: profile for profile in found}.values())


def _parse_text_brief(text: str, programs: Mapping[str, str]) -> ResearchBrief:
    brief = ResearchBrief(input_format="text")
    normalized = " ".join(text.strip().split())
    lowered = normalized.lower()

    return_or_multicity_requested = bool(re.search(
        r"\b(round[ -]?trip|return(?:ing)?|back\s+on|multi[ -]?city|open[ -]?jaw)\b", lowered
    ))
    if return_or_multicity_requested:
        brief.journey = "round_trip"
        _append_issue(brief, "return_leg", "This MVP searches one-way only. Provide each outbound/return leg separately with its own airports and dates.")
    else:
        brief.provenance["journey"] = "default"

    if re.search(r"\bcash[ -]?only\b", lowered):
        brief.intent = "cash_only"
        brief.provenance["intent"] = "user"
    elif re.search(r"\b(?:award|points)[ -]?only\b", lowered):
        brief.intent = "award_only"
        brief.provenance["intent"] = "user"
    else:
        brief.provenance["intent"] = "default"

    airport_group = r"([A-Za-z]{3}(?:\s*,\s*[A-Za-z]{3})*)"
    # A bare route must use uppercase IATA-like tokens. Lowercase tokens are
    # accepted only after an explicit "from", which avoids treating prose such
    # as "fly to LAX" as a fabricated FLY airport.
    route_match = re.search(
        rf"\bfrom\s+{airport_group}\b\s*(?:\bto\b|->)\s*\b{airport_group}\b",
        normalized,
        re.IGNORECASE,
    )
    if route_match is None:
        uppercase_group = r"([A-Z]{3}(?:\s*,\s*[A-Z]{3})*)"
        route_match = re.search(
            rf"\b{uppercase_group}\b\s*(?:\b[tT][oO]\b|->)\s*\b{uppercase_group}\b",
            normalized,
        )
    if route_match:
        origins = _normalize_airports(route_match.group(1))
        destinations = _normalize_airports(route_match.group(2))
        if origins and destinations:
            brief.origins = origins
            brief.destinations = destinations
            brief.provenance["origin"] = "user"
            brief.provenance["destination"] = "user"
            if set(origins) & set(destinations):
                _append_issue(brief, "route", "Origin and destination airport lists must not overlap.")
    else:
        codes = re.findall(r"\b[A-Za-z]{3}\b", normalized)
        if codes:
            _append_issue(brief, "route", "State the route as 'SFO to CDG' so origin and destination are unambiguous.")
        _append_issue(brief, "origin", "Provide a 3-letter origin IATA code (for example, SFO).")
        _append_issue(brief, "destination", "Provide a 3-letter destination IATA code (for example, CDG).")

    date_strings = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", normalized)
    if len(date_strings) == 0:
        _append_issue(brief, "departure", "Provide a departure date or date range in YYYY-MM-DD format.")
    elif len(date_strings) > 2:
        _append_issue(brief, "departure", "Provide one departure date or one start/end range; research return legs separately.")
    else:
        # When return language is present, the second date is not silently repurposed
        # as an outbound-flexibility range.
        end_date = None if return_or_multicity_requested else (date_strings[1] if len(date_strings) == 2 else None)
        _set_dates(brief, date_strings[0], end_date, "user")

    cabin_matches: set[str] = set()
    for alias, cabin in sorted(CABIN_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])", lowered):
            cabin_matches.add(cabin)
    if len(cabin_matches) == 1:
        brief.cabin = next(iter(cabin_matches))
        brief.provenance["cabin"] = "user"
    elif len(cabin_matches) > 1:
        _append_issue(brief, "cabin", "Specify one cabin: economy, premium, business, or first.")

    count_token = r"(?:[+-]?\d+|zero|one|two|three|four|five|six|seven|eight|nine|ten)"
    passenger_patterns = (
        rf"(?<![a-z0-9+-])(?P<count>{count_token})\s*(?:passengers?|travelers?|travellers?|pax|seats?|people)\b",
        rf"\b(?:family|group|party)\s+of\s+(?P<count>{count_token})\b",
    )
    passenger_tokens = [
        match.group("count")
        for pattern in passenger_patterns
        for match in re.finditer(pattern, lowered)
    ]
    parsed_counts = [_parse_text_count(token) for token in passenger_tokens]
    valid_counts = [count for count in parsed_counts if count is not None and 1 <= count <= 9]
    if passenger_tokens and (any(count is None or count < 1 or count > 9 for count in parsed_counts)):
        _append_issue(brief, "passengers", "Provide a passenger count from 1 through 9.")
    elif len(set(valid_counts)) == 1:
        brief.passengers = valid_counts[0]
        brief.provenance["passengers"] = "user"
    elif len(set(valid_counts)) > 1:
        _append_issue(brief, "passengers", "Specify one passenger count from 1 through 9.")
    elif re.search(r"\b(we|us|our|family|group|party|kids|children)\b", lowered):
        _append_issue(brief, "passengers", "State how many passengers are traveling (1 through 9).")

    if re.search(r"\b(?:non[ -]?stop|direct)\s+only\b", lowered):
        brief.direct_only = True
        brief.provenance["stops"] = "user"

    cap_prefix = r"(?:under|below|max(?:imum)?(?:\s+of)?|at\s+most)"
    point_match = re.search(
        rf"\b{cap_prefix}\s*(?P<limit>[+-]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)(?:k)?)\s*(?:points?|miles?)\b",
        lowered,
    )
    cap_context = re.search(rf"\b{cap_prefix}\b[^.?!;]*\b(?:points?|miles?)\b", lowered)
    if point_match:
        maximum = _parse_text_point_limit(point_match.group("limit"))
        if maximum is None:
            _append_issue(brief, "max_points", "Provide a positive whole-number point or mile cap.")
        else:
            brief.max_points = maximum
            brief.provenance["max_points"] = "user"
    elif cap_context:
        _append_issue(brief, "max_points", "Provide a positive whole-number point or mile cap.")

    selected_programs = _text_programs(normalized, programs)
    if selected_programs:
        brief.programs = selected_programs
        brief.provenance["programs"] = "user"

    transfer_profiles = _text_transfer_profiles(normalized)
    if transfer_profiles:
        brief.transfer_profiles = transfer_profiles
        brief.provenance["transfer_sources"] = "user"

    _apply_common_defaults(brief)
    brief.follow_up_fields = _unique_follow_ups(brief.follow_up_fields)
    return brief


def parse_trip_brief(raw: str | Mapping[str, Any], programs: Mapping[str, str]) -> ResearchBrief:
    """Parse JSON or a deliberately small, deterministic textual brief.

    City-name resolution, relative dates, and unlabelled airport-code pairs are left as
    follow-ups instead of guessed.  This is intended for an AI/controller to surface to
    a user before an API request is made.
    """

    if isinstance(raw, Mapping):
        return _parse_structured_brief(raw, programs)
    if not isinstance(raw, str) or not raw.strip():
        brief = ResearchBrief()
        _append_issue(brief, "brief", "Provide a JSON trip brief or text such as 'SFO to CDG 2026-09-01 business 2 passengers'.")
        _apply_common_defaults(brief)
        return brief
    text = raw.strip()
    if text.startswith("{") or text.startswith("["):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            brief = ResearchBrief(input_format="json")
            _append_issue(brief, "brief", "The JSON trip brief is invalid. Supply one JSON object with origin, destination, and departure_date.")
            _apply_common_defaults(brief)
            return brief
        if not isinstance(decoded, Mapping):
            brief = ResearchBrief(input_format="json")
            _append_issue(brief, "brief", "The JSON trip brief must be an object, not a list or scalar.")
            _apply_common_defaults(brief)
            return brief
        return _parse_structured_brief(decoded, programs)
    return _parse_text_brief(text, programs)


def google_flights_handoff(brief: ResearchBrief) -> dict[str, Any]:
    """Build a browser handoff only; this code never contacts or scrapes Google."""

    unresolved = {
        field for field in ("origin", "destination", "departure", "cabin", "passengers")
        if brief.provenance.get(field) == "unresolved"
    }
    if not brief.origins or not brief.destinations or not brief.start_date or unresolved:
        return {
            "provider": "google_flights",
            "status": "needs_trip_fields",
            "mode": "manual_handoff",
            "scraped": False,
            "live_fares_obtained": False,
            "reason": "A resolved route, departure date, cabin, and passenger count are required before a Google Flights handoff can be formed.",
        }
    route = f"{','.join(brief.origins)} to {','.join(brief.destinations)}"
    dates = brief.start_date if brief.start_date == brief.end_date else f"{brief.start_date} to {brief.end_date}"
    query_text = (
        f"Flights from {route} departing {dates}, {brief.cabin}, "
        f"{brief.passengers} passenger{'s' if brief.passengers != 1 else ''}"
    )
    url = GOOGLE_FLIGHTS_URL + "?" + urllib.parse.urlencode({"q": query_text})
    return {
        "provider": "google_flights",
        "status": "ready_for_browser",
        "mode": "manual_handoff",
        "url": url,
        "query_text": query_text,
        "scraped": False,
        "live_fares_obtained": False,
        "source_confidence": "manual_pending",
        "instructions": [
            "Open the URL in a browser and enter or confirm the visible route, dates, cabin, and passenger count.",
            "This CLI does not scrape Google Flights and does not receive live fare data from Google.",
            "Optionally import a user-observed cash quote with itinerary and fare-inclusion evidence for a guarded CPP comparison.",
        ],
    }


def _decimal_to_cents(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0:
            return None
        cents = int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, OverflowError):
        return None
    return cents if cents > 0 else None


def _normalise_cash_scope(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace("-", "_")
    return normalized if normalized in {"total", "per_passenger"} else None


def _manual_source_confidence(value: Any) -> str:
    """Manual imports cannot elevate themselves to an API/live-fare claim."""
    normalized = str(value or "manual_unverified").strip().lower().replace("-", "_")
    return normalized if normalized in {"manual_verified", "manual_unverified"} else "manual_unverified"


def _normalise_observed_at(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        observed_at = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if observed_at.tzinfo is None:
        return None
    return observed_at.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _normalise_evidence_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    url = value.strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return url


def _normalise_itinerary_evidence(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, (list, tuple)):
        values = [str(item).strip() for item in value if str(item).strip()]
        if values:
            return ", ".join(values)
    return None


def parse_manual_cash_quotes(payloads: Sequence[Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Validate user-supplied cash observations without representing them as live data."""

    quotes: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []
    ordinal = 0
    for payload in payloads:
        objects: Any = payload.get("quotes") if isinstance(payload, Mapping) and "quotes" in payload else payload
        if isinstance(objects, Mapping):
            objects = [objects]
        if not isinstance(objects, list):
            issues.append({"field": "cash_quote", "reason": "A cash-quote import must be a JSON object, a list, or an object with a quotes list."})
            continue
        for obj in objects:
            ordinal += 1
            if not isinstance(obj, Mapping):
                issues.append({"field": "cash_quote", "reason": f"Cash quote #{ordinal} must be a JSON object."})
                continue
            cents = obj.get("total_cents")
            if cents is not None:
                cents = _positive_int(cents)
            else:
                cents = _decimal_to_cents(obj.get("total", obj.get("amount")))
            currency = str(obj.get("currency") or "").upper()
            scope = _normalise_cash_scope(obj.get("amount_scope"))
            if cents is None or not re.fullmatch(r"[A-Z]{3}", currency) or scope is None:
                issues.append({
                    "field": "cash_quote",
                    "reason": f"Cash quote #{ordinal} needs a positive total/total_cents, ISO currency, and amount_scope ('total' or 'per_passenger').",
                })
                continue
            origin = _normalize_airports(obj.get("origin"))
            destination = _normalize_airports(obj.get("destination"))
            # A quote is for one itinerary, so a list is intentionally not accepted here.
            if origin is not None and len(origin) != 1:
                origin = None
            if destination is not None and len(destination) != 1:
                destination = None
            departure_date = _parse_iso_date(obj.get("departure_date", obj.get("date")))
            cabin = _normalize_cabin(obj.get("cabin"))
            passengers = _positive_int(obj.get("passengers", obj.get("passenger_count")))
            observed_at = _normalise_observed_at(obj.get("observed_at"))
            booking_url = _normalise_evidence_url(obj.get("booking_url"))
            itinerary_evidence = _normalise_itinerary_evidence(
                obj.get("itinerary_evidence", obj.get("flight_numbers"))
            )
            evidence_issues: list[str] = []
            if observed_at is None:
                evidence_issues.append("a timezone-aware observed_at timestamp")
            if booking_url is None and itinerary_evidence is None:
                evidence_issues.append("a valid booking/search URL or itinerary_evidence")
            quote = {
                "id": str(obj.get("id") or f"manual-cash-{ordinal}"),
                "provider": str(obj.get("provider") or "manual_import"),
                "amount_cents": cents,
                "currency": currency,
                "amount_scope": scope,
                "origin": origin[0] if origin else None,
                "destination": destination[0] if destination else None,
                "departure_date": departure_date,
                "cabin": cabin,
                "passengers": passengers,
                "same_itinerary": obj.get("same_itinerary") is True,
                "fare_inclusions_match": obj.get("fare_inclusions_match") is True,
                "fare_inclusions": obj.get("fare_inclusions"),
                "observed_at": observed_at,
                "booking_url": booking_url,
                "itinerary_evidence": itinerary_evidence,
                "evidence_issues": evidence_issues,
                "evidence_origin": "user_import",
                # This is a user assertion, never an assertion made by this CLI.
                # Never allow an imported label to turn into a live/API-fare claim.
                "source_confidence": _manual_source_confidence(obj.get("source_confidence")),
            }
            if evidence_issues:
                issues.append({
                    "field": "cash_quote",
                    "reason": f"Cash quote #{ordinal} needs " + " and ".join(evidence_issues) + " before CPP can be calculated.",
                })
            quotes.append(quote)
    return quotes, _unique_follow_ups(issues)


def _quote_total_cents(quote: Mapping[str, Any], passengers: int) -> int:
    amount = int(quote["amount_cents"])
    return amount if quote["amount_scope"] == "total" else amount * passengers


def _quote_matches_candidate(quote: Mapping[str, Any], candidate: Mapping[str, Any], passengers: int) -> tuple[bool, str | None]:
    required = {
        "origin": candidate.get("origin"),
        "destination": candidate.get("destination"),
        "departure_date": candidate.get("date"),
        "cabin": candidate.get("cabin"),
        "passengers": passengers,
    }
    for key, expected in required.items():
        if quote.get(key) != expected:
            return False, f"cash quote {key} does not match the award itinerary"
    if not quote.get("same_itinerary"):
        return False, "cash quote is not explicitly marked as the same itinerary"
    if not quote.get("fare_inclusions_match"):
        return False, "cash quote does not confirm comparable fare inclusions"
    if not quote.get("observed_at"):
        return False, "cash quote needs a valid, timezone-aware observed_at timestamp"
    if not quote.get("booking_url") and not quote.get("itinerary_evidence"):
        return False, "cash quote needs a valid booking/search URL or itinerary evidence"
    return True, None


def compare_award_to_cash(
    candidate: Mapping[str, Any],
    quotes: Sequence[Mapping[str, Any]],
    passengers: int,
    transfers: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return guarded point-source-specific CPP comparisons for one award.

    The denominator is the smallest configured transferable-point amount, including
    ratio and rounding, rather than treating an award mile and another point currency
    as interchangeable.
    """

    comparisons: list[dict[str, Any]] = []
    for quote in quotes:
        matches, reason = _quote_matches_candidate(quote, candidate, passengers)
        base = {
            "cash_quote_id": quote.get("id"),
            "cash_provider": quote.get("provider"),
            "cash_currency": quote.get("currency"),
            "cash_observed_at": quote.get("observed_at"),
            "cash_booking_url": quote.get("booking_url"),
            "cash_itinerary_evidence": quote.get("itinerary_evidence"),
            "cash_evidence_origin": quote.get("evidence_origin", "user_import"),
            "cash_source_confidence": quote.get("source_confidence", "manual_unverified"),
        }
        if not matches:
            comparisons.append({**base, "state": "not_comparable", "reason": reason})
            continue
        if candidate.get("kind") == "summary":
            comparisons.append({
                **base,
                "state": "not_comparable",
                "reason": "award is summary-only, so the exact itinerary cannot be compared to a cash quote",
            })
            continue
        points = candidate.get("points")
        taxes = candidate.get("taxes_cents")
        award_currency = candidate.get("taxes_currency")
        if points is None or int(points) <= 0:
            comparisons.append({**base, "state": "not_comparable", "reason": "award points are not reported"})
        elif taxes is None or not award_currency:
            comparisons.append({**base, "state": "not_comparable", "reason": "award taxes/fees and currency are not reported"})
        elif str(award_currency).upper() != quote["currency"]:
            comparisons.append({
                **base,
                "state": "not_comparable",
                "reason": f"cash is {quote['currency']} while award taxes are {award_currency}; no currency conversion is assumed",
            })
        else:
            direct_transfers = [
                transfer for transfer in transfers
                if transfer.get("status") == "direct_reference" and transfer.get("source_points_to_transfer")
            ]
            if not direct_transfers:
                comparisons.append({
                    **base,
                    "state": "not_comparable",
                    "reason": "no configured, directly usable transfer-source amount is available for this redemption program",
                })
                continue
            total_cash = _quote_total_cents(quote, passengers)
            total_points = int(points) * passengers
            total_taxes = int(taxes) * passengers
            for transfer in direct_transfers:
                source_points = int(transfer["source_points_to_transfer"])
                # Cash values are stored in cents, so dividing by transferable points
                # yields cents per point (the usual CPP unit).
                cpp = (total_cash - total_taxes) / source_points
                comparisons.append({
                    **base,
                    "state": "user_asserted_comparable",
                    "comparison_confidence": "user_asserted",
                    "verification_required": True,
                    "point_source": transfer["point_source"],
                    "point_source_name": transfer["point_source_name"],
                    "source_points_transferred": source_points,
                    "recipient_points_received": transfer.get("recipient_points_received"),
                    "award_points_total": total_points,
                    "award_taxes_total_cents": total_taxes,
                    "cpp": round(cpp, 3),
                    "formula": "(cash total in major currency units - award taxes/fees) / transferable source points × 100 (reported as cents per point)",
                    "note": "CPP is based on user-asserted manual cash evidence and still requires itinerary/fare verification.",
                })
    return comparisons


def _transfer_requirement(
    profile: TransferProfile, rule: TransferRule, required_recipient_points: int | None
) -> dict[str, Any]:
    base = {
        "point_source": profile.id,
        "point_source_name": profile.name,
        "recipient_program": rule.recipient_name,
        "ratio": f"1,000 source points : {rule.recipient_per_1000_source_points:,} recipient points",
        "minimum_source_points": rule.minimum_source_points,
        "source_increment": rule.source_increment,
        "reference_version": profile.reference_version,
        "as_of": profile.as_of,
        "source_url": rule.source_url or profile.source_url,
        "transfer_time_note": rule.transfer_time_note,
        "verification_required": True,
    }
    if rule.requires_manual_confirmation:
        return {
            **base,
            "status": "requires_manual_confirmation",
            "reason": "The configured point-source target is not automatically interchangeable with the Seats.aero redemption program.",
            "source_points_to_transfer": None,
            "recipient_points_received": None,
        }
    if required_recipient_points is None or required_recipient_points <= 0:
        return {
            **base,
            "status": "direct_reference",
            "reason": "Award points are not reported, so a transfer amount cannot be calculated.",
            "source_points_to_transfer": None,
            "recipient_points_received": None,
        }
    increments = math.ceil(required_recipient_points / rule.recipient_per_1000_source_points)
    source_points = max(rule.minimum_source_points, increments * rule.source_increment)
    source_points = math.ceil(source_points / rule.source_increment) * rule.source_increment
    recipient_points = (source_points // rule.source_increment) * rule.recipient_per_1000_source_points
    return {
        **base,
        "status": "direct_reference",
        "source_points_to_transfer": source_points,
        "recipient_points_received": recipient_points,
        "assumption": "Assumes no existing recipient-program balance and no transfer bonus.",
    }


def transfer_options(
    program: str | None,
    required_recipient_points: int | None,
    profiles: Sequence[TransferProfile] = (),
) -> list[dict[str, Any]]:
    """Map selected point sources to rules or deliberately non-claiming gaps."""

    result: list[dict[str, Any]] = []
    for profile in profiles:
        rule = next((item for item in profile.rules if item.program == program), None)
        if rule:
            result.append(_transfer_requirement(profile, rule, required_recipient_points))
        else:
            result.append({
                "point_source": profile.id,
                "point_source_name": profile.name,
                "status": "not_configured",
                "source_points_to_transfer": None,
                "recipient_points_received": None,
                "verification_required": True,
                "reference_version": profile.reference_version,
                "as_of": profile.as_of,
                "source_url": profile.source_url,
                "reason": "No direct-transfer rule is configured for this redemption program; this is not proof that no route exists.",
            })
    return result


def _seat_confidence(candidate: Mapping[str, Any]) -> str:
    try:
        return "reported" if candidate.get("seats") is not None and int(candidate["seats"]) > 0 else "unknown"
    except (TypeError, ValueError):
        return "unknown"


def _candidate_sort_key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    """Prefer known nonstop options without turning tax currencies into a common value."""

    points = candidate.get("points")
    stops = candidate.get("stops")
    if not isinstance(stops, int) or stops < 0:
        stops = 0 if candidate.get("direct") is True else 99
    return (
        stops,
        points if isinstance(points, int) and points > 0 else 10**12,
        0 if _seat_confidence(candidate) == "reported" else 1,
        0 if candidate.get("kind") == "trip" else 1,
        candidate.get("date") or "9999-99-99",
        str(candidate.get("id") or ""),
    )


def group_award_recommendations(
    candidates: Sequence[Mapping[str, Any]],
    brief: ResearchBrief,
    program_names: Mapping[str, str],
    cash_quotes: Sequence[Mapping[str, Any]],
    limit_per_bucket: int,
    transfer_profiles: Sequence[TransferProfile] = (),
) -> list[dict[str, Any]]:
    """Preserve every redemption-program source and isolate non-comparable tax currencies."""

    by_program: dict[str, list[Mapping[str, Any]]] = {}
    for candidate in candidates:
        program = str(candidate.get("program") or "unknown").lower()
        by_program.setdefault(program, []).append(candidate)

    groups: list[dict[str, Any]] = []
    for program in sorted(by_program, key=lambda item: (program_names.get(item, item).lower(), item)):
        by_currency: dict[str, list[Mapping[str, Any]]] = {}
        for candidate in by_program[program]:
            currency = str(candidate.get("taxes_currency") or "unknown").upper()
            by_currency.setdefault(currency, []).append(candidate)
        buckets: list[dict[str, Any]] = []
        for currency in sorted(by_currency, key=lambda item: (item == "UNKNOWN", item)):
            recommendations: list[dict[str, Any]] = []
            for rank, original in enumerate(sorted(by_currency[currency], key=_candidate_sort_key)[:limit_per_bucket], 1):
                candidate = dict(original)
                # rank_candidates() is reused for filtering, but its legacy composite
                # score compares tax cents. Never expose that score in research output.
                candidate.pop("score", None)
                award_points = candidate.get("points")
                total_points = int(award_points) * brief.passengers if isinstance(award_points, int) and award_points > 0 else None
                candidate["research_evidence"] = {
                    "provider": "seats.aero",
                    "provider_mode": "cached_availability",
                    "redemption_program": program,
                    "source_updated_at": candidate.get("source_updated_at"),
                    "fetched_at": candidate.get("fetched_at"),
                    "detail_level": "flight_level" if candidate.get("kind") == "trip" else "summary_only",
                    "seat_confidence": _seat_confidence(candidate),
                }
                candidate["party_award_points"] = total_points
                candidate["transfer_access"] = transfer_options(program, total_points, transfer_profiles)
                candidate["cash_comparison"] = compare_award_to_cash(
                    candidate, cash_quotes, brief.passengers, candidate["transfer_access"]
                )
                candidate["rank_within_tax_currency"] = rank
                recommendations.append(candidate)
            buckets.append({
                "tax_currency": None if currency == "UNKNOWN" else currency,
                "ranking_scope": "Within this redemption program and one tax-currency bucket; known nonstop options first, then award points, seat confidence, and detail level.",
                "recommendations": recommendations,
            })
        groups.append({
            "program": program,
            "program_name": program_names.get(program, program),
            "provider": "seats.aero",
            "ranking_scope": "No cross-program or cross-currency winner is claimed.",
            "tax_currency_buckets": buckets,
        })
    return groups


def comparison_summary(groups: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    user_asserted_comparable = 0
    unavailable_reasons: dict[str, int] = {}
    for group in groups:
        for bucket in group.get("tax_currency_buckets", []):
            for recommendation in bucket.get("recommendations", []):
                comparisons = recommendation.get("cash_comparison", [])
                if not comparisons:
                    unavailable_reasons["No manually imported cash quote matches this award itinerary."] = (
                        unavailable_reasons.get("No manually imported cash quote matches this award itinerary.", 0) + 1
                    )
                for comparison in comparisons:
                    if comparison.get("state") in {"comparable", "user_asserted_comparable"}:
                        user_asserted_comparable += 1
                    else:
                        reason = str(comparison.get("reason") or "Cash comparison is not comparable.")
                        unavailable_reasons[reason] = unavailable_reasons.get(reason, 0) + 1
    return {
        "method": "CPP appears only for user-asserted manual cash evidence with a matching route, date, cabin, passenger count, fare inclusions, timestamp, evidence reference, and tax currency.",
        "comparable_pair_count": user_asserted_comparable,
        "user_asserted_comparable_pair_count": user_asserted_comparable,
        "not_comparable_reasons": [
            {"reason": reason, "count": count}
            for reason, count in sorted(unavailable_reasons.items())
        ],
    }


def manual_cash_quote_schema() -> dict[str, Any]:
    return {
        "provider": "google_flights",
        "total": 1234.56,
        "currency": "USD",
        "amount_scope": "total",
        "origin": "SFO",
        "destination": "CDG",
        "departure_date": "2026-09-01",
        "cabin": "business",
        "passengers": 1,
        "same_itinerary": True,
        "fare_inclusions_match": True,
        "fare_inclusions": "Describe baggage/fare-rule comparison",
        "observed_at": "2026-01-01T12:00:00Z",
        "booking_url": "https://www.google.com/travel/flights",
        "itinerary_evidence": "Flight numbers/schedule or another user-observed itinerary reference",
    }
