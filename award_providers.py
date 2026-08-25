"""Provider-neutral award-offer contracts and adapter interfaces.

This module is deliberately dependency-free and does not perform network requests or
load credentials.  Providers return normalized :class:`AwardOffer` objects; ranking,
transfer comparisons, and reporting consume the common candidate representation
instead of provider payloads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import copy
import datetime as dt
import re
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable
import urllib.parse


AWARD_OFFER_SCHEMA_VERSION = 1
CABINS = ("economy", "premium", "business", "first")
_PROVIDER_ID_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}")
_PROGRAM_ID_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_IATA_RE = re.compile(r"[A-Z]{3}")
_AVAILABILITY_MODES = {
    "manual_import",
    "cached_availability",
    "provider_live_offer",
    "hosted_licensed_api",
    "unknown",
}
_SEAT_CONFIDENCE = {"reported", "estimated", "unknown"}
_DETAIL_LEVELS = {"summary_only", "flight_level"}


class ProviderError(RuntimeError):
    """An adapter-facing failure that does not expose provider payloads."""


class AwardOfferValidationError(ValueError):
    """Raised only by the single-offer convenience validator."""


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _normalise_identifier(value: Any, pattern: re.Pattern[str]) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if pattern.fullmatch(normalized) else None


def normalize_provider_id(value: Any) -> str | None:
    """Return the registry form of a provider id without loading an adapter."""

    return _normalise_identifier(value, _PROVIDER_ID_RE)


def _normalise_airport(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    return normalized if _IATA_RE.fullmatch(normalized) else None


def _normalise_date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return None


def _normalise_timestamp(value: Any) -> str | None:
    """Return a UTC ISO timestamp only when an offset is supplied."""

    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _normalise_nonnegative_int(value: Any, *, allow_none: bool = True) -> int | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        result = value
    elif isinstance(value, float):
        if not value.is_integer():
            return None
        result = int(value)
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        result = int(value.strip())
    else:
        return None
    return result if result >= 0 else None


def _normalise_positive_int(value: Any, *, allow_none: bool = True) -> int | None:
    result = _normalise_nonnegative_int(value, allow_none=allow_none)
    return result if result is None or result > 0 else None


def _normalise_url(value: Any) -> str | None:
    """Return a public-safe http(s) URL without credentials, query, or fragment."""

    if not isinstance(value, str) or not value.strip():
        return None
    parsed = urllib.parse.urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    # Query parameters and fragments often contain booking/session identifiers. The
    # public normalized contract deliberately retains only a safe destination URL.
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path, "", ""))


def _normalise_cabin(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    aliases = {"premium_economy": "premium", "premium-economy": "premium", "pe": "premium"}
    normalized = aliases.get(value.strip().lower(), value.strip().lower())
    return normalized if normalized in CABINS else None


def _normalise_carrier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    return normalized if re.fullmatch(r"[A-Z0-9]{2,3}", normalized) else None


def _normalise_carriers(value: Any) -> list[str] | None:
    if value is None:
        return []
    if isinstance(value, str):
        raw_values = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple)):
        raw_values = list(value)
    else:
        return None
    carriers: list[str] = []
    for raw in raw_values:
        carrier = _normalise_carrier(raw)
        if carrier is None:
            return None
        if carrier not in carriers:
            carriers.append(carrier)
    return carriers


def _normalise_availability_mode(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    aliases = {
        "manual": "manual_import",
        "user_asserted": "manual_import",
        "cached": "cached_availability",
        "provider_cached": "cached_availability",
        "live": "provider_live_offer",
        "provider_live": "provider_live_offer",
    }
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in _AVAILABILITY_MODES else None


def _normalise_detail_level(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    aliases = {"summary": "summary_only", "itinerary": "flight_level", "offer": "flight_level"}
    normalized = value.strip().lower().replace("-", "_")
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in _DETAIL_LEVELS else None


def _safe_text(value: Any, *, max_length: int = 256) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > max_length or any(ord(char) < 32 for char in text):
        return None
    return text


def _normalise_booking_links(value: Any) -> tuple[list[dict[str, str]], str | None]:
    if value is None:
        return [], None
    if not isinstance(value, list):
        return [], "booking_links must be a list of http(s) URLs or {url, label} objects."
    links: list[dict[str, str]] = []
    for index, raw in enumerate(value, 1):
        if isinstance(raw, str):
            url = _normalise_url(raw)
            label = None
        elif isinstance(raw, Mapping):
            url = _normalise_url(raw.get("url"))
            label = _safe_text(raw.get("label"), max_length=120) if raw.get("label") is not None else None
        else:
            return [], f"booking_links item #{index} must be a URL or object."
        if url is None:
            return [], f"booking_links item #{index} needs a valid http(s) URL."
        entry = {"url": url}
        if label:
            entry["label"] = label
        if entry not in links:
            links.append(entry)
    return links, None


def _normalise_segments(value: Any) -> tuple[list[dict[str, Any]], str | None]:
    if value is None:
        return [], None
    if not isinstance(value, list):
        return [], "itinerary.segments must be a list."
    segments: list[dict[str, Any]] = []
    for index, raw in enumerate(value, 1):
        if not isinstance(raw, Mapping):
            return [], f"itinerary.segments item #{index} must be an object."
        origin = _normalise_airport(raw.get("origin")) if raw.get("origin") is not None else None
        destination = _normalise_airport(raw.get("destination")) if raw.get("destination") is not None else None
        if raw.get("origin") is not None and origin is None:
            return [], f"itinerary.segments item #{index} origin must be a 3-letter IATA code."
        if raw.get("destination") is not None and destination is None:
            return [], f"itinerary.segments item #{index} destination must be a 3-letter IATA code."
        operating = _normalise_carrier(raw.get("operating_carrier")) if raw.get("operating_carrier") is not None else None
        marketing = _normalise_carrier(raw.get("marketing_carrier")) if raw.get("marketing_carrier") is not None else None
        if raw.get("operating_carrier") is not None and operating is None:
            return [], f"itinerary.segments item #{index} operating_carrier must be a 2-3 character carrier code."
        if raw.get("marketing_carrier") is not None and marketing is None:
            return [], f"itinerary.segments item #{index} marketing_carrier must be a 2-3 character carrier code."
        segment: dict[str, Any] = {}
        if origin:
            segment["origin"] = origin
        if destination:
            segment["destination"] = destination
        for field_name in ("departs_at", "arrives_at", "flight_number", "aircraft", "fare_class"):
            if raw.get(field_name) is not None:
                text = _safe_text(raw.get(field_name), max_length=120)
                if text is None:
                    return [], f"itinerary.segments item #{index} {field_name} must be text."
                segment[field_name] = text
        if operating:
            segment["operating_carrier"] = operating
        if marketing:
            segment["marketing_carrier"] = marketing
        segments.append(segment)
    return segments, None


def _validate_flight_level_segments(
    segments: Sequence[Mapping[str, Any]], *, origin: str, destination: str, stops: int | None
) -> str | None:
    """Require an actual, route-consistent itinerary before calling an offer a trip."""

    if not segments:
        return "flight_level offers need at least one itinerary segment; use summary_only when schedule detail is absent."
    for index, segment in enumerate(segments, 1):
        segment_origin = segment.get("origin")
        segment_destination = segment.get("destination")
        if not isinstance(segment_origin, str) or not isinstance(segment_destination, str):
            return f"flight_level itinerary.segments item #{index} needs origin and destination."
        if not segment.get("departs_at") or not segment.get("arrives_at"):
            return f"flight_level itinerary.segments item #{index} needs departs_at and arrives_at schedule evidence."
        if segment_origin == segment_destination:
            return f"flight_level itinerary.segments item #{index} cannot have the same origin and destination."
        if index > 1 and segments[index - 2].get("destination") != segment_origin:
            return "flight_level itinerary.segments must connect in order."
    if segments[0].get("origin") != origin or segments[-1].get("destination") != destination:
        return "flight_level itinerary.segments must begin at itinerary.origin and end at itinerary.destination."
    derived_stops = len(segments) - 1
    if stops is not None and stops != derived_stops:
        return "itinerary.stops must match the number of flight-level itinerary segments."
    return None


def _provenance_state(mode: str, imported_manually: bool) -> str:
    if mode == "manual_import":
        return "user_asserted"
    if imported_manually:
        return f"{mode}_claimed_by_import"
    return mode


@dataclass(frozen=True)
class AwardOffer:
    """A validated, normalized award offer with provider-scoped identifiers."""

    payload: Mapping[str, Any]

    @property
    def provider_id(self) -> str:
        return str(self.payload["provider"]["id"])

    @property
    def provider_name(self) -> str:
        return str(self.payload["provider"]["name"])

    @property
    def program_id(self) -> str:
        return str(self.payload["redemption_program"]["id"])

    @property
    def program_name(self) -> str:
        return str(self.payload["redemption_program"]["name"])

    @property
    def offer_id(self) -> str:
        return str(self.payload["offer_id"])

    def to_dict(self) -> dict[str, Any]:
        """Return only normalized allowlisted fields, never a provider raw payload."""

        return copy.deepcopy(dict(self.payload))

    def to_candidate(self) -> dict[str, Any]:
        """Adapt the normalized contract to the provider-independent ranking input."""

        value = self.payload
        itinerary = value["itinerary"]
        evidence = value["evidence"]
        availability = value["seat_availability"]
        taxes = value["taxes"]
        segments = copy.deepcopy(list(itinerary.get("segments") or []))
        stops = itinerary.get("stops")
        if not isinstance(stops, int) or stops < 0:
            stops = len(segments) - 1 if segments else None
        seat_count = availability.get("count")
        # An estimated/unknown count must not make ranking claim usable inventory.
        seats = seat_count if availability.get("confidence") == "reported" else None
        operating_carriers = itinerary.get("operating_carriers") or []
        flight_numbers = [str(segment["flight_number"]) for segment in segments if segment.get("flight_number")]
        return {
            "kind": "trip" if value["detail_level"] == "flight_level" else "summary",
            "id": value["offer_id"],
            "availability_id": value["provider_offer_id"],
            "offer_id": value["offer_id"],
            "provider_offer_id": value["provider_offer_id"],
            "date": itinerary["departure_date"],
            "origin": itinerary["origin"],
            "destination": itinerary["destination"],
            "cabin": value["cabin"],
            "points": value["award"]["points"],
            "award_per_passenger": value["award"]["per_passenger"],
            "taxes_cents": taxes["cents"],
            "taxes_currency": taxes["currency"],
            "taxes_symbol": taxes["symbol"],
            "taxes_per_passenger": taxes.get("per_passenger"),
            "seats": seats,
            "reported_seat_count": seat_count,
            "seat_confidence": availability["confidence"],
            "stops": stops,
            "direct": stops == 0 if stops is not None else None,
            "duration_minutes": itinerary.get("duration_minutes"),
            "program": value["redemption_program"]["id"],
            "program_name": value["redemption_program"]["name"],
            "provider_program_id": value["redemption_program"].get("provider_program_id"),
            "provider_id": value["provider"]["id"],
            "provider_name": value["provider"]["name"],
            "provider_mode": evidence["availability_mode"],
            "provider_observation_state": evidence["observation_state"],
            "verification_status": evidence["verification_status"],
            "imported_manually": evidence["imported_manually"],
            "carriers": ",".join(operating_carriers) or None,
            "flight_numbers": ",".join(flight_numbers) or None,
            "departs_at": segments[0].get("departs_at") if segments else None,
            "arrives_at": segments[-1].get("arrives_at") if segments else None,
            "segments": segments,
            "booking_links": copy.deepcopy(list(value["booking_links"])),
            "source_updated_at": evidence.get("source_updated_at"),
            "fetched_at": evidence.get("fetched_at") or evidence.get("observed_at"),
            "award_offer": self.to_dict(),
        }


@dataclass(frozen=True)
class AwardSearchRequest:
    """Provider-neutral search intent passed to an adapter."""

    origins: tuple[str, ...]
    destinations: tuple[str, ...]
    start_date: str
    end_date: str
    cabin: str
    passengers: int
    program_ids: tuple[str, ...] = ()
    direct_only: bool = False
    max_points: int | None = None
    result_limit: int = 100
    cache_ttl_hours: float = 24.0


@dataclass(frozen=True)
class ProviderLimits:
    """Capability/approval boundary and optional adapter-specific fetch bounds."""

    supports_fetch: bool = False
    requires_credentials: bool = False
    network_access: bool = False
    requires_explicit_selection: bool = False
    max_airports_per_side: int | None = None
    max_date_span_days: int | None = None
    max_results: int | None = None
    strict_program_catalog: bool = False
    description: str = ""


@dataclass(frozen=True)
class AwardSearchSnapshot:
    """A provider response expressed entirely in the normalized contract."""

    provider_id: str
    provider_name: str
    offers: tuple[AwardOffer, ...] = ()
    status: str = "unavailable"
    coverage: Mapping[str, Any] = field(default_factory=dict)
    cache: Mapping[str, Any] | None = None
    can_suppress_fetch: bool = False
    fetch_supported: bool = False
    note: str | None = None


@runtime_checkable
class AwardProvider(Protocol):
    """Adapter seam for local imports, licensed APIs, or a hosted backend.

    Implementations must return ``AwardOffer`` values and must not make ranking or
    transfer-comparison decisions.  A future adapter can own its cache and transport
    behind these two methods without changing the report format.
    """

    id: str
    display_name: str
    program_catalog: Mapping[str, str]
    limits: ProviderLimits

    def find_cached(self, request: AwardSearchRequest) -> AwardSearchSnapshot:
        """Return local/cache data without contacting a provider."""

    def fetch(self, request: AwardSearchRequest) -> AwardSearchSnapshot:
        """Fetch only when the caller has explicitly approved this adapter."""


class ProviderRegistry:
    """Small explicit registry; no provider is selected implicitly by credentials."""

    def __init__(self, providers: Sequence[AwardProvider] = ()):
        self._providers: dict[str, AwardProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: AwardProvider) -> None:
        provider_id = _normalise_identifier(getattr(provider, "id", None), _PROVIDER_ID_RE)
        if provider_id is None:
            raise ProviderError("provider adapters need a lowercase provider id")
        if provider_id in self._providers:
            raise ProviderError(f"provider '{provider_id}' is already registered")
        self._providers[provider_id] = provider

    def get(self, provider_id: str) -> AwardProvider:
        normalized = _normalise_identifier(provider_id, _PROVIDER_ID_RE)
        if normalized is None or normalized not in self._providers:
            available = ", ".join(self.ids()) or "none"
            raise ProviderError(f"unknown research provider '{provider_id}'; choose one of: {available}")
        return self._providers[normalized]

    def ids(self) -> tuple[str, ...]:
        return tuple(self._providers)

    def manifest(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for provider in self._providers.values():
            limits = provider.limits
            result.append({
                "id": provider.id,
                "name": provider.display_name,
                "program_catalog_size": len(provider.program_catalog),
                "supports_fetch": limits.supports_fetch,
                "requires_credentials": limits.requires_credentials,
                "network_access": limits.network_access,
                "requires_explicit_selection": limits.requires_explicit_selection,
                "max_airports_per_side": limits.max_airports_per_side,
                "max_date_span_days": limits.max_date_span_days,
                "max_results": limits.max_results,
                "strict_program_catalog": limits.strict_program_catalog,
                "description": limits.description,
            })
        return result


# A descriptive alias makes the public seam easy to find without breaking callers
# that use the shorter name above.
AwardProviderRegistry = ProviderRegistry


def _normalise_offer(
    value: Mapping[str, Any],
    *,
    imported_manually: bool,
) -> tuple[AwardOffer | None, str | None]:
    if value.get("schema_version") != AWARD_OFFER_SCHEMA_VERSION:
        return None, f"schema_version must be {AWARD_OFFER_SCHEMA_VERSION}."

    provider_value = value.get("provider")
    if not isinstance(provider_value, Mapping):
        return None, "provider must be an object with id and name."
    provider_id = _normalise_identifier(provider_value.get("id"), _PROVIDER_ID_RE)
    provider_name = _safe_text(provider_value.get("name"), max_length=160)
    if provider_id is None or provider_name is None:
        return None, "provider.id must be a lowercase stable id and provider.name must be text."

    provider_offer_id = _safe_text(value.get("provider_offer_id"), max_length=256)
    raw_offer_id = _safe_text(value.get("offer_id"), max_length=320)
    if provider_offer_id is None or raw_offer_id is None:
        return None, "offer_id and provider_offer_id are required text identifiers."
    # A provider namespace prevents cross-provider collisions even when an imported
    # source reused a short opaque record ID.
    offer_id = raw_offer_id if raw_offer_id.startswith(provider_id + ":") else f"{provider_id}:{raw_offer_id}"

    program_value = value.get("redemption_program")
    if not isinstance(program_value, Mapping):
        return None, "redemption_program must be an object with id and name."
    program_id = _normalise_identifier(program_value.get("id"), _PROGRAM_ID_RE)
    program_name = _safe_text(program_value.get("name"), max_length=160)
    provider_program_id = _safe_text(program_value.get("provider_program_id"), max_length=160)
    if program_id is None or program_name is None or provider_program_id is None:
        return None, "redemption_program.id, name, and provider_program_id are required."

    itinerary_value = value.get("itinerary")
    if not isinstance(itinerary_value, Mapping):
        return None, "itinerary must be an object."
    origin = _normalise_airport(itinerary_value.get("origin"))
    destination = _normalise_airport(itinerary_value.get("destination"))
    departure_date = _normalise_date(itinerary_value.get("departure_date"))
    if origin is None or destination is None or origin == destination or departure_date is None:
        return None, "itinerary needs distinct 3-letter origin/destination codes and departure_date YYYY-MM-DD."
    if "segments" not in itinerary_value:
        return None, "itinerary.segments is required (use [] for a summary-only offer)."
    segments, segment_error = _normalise_segments(itinerary_value.get("segments"))
    if segment_error:
        return None, segment_error
    stops_raw = itinerary_value.get("stops")
    stops = _normalise_nonnegative_int(stops_raw) if stops_raw is not None else (len(segments) - 1 if segments else None)
    if stops_raw is not None and stops is None:
        return None, "itinerary.stops must be a non-negative whole number."
    duration_raw = itinerary_value.get("duration_minutes")
    duration = _normalise_positive_int(duration_raw) if duration_raw is not None else None
    if duration_raw is not None and duration is None:
        return None, "itinerary.duration_minutes must be a positive whole number."
    operating_carriers = _normalise_carriers(itinerary_value.get("operating_carriers"))
    marketing_carriers = _normalise_carriers(itinerary_value.get("marketing_carriers"))
    if operating_carriers is None or marketing_carriers is None:
        return None, "itinerary operating_carriers and marketing_carriers must contain 2-3 character carrier codes."

    cabin = _normalise_cabin(value.get("cabin"))
    if cabin is None:
        return None, "cabin must be economy, premium, business, or first."

    award_value = value.get("award")
    if not isinstance(award_value, Mapping):
        return None, "award must be an object with points and per_passenger."
    if "points" not in award_value:
        return None, "award.points is required."
    points_raw = award_value.get("points")
    points = _normalise_positive_int(points_raw, allow_none=False)
    if points is None:
        return None, "award.points must be a positive whole number."
    if not isinstance(award_value.get("per_passenger"), bool):
        return None, "award.per_passenger must be boolean."

    taxes_value = value.get("taxes", value.get("fees"))
    if not isinstance(taxes_value, Mapping):
        return None, "taxes (or fees) must be an object with cents, currency, and symbol."
    if "cents" not in taxes_value or "currency" not in taxes_value or "per_passenger" not in taxes_value:
        return None, "taxes.cents, taxes.currency, and taxes.per_passenger are required (use null only for unknown amounts/currency)."
    cents_raw = taxes_value.get("cents")
    cents = _normalise_nonnegative_int(cents_raw)
    if cents_raw is not None and cents is None:
        return None, "taxes.cents must be a non-negative whole number or null when unknown."
    currency_raw = taxes_value.get("currency")
    currency = str(currency_raw).strip().upper() if currency_raw is not None else None
    if currency is not None and not re.fullmatch(r"[A-Z]{3}", currency):
        return None, "taxes.currency must be a 3-letter ISO code or null."
    if cents is not None and currency is None:
        return None, "taxes.currency is required when taxes.cents is reported."
    symbol = _safe_text(taxes_value.get("symbol"), max_length=12) if taxes_value.get("symbol") is not None else None
    if taxes_value.get("symbol") is not None and symbol is None:
        return None, "taxes.symbol must be short text or null."
    if not isinstance(taxes_value.get("per_passenger"), bool):
        return None, "taxes.per_passenger must be boolean so CPP can use the correct party total."

    availability_value = value.get("seat_availability")
    if not isinstance(availability_value, Mapping):
        return None, "seat_availability must be an object with count and confidence."
    if "count" not in availability_value or "confidence" not in availability_value:
        return None, "seat_availability.count and seat_availability.confidence are required."
    count_raw = availability_value.get("count")
    count = _normalise_nonnegative_int(count_raw)
    if count_raw is not None and count is None:
        return None, "seat_availability.count must be a non-negative whole number or null."
    confidence_raw = availability_value.get("confidence")
    confidence = str(confidence_raw).strip().lower() if isinstance(confidence_raw, str) else None
    if confidence not in _SEAT_CONFIDENCE:
        return None, "seat_availability.confidence must be reported, estimated, or unknown."
    # Providers frequently encode unknown inventory as zero. Never turn that into a
    # sold-out statement or a ranking filter.
    if count in {None, 0}:
        count = None
        confidence = "unknown"

    detail_level = _normalise_detail_level(value.get("detail_level"))
    if detail_level is None:
        return None, "detail_level must be summary_only or flight_level."
    if detail_level == "flight_level":
        segment_error = _validate_flight_level_segments(
            segments, origin=origin, destination=destination, stops=stops
        )
        if segment_error:
            return None, segment_error

    if "booking_links" not in value:
        return None, "booking_links is required (use [] when no link is available)."
    booking_links, booking_error = _normalise_booking_links(value.get("booking_links"))
    if booking_error:
        return None, booking_error

    evidence_value = value.get("evidence")
    if not isinstance(evidence_value, Mapping):
        return None, "evidence must state availability_mode and an offset-aware timestamp."
    availability_mode = _normalise_availability_mode(evidence_value.get("availability_mode"))
    if availability_mode is None:
        return None, "evidence.availability_mode must identify manual_import, cached_availability, provider_live_offer, hosted_licensed_api, or unknown."
    source_updated_at = _normalise_timestamp(evidence_value.get("source_updated_at"))
    fetched_at = _normalise_timestamp(evidence_value.get("fetched_at"))
    observed_at = _normalise_timestamp(evidence_value.get("observed_at"))
    supplied_times = [
        ("source_updated_at", evidence_value.get("source_updated_at"), source_updated_at),
        ("fetched_at", evidence_value.get("fetched_at"), fetched_at),
        ("observed_at", evidence_value.get("observed_at"), observed_at),
    ]
    for field_name, supplied, normalized in supplied_times:
        if supplied is not None and normalized is None:
            return None, f"evidence.{field_name} must be an offset-aware ISO timestamp or null."
    if not any((source_updated_at, fetched_at, observed_at)):
        return None, "evidence needs an offset-aware source_updated_at, fetched_at, or observed_at timestamp."
    acquisition = _safe_text(evidence_value.get("acquisition"), max_length=80)
    if evidence_value.get("acquisition") is not None and acquisition is None:
        return None, "evidence.acquisition must be short text or null."

    normalized = {
        "schema_version": AWARD_OFFER_SCHEMA_VERSION,
        "offer_id": offer_id,
        "provider": {"id": provider_id, "name": provider_name},
        "provider_offer_id": provider_offer_id,
        "redemption_program": {
            "id": program_id,
            "name": program_name,
            "provider_program_id": provider_program_id,
        },
        "itinerary": {
            "origin": origin,
            "destination": destination,
            "departure_date": departure_date,
            "segments": segments,
            "stops": stops,
            "duration_minutes": duration,
            "operating_carriers": operating_carriers,
            "marketing_carriers": marketing_carriers,
        },
        "cabin": cabin,
        "award": {"points": points, "per_passenger": award_value["per_passenger"]},
        "taxes": {
            "cents": cents,
            "currency": currency,
            "symbol": symbol,
            "per_passenger": taxes_value["per_passenger"],
        },
        "seat_availability": {"count": count, "confidence": confidence},
        "detail_level": detail_level,
        "booking_links": booking_links,
        "evidence": {
            "availability_mode": availability_mode,
            "observation_state": _provenance_state(availability_mode, imported_manually),
            "acquisition": acquisition or ("manual_award_offer_import" if imported_manually else "provider_adapter"),
            "source_updated_at": source_updated_at,
            "fetched_at": fetched_at,
            "observed_at": observed_at,
            "imported_manually": imported_manually,
            # An import can preserve a provider's stated mode, but this CLI has not
            # independently checked that statement and never promotes it to verified.
            "verification_status": "not_independently_verified" if imported_manually else "provider_reported",
        },
    }
    return AwardOffer(normalized), None


def normalize_provider_award_offer(
    value: Mapping[str, Any], *, imported_manually: bool = False
) -> AwardOffer:
    """Validate one offer for a provider adapter or local import boundary.

    Licensed/hosted adapters should call this with the default ``False`` after their
    own authorized acquisition. CLI file/inline imports use ``normalize_award_offer``
    below, which always keeps the independent-verification boundary intact.
    """

    offer, error = _normalise_offer(value, imported_manually=imported_manually)
    if error or offer is None:
        raise AwardOfferValidationError(error or "invalid award offer")
    return offer


def normalize_award_offer(value: Mapping[str, Any]) -> AwardOffer:
    """Validate one manually supplied normalized award offer or raise a concise error."""

    return normalize_provider_award_offer(value, imported_manually=True)


def parse_award_offers(payloads: Sequence[Any]) -> tuple[list[AwardOffer], list[dict[str, str]]]:
    """Parse inline/file payloads without retaining their unnormalized raw objects.

    Each payload may be one offer, a list, ``{offers:[...]}``, or
    ``{award_offers:[...]}``.  Invalid objects are reported by ordinal while valid
    objects remain usable; duplicate provider-scoped IDs are rejected.
    """

    offers: list[AwardOffer] = []
    issues: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    ordinal = 0
    for payload in payloads:
        objects: Any = payload
        if isinstance(payload, Mapping):
            if "offers" in payload:
                objects = payload["offers"]
            elif "award_offers" in payload:
                objects = payload["award_offers"]
        if isinstance(objects, Mapping):
            objects = [objects]
        if not isinstance(objects, list):
            issues.append({
                "field": "award_offer",
                "reason": "An award-offer import must be one object, a list, or an object with offers/award_offers.",
            })
            continue
        for raw in objects:
            ordinal += 1
            if not isinstance(raw, Mapping):
                issues.append({"field": "award_offer", "reason": f"Award offer #{ordinal} must be a JSON object."})
                continue
            offer, error = _normalise_offer(raw, imported_manually=True)
            if error or offer is None:
                issues.append({"field": "award_offer", "reason": f"Award offer #{ordinal}: {error or 'invalid offer'}."})
                continue
            if offer.offer_id in seen_ids:
                issues.append({
                    "field": "award_offer",
                    "reason": f"Award offer #{ordinal}: duplicate provider-scoped offer_id '{offer.offer_id}'.",
                })
                continue
            seen_ids.add(offer.offer_id)
            offers.append(offer)
    return offers, issues


# A longer name is convenient for callers that distinguish import parsing from a
# provider adapter's own normalization.
parse_award_offer_payloads = parse_award_offers


class ManualAwardImportProvider:
    """Default no-login adapter for normalized local award-offer imports."""

    id = "manual_import"
    display_name = "Manual award-offer import"
    limits = ProviderLimits(
        supports_fetch=False,
        requires_credentials=False,
        network_access=False,
        requires_explicit_selection=False,
        description="Local normalized JSON only; no network request, account, or credential is used.",
    )

    def __init__(self, offers: Sequence[AwardOffer] = ()):
        self.offers = tuple(offers)
        self.program_catalog = {
            offer.program_id: offer.program_name
            for offer in self.offers
        }

    @staticmethod
    def _matches(offer: AwardOffer, request: AwardSearchRequest) -> bool:
        value = offer.payload
        itinerary = value["itinerary"]
        if itinerary["origin"] not in request.origins or itinerary["destination"] not in request.destinations:
            return False
        if not (request.start_date <= itinerary["departure_date"] <= request.end_date):
            return False
        if value["cabin"] != request.cabin:
            return False
        if request.program_ids and offer.program_id not in request.program_ids:
            return False
        stops = itinerary.get("stops")
        if request.direct_only and stops != 0:
            return False
        return True

    @staticmethod
    def _evidence_coverage(offers: Sequence[AwardOffer]) -> dict[str, Any]:
        timestamps: list[tuple[dt.datetime, str]] = []
        for offer in offers:
            evidence = offer.payload.get("evidence", {})
            if not isinstance(evidence, Mapping):
                continue
            for field_name in ("fetched_at", "observed_at", "source_updated_at"):
                raw = evidence.get(field_name)
                normalized = _normalise_timestamp(raw)
                if normalized is None:
                    continue
                try:
                    timestamps.append((dt.datetime.fromisoformat(normalized), normalized))
                except ValueError:
                    continue
        if not timestamps:
            return {
                "oldest_evidence_at": None,
                "newest_evidence_at": None,
                "newest_evidence_age_seconds": None,
            }
        oldest = min(timestamps, key=lambda item: item[0])
        newest = max(timestamps, key=lambda item: item[0])
        now = dt.datetime.now(dt.timezone.utc)
        return {
            "oldest_evidence_at": oldest[1],
            "newest_evidence_at": newest[1],
            "newest_evidence_age_seconds": max(0, int((now - newest[0]).total_seconds())),
        }

    def find_cached(self, request: AwardSearchRequest) -> AwardSearchSnapshot:
        matches = tuple(offer for offer in self.offers if self._matches(offer, request))
        flight_level = sum(1 for offer in matches if offer.payload["detail_level"] == "flight_level")
        status = "manual_import_ready" if matches else (
            "manual_import_empty" if not self.offers else "manual_import_no_match"
        )
        return AwardSearchSnapshot(
            provider_id=self.id,
            provider_name=self.display_name,
            offers=matches,
            status=status,
            coverage={
                "mode": "manual_import",
                "coverage_status": "unknown_unverified",
                "completeness": "unknown",
                "input_offer_count": len(self.offers),
                "stored_result_count": len(matches),
                # A hand-picked import is never evidence that every possible result
                # was supplied, even when it has no local cap.
                "may_be_truncated": None,
                "trip_details_requested": None,
                "flight_level_availability_count": flight_level,
                "summary_only_availability_count": len(matches) - flight_level,
                "network_used": False,
                **self._evidence_coverage(matches),
            },
            cache=None,
            can_suppress_fetch=True,
            fetch_supported=False,
            note=(
                "Imported award offers are user-supplied records and are not independently verified; "
                "their completeness and coverage are unknown."
            ),
        )

    def fetch(self, request: AwardSearchRequest) -> AwardSearchSnapshot:
        # Deliberately no transport fallback: importing data is not authorization to
        # query or scrape any named source.
        return self.find_cached(request)


def legacy_candidate_to_award_offer(
    candidate: Mapping[str, Any],
    *,
    provider_id: str = "seats.aero",
    provider_name: str = "Seats.aero",
    program_name: str | None = None,
) -> AwardOffer:
    """Map legacy local cache rows into the same normalized adapter boundary.

    This compatibility mapper is intentionally one-way: new providers do not query
    the legacy tables, so upstream IDs cannot leak across provider caches.
    """

    raw_provider_id = _normalise_identifier(provider_id, _PROVIDER_ID_RE) or "legacy_provider"
    raw_id = _safe_text(candidate.get("id"), max_length=256) or "unknown"
    raw_program = _normalise_identifier(candidate.get("program"), _PROGRAM_ID_RE) or "unknown"
    segments: list[dict[str, Any]] = []
    for segment in candidate.get("segments") or []:
        if not isinstance(segment, Mapping):
            continue
        normalized_segment: dict[str, Any] = {}
        source_field_map = {
            "origin": "origin",
            "destination": "destination",
            "departs_at": "departs_at",
            "arrives_at": "arrives_at",
            "flight_number": "flight_number",
            "aircraft_name": "aircraft",
            "fare_class": "fare_class",
        }
        for target, source in source_field_map.items():
            raw = segment.get(source)
            if isinstance(raw, str) and raw.strip():
                normalized_segment[target] = raw.strip().upper() if target in {"origin", "destination"} else raw.strip()
        for target, source in (("operating_carrier", "operating_carrier"), ("marketing_carrier", "marketing_carrier")):
            carrier = _normalise_carrier(segment.get(source))
            if carrier:
                normalized_segment[target] = carrier
        segments.append(normalized_segment)
    if candidate.get("kind") == "trip" and not segments:
        # Older local rows can have trip-level schedule/flight fields but no stored
        # child segment rows. Synthesize one allowlisted segment so the normalized
        # contract remains itinerary-shaped without retaining raw provider JSON.
        fallback_segment: dict[str, Any] = {}
        for target, source in (
            ("origin", "origin"),
            ("destination", "destination"),
            ("departs_at", "departs_at"),
            ("arrives_at", "arrives_at"),
            ("flight_number", "flight_numbers"),
        ):
            raw = candidate.get(source)
            if isinstance(raw, str) and raw.strip():
                fallback_segment[target] = raw.strip().upper() if target in {"origin", "destination"} else raw.strip()
        if fallback_segment:
            segments.append(fallback_segment)
    carrier_values = _normalise_carriers(candidate.get("carriers")) or []
    points = _normalise_positive_int(candidate.get("points"))
    taxes_cents = _normalise_nonnegative_int(candidate.get("taxes_cents"))
    taxes_currency = candidate.get("taxes_currency")
    currency = str(taxes_currency).upper() if isinstance(taxes_currency, str) and re.fullmatch(r"[A-Za-z]{3}", taxes_currency) else None
    if taxes_cents is not None and currency is None:
        taxes_cents = None
    taxes_per_passenger = candidate.get("taxes_per_passenger")
    if not isinstance(taxes_per_passenger, bool):
        taxes_per_passenger = None
    raw_seats = _normalise_nonnegative_int(candidate.get("seats"))
    seats = raw_seats if raw_seats and raw_seats > 0 else None
    detail_level = "flight_level" if candidate.get("kind") == "trip" else "summary_only"
    links, _ = _normalise_booking_links(candidate.get("booking_links") or [])
    fetched_at = _normalise_timestamp(candidate.get("fetched_at")) or _utc_now()
    source_updated_at = _normalise_timestamp(candidate.get("source_updated_at"))
    payload = {
        "schema_version": AWARD_OFFER_SCHEMA_VERSION,
        "offer_id": f"{raw_provider_id}:{'trip' if detail_level == 'flight_level' else 'availability'}:{raw_id}",
        "provider": {"id": raw_provider_id, "name": provider_name},
        "provider_offer_id": raw_id,
        "redemption_program": {
            "id": raw_program,
            "name": program_name or raw_program,
            "provider_program_id": raw_program,
        },
        "itinerary": {
            "origin": _normalise_airport(candidate.get("origin")) or "UNK",
            "destination": _normalise_airport(candidate.get("destination")) or "UNK",
            "departure_date": _normalise_date(candidate.get("date")) or "1970-01-01",
            "segments": segments,
            "stops": _normalise_nonnegative_int(candidate.get("stops")),
            "duration_minutes": _normalise_positive_int(candidate.get("duration_minutes")),
            "operating_carriers": carrier_values,
            "marketing_carriers": [],
        },
        "cabin": _normalise_cabin(candidate.get("cabin")) or "business",
        "award": {"points": points, "per_passenger": True},
        "taxes": {
            "cents": taxes_cents,
            "currency": currency,
            "symbol": _safe_text(candidate.get("taxes_symbol"), max_length=12),
            # Legacy rows do not carry a validated fee scope. Keep it unknown rather
            # than silently multiplying it in CPP math.
            "per_passenger": taxes_per_passenger,
        },
        "seat_availability": {"count": seats, "confidence": "reported" if seats else "unknown"},
        "detail_level": detail_level,
        "booking_links": links,
        "evidence": {
            "availability_mode": "cached_availability",
            "observation_state": "cached_availability",
            "acquisition": "legacy_local_cache_adapter",
            "source_updated_at": source_updated_at,
            "fetched_at": fetched_at,
            "observed_at": None,
            "imported_manually": False,
            "verification_status": "provider_reported",
        },
    }
    return AwardOffer(payload)


def award_offer_schema() -> dict[str, Any]:
    """A compact public contract/example for local import UI and API clients."""

    return {
        "schema_version": AWARD_OFFER_SCHEMA_VERSION,
        "offer_id": "provider-id:offer:opaque-id",
        "provider": {"id": "provider-id", "name": "Provider display name"},
        "provider_offer_id": "opaque-provider-id",
        "redemption_program": {
            "id": "aeroplan",
            "name": "Air Canada Aeroplan",
            "provider_program_id": "provider-program-id",
        },
        "itinerary": {
            "origin": "SFO",
            "destination": "CDG",
            "departure_date": "2026-09-01",
            "segments": [],
            "operating_carriers": [],
            "marketing_carriers": [],
        },
        "cabin": "business",
        "award": {"points": 50000, "per_passenger": True},
        "taxes": {"cents": 560, "currency": "USD", "symbol": "$", "per_passenger": True},
        "seat_availability": {"count": None, "confidence": "unknown"},
        "detail_level": "summary_only",
        "booking_links": [],
        "evidence": {
            "availability_mode": "manual_import",
            "fetched_at": "2026-01-01T12:00:00Z",
        },
    }
