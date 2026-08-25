import argparse
import datetime as dt
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import flights  # noqa: E402
import research  # noqa: E402


GENERIC_TRANSFER_PROFILE = {
    "id": "example_rewards",
    "name": "Example Rewards",
    "reference_version": "example-profile-v1",
    "as_of": "2026-08-25",
    "source_url": "https://example.test/rewards",
    "partners": [{
        "program": "aeroplan",
        "recipient_name": "Air Canada Aeroplan",
        "recipient_per_1000_source_points": 750,
        "minimum_source_points": 1000,
        "source_increment": 1000,
        "source_url": "https://example.test/rewards/aeroplan",
    }],
}


SAMPLE = {
    "ID": "research-avail-1",
    "Route": {
        "OriginAirport": "SFO",
        "DestinationAirport": "CDG",
        "Source": "aeroplan",
    },
    "Date": "2026-09-01",
    "JAvailable": True,
    "JMileageCost": 50000,
    "JRemainingSeats": 2,
    "JAirlines": "UA",
    "JDirect": True,
    "Source": "aeroplan",
    "AvailabilityTrips": [
        {
            "ID": "research-trip-1",
            "AvailabilityID": "research-avail-1",
            "Cabin": "business",
            "MileageCost": 50000,
            "TotalTaxes": 560,
            "TaxesCurrency": "USD",
            "TaxesCurrencySymbol": "$",
            "RemainingSeats": 2,
            "Stops": 0,
            "TotalDuration": 600,
            "Carriers": "UA",
            "FlightNumbers": "UA990",
            "Source": "aeroplan",
        }
    ],
}


class BriefTests(unittest.TestCase):
    def test_structured_controller_brief_has_provenance_and_defaults(self):
        brief = research.parse_trip_brief(
            {
                "journey": "one_way",
                "legs": [{
                    "origin": ["sfo"],
                    "destination": ["cdg"],
                    "departure": {"start": "2026-09-01", "end": "2026-09-03"},
                }],
                "passengers": {"count": 2},
                "cabin": {"primary": "business"},
                "points": {"programs": ["aeroplan"]},
            },
            flights.PROGRAMS,
        )
        self.assertTrue(brief.ready)
        self.assertEqual(brief.origins, ("SFO",))
        self.assertEqual(brief.destinations, ("CDG",))
        self.assertEqual(brief.passengers, 2)
        self.assertEqual(brief.programs, ("aeroplan",))
        self.assertEqual(brief.provenance["origin"], "user")
        self.assertEqual(brief.provenance["cabin"], "user")

    def test_transfer_sources_are_optional_and_accept_custom_profiles(self):
        unconfigured = research.parse_trip_brief(
            '{"origin":"SFO","destination":"CDG","departure_date":"2026-09-01"}',
            flights.PROGRAMS,
        )
        self.assertEqual(unconfigured.transfer_profiles, ())
        self.assertEqual(unconfigured.to_dict()["transfer_sources"]["source"], "not_configured")

        custom = research.parse_trip_brief(
            {
                "origin": "SFO", "destination": "CDG", "departure_date": "2026-09-01",
                "transfer_sources": [GENERIC_TRANSFER_PROFILE],
            },
            flights.PROGRAMS,
        )
        self.assertTrue(custom.ready)
        self.assertEqual(custom.transfer_profiles[0].id, "example_rewards")
        self.assertEqual(custom.to_dict()["transfer_sources"]["selected"][0]["name"], "Example Rewards")

        invalid = research.parse_trip_brief(
            {
                "origin": "SFO", "destination": "CDG", "departure_date": "2026-09-01",
                "transfer_sources": ["unknown_rewards"],
            },
            flights.PROGRAMS,
        )
        self.assertIn("transfer_sources", {item["field"] for item in invalid.follow_up_fields})
        self.assertEqual(invalid.transfer_profiles, ())

    def test_cash_only_brief_defaults_to_economy_without_claiming_a_live_fare(self):
        brief = research.parse_trip_brief(
            '{"intent":"cash_only","origin":"SFO","destination":"CDG","departure_date":"2026-09-01"}',
            flights.PROGRAMS,
        )
        self.assertTrue(brief.ready)
        self.assertEqual(brief.cabin, "economy")
        self.assertEqual(brief.provenance["cabin"], "default")
        handoff = research.google_flights_handoff(brief)
        self.assertEqual(handoff["status"], "ready_for_browser")
        self.assertFalse(handoff["live_fares_obtained"])

    def test_ambiguous_or_incomplete_brief_requests_fields_without_guessing(self):
        brief = research.parse_trip_brief("San Francisco to Paris next week with family", flights.PROGRAMS)
        issues = {item["field"] for item in brief.follow_up_fields}
        self.assertIn("origin", issues)
        self.assertIn("destination", issues)
        self.assertIn("departure", issues)
        self.assertIn("passengers", issues)
        self.assertEqual(brief.origins, ())
        self.assertEqual(brief.destinations, ())
        self.assertIsNone(brief.to_dict()["passengers"]["count"])

    def test_invalid_structured_values_are_not_emitted_as_defaults(self):
        brief = research.parse_trip_brief(
            {
                "origin": "SFO", "destination": "CDG", "departure_date": "2026-09-01",
                "cabin": "spaceship", "intent": "maybe", "programs": ["unknown program"],
            },
            flights.PROGRAMS,
        )
        rendered = brief.to_dict()
        self.assertIsNone(rendered["cabin"]["primary"])
        self.assertEqual(rendered["intent"], "unresolved")
        self.assertEqual(rendered["points"]["programs"], "unresolved")

    def test_invalid_intent_does_not_silently_assume_award_defaults(self):
        brief = research.parse_trip_brief(
            '{"intent":"maybe","origin":"SFO","destination":"CDG","departure_date":"2026-09-01"}',
            flights.PROGRAMS,
        )
        rendered = brief.to_dict()
        self.assertEqual(rendered["intent"], "unresolved")
        self.assertIsNone(rendered["cabin"]["primary"])
        self.assertEqual(rendered["points"]["programs"], "unresolved")

    def test_text_counts_and_point_caps_do_not_silently_change_the_brief(self):
        valid = research.parse_trip_brief(
            "SFO to CDG 2026-09-01 two passengers under 50k points", flights.PROGRAMS
        )
        self.assertTrue(valid.ready)
        self.assertEqual(valid.passengers, 2)
        self.assertEqual(valid.max_points, 50000)

        for request in (
            "SFO to CDG 2026-09-01 10 passengers",
            "SFO to CDG 2026-09-01 -2 passengers",
        ):
            brief = research.parse_trip_brief(request, flights.PROGRAMS)
            self.assertIn("passengers", {item["field"] for item in brief.follow_up_fields})
            self.assertIsNone(brief.to_dict()["passengers"]["count"])

        invalid_cap = research.parse_trip_brief(
            "SFO to CDG 2026-09-01 under 0 points", flights.PROGRAMS
        )
        self.assertIn("max_points", {item["field"] for item in invalid_cap.follow_up_fields})
        self.assertEqual(invalid_cap.to_dict()["points"]["max_points_source"], "unresolved")

    def test_text_route_does_not_treat_ordinary_prose_as_an_airport(self):
        brief = research.parse_trip_brief("fly to LAX 2026-09-01", flights.PROGRAMS)
        self.assertEqual(brief.origins, ())
        self.assertEqual(brief.destinations, ())
        self.assertIn("route", {item["field"] for item in brief.follow_up_fields})

    def test_structured_stop_preferences_are_preserved_or_blocked(self):
        brief = research.parse_trip_brief(
            {
                "origin": "SFO", "destination": "CDG", "departure_date": "2026-09-01",
                "stops": {"preference": "prefer_nonstop_allow_connections"},
            },
            flights.PROGRAMS,
        )
        self.assertTrue(brief.ready)
        self.assertFalse(brief.direct_only)
        self.assertEqual(brief.provenance["stops"], "user")

        invalid = research.parse_trip_brief(
            {
                "origin": "SFO", "destination": "CDG", "departure_date": "2026-09-01",
                "stops": {"preference": "maybe"},
            },
            flights.PROGRAMS,
        )
        self.assertIn("stops", {item["field"] for item in invalid.follow_up_fields})
        self.assertEqual(invalid.to_dict()["stops"]["preference"], "unresolved")

    def test_round_trip_is_a_separate_leg_follow_up(self):
        brief = research.parse_trip_brief(
            "SFO to CDG 2026-09-01 returning 2026-09-10", flights.PROGRAMS
        )
        self.assertIn("return_leg", {item["field"] for item in brief.follow_up_fields})
        self.assertEqual(brief.start_date, brief.end_date)


class HandoffAndComparisonTests(unittest.TestCase):
    def test_google_handoff_is_only_a_manual_url(self):
        brief = research.parse_trip_brief(
            '{"origin":"SFO","destination":"CDG","departure_date":"2026-09-01"}',
            flights.PROGRAMS,
        )
        handoff = research.google_flights_handoff(brief)
        self.assertEqual(handoff["provider"], "google_flights")
        self.assertFalse(handoff["scraped"])
        self.assertFalse(handoff["live_fares_obtained"])
        self.assertIn("sends the displayed trip query to Google", handoff["data_sharing_notice"])
        self.assertIn("google.com/travel/flights", handoff["url"])
        self.assertIn("SFO", handoff["query_text"])

    def test_selected_transfer_sources_drive_math_and_cpp(self):
        profiles, error = research.parse_transfer_profiles(
            ["chase_ultimate_rewards", "capital_one_miles"], flights.PROGRAMS
        )
        self.assertIsNone(error)
        self.assertIsNotNone(profiles)
        transfers = research.transfer_options("jetblue", 45000, profiles or ())
        by_source = {item["point_source"]: item for item in transfers}
        self.assertEqual(by_source["chase_ultimate_rewards"]["source_points_to_transfer"], 45000)
        self.assertEqual(by_source["capital_one_miles"]["source_points_to_transfer"], 75000)
        virgin_capital_one = {
            item["point_source"]: item
            for item in research.transfer_options("virginatlantic", 45000, profiles or ())
        }["capital_one_miles"]
        self.assertEqual(virgin_capital_one["status"], "requires_manual_confirmation")

        custom_profiles, error = research.parse_transfer_profiles([GENERIC_TRANSFER_PROFILE], flights.PROGRAMS)
        self.assertIsNone(error)
        custom_transfer = research.transfer_options("aeroplan", 45000, custom_profiles or ())[0]
        self.assertEqual(custom_transfer["point_source"], "example_rewards")
        self.assertEqual(custom_transfer["source_points_to_transfer"], 60000)

        quotes, issues = research.parse_manual_cash_quotes([{
            "provider": "google_flights",
            "total": 1000,
            "currency": "USD",
            "amount_scope": "total",
            "origin": "SFO",
            "destination": "JFK",
            "departure_date": "2026-09-01",
            "cabin": "business",
            "passengers": 1,
            "same_itinerary": True,
            "fare_inclusions_match": True,
            "observed_at": "2026-01-01T12:00:00Z",
            "booking_url": "https://www.google.com/travel/flights",
        }])
        self.assertEqual(issues, [])
        comparisons = research.compare_award_to_cash(
            {
                "kind": "trip", "origin": "SFO", "destination": "JFK", "date": "2026-09-01",
                "cabin": "business", "points": 45000, "taxes_cents": 560,
                "taxes_currency": "USD", "taxes_per_passenger": True,
                "segments": [{
                    "origin": "SFO", "destination": "JFK",
                    "departs_at": "2026-09-01T08:00:00Z", "arrives_at": "2026-09-01T16:00:00Z",
                }],
            },
            quotes,
            1,
            transfers,
        )
        cpp_by_source = {
            item["point_source"]: item["cpp"]
            for item in comparisons if item["state"] == "user_asserted_comparable"
        }
        self.assertAlmostEqual(cpp_by_source["chase_ultimate_rewards"], 2.21, places=2)
        self.assertAlmostEqual(cpp_by_source["capital_one_miles"], 1.326, places=3)
        self.assertTrue(all(item["verification_required"] for item in comparisons))

    def test_user_supplied_reference_urls_are_public_safe(self):
        profile = json.loads(json.dumps(GENERIC_TRANSFER_PROFILE))
        profile["source_url"] = "https://example.test/rewards?session=demo#fragment"
        profile["partners"][0]["source_url"] = "https://example.test/rewards/aeroplan?token=demo"
        profiles, error = research.parse_transfer_profiles([profile], flights.PROGRAMS)

        self.assertIsNone(error)
        self.assertEqual(profiles[0].source_url, "https://example.test/rewards")
        transfer = research.transfer_options("aeroplan", 50000, profiles)[0]
        self.assertEqual(transfer["source_url"], "https://example.test/rewards/aeroplan")

    def test_builtin_profiles_are_optional_and_do_not_imply_other_sources(self):
        profiles, error = research.parse_transfer_profiles(
            ["chase_ultimate_rewards", "capital_one_miles"], flights.PROGRAMS
        )
        self.assertIsNone(error)
        by_source = {
            item["point_source"]: item for item in research.transfer_options("emirates", 45000, profiles or ())
        }
        self.assertEqual(by_source["chase_ultimate_rewards"]["status"], "not_configured")
        self.assertEqual(by_source["capital_one_miles"]["status"], "direct_reference")
        self.assertEqual(by_source["capital_one_miles"]["source_points_to_transfer"], 60000)
        self.assertEqual(
            by_source["capital_one_miles"]["reference_version"], research.BUILTIN_TRANSFER_REFERENCE_VERSION
        )
        self.assertEqual(by_source["capital_one_miles"]["as_of"], research.BUILTIN_TRANSFER_REFERENCE_AS_OF)
        aeromexico = {
            item["point_source"]: item for item in research.transfer_options("aeromexico", 10000, profiles or ())
        }["capital_one_miles"]
        self.assertEqual(aeromexico["recipient_program"], "Aeromexico Rewards (formerly Club Premier)")

    def test_program_groups_preserve_source_provenance_without_global_dedup(self):
        brief = research.parse_trip_brief(
            '{"origin":"SFO","destination":"CDG","departure_date":"2026-09-01"}',
            flights.PROGRAMS,
        )
        groups = research.group_award_recommendations([
            {
                "id": "same-external-id", "availability_id": "same-external-id", "program": "aeroplan",
                "origin": "SFO", "destination": "CDG", "date": "2026-09-01", "cabin": "business",
                "points": 50000, "taxes_cents": 500, "taxes_currency": "USD", "seats": 1,
                "stops": 0, "kind": "trip", "source_updated_at": None, "fetched_at": None, "score": 1,
            },
            {
                "id": "same-external-id", "availability_id": "same-external-id", "program": "flyingblue",
                "origin": "SFO", "destination": "CDG", "date": "2026-09-01", "cabin": "business",
                "points": 55000, "taxes_cents": 700, "taxes_currency": "EUR", "seats": 0,
                "stops": 0, "kind": "summary", "source_updated_at": None, "fetched_at": None, "score": 2,
            },
        ], brief, flights.PROGRAMS, [], 5)
        self.assertEqual([group["program"] for group in groups], ["aeroplan", "flyingblue"])
        first = groups[0]["tax_currency_buckets"][0]["recommendations"][0]
        second = groups[1]["tax_currency_buckets"][0]["recommendations"][0]
        self.assertEqual(first["research_evidence"]["redemption_program"], "aeroplan")
        self.assertEqual(second["research_evidence"]["seat_confidence"], "unknown")
        self.assertNotIn("score", first)

    def test_program_groups_prefer_known_nonstop_within_a_tax_bucket(self):
        brief = research.parse_trip_brief(
            '{"origin":"SFO","destination":"CDG","departure_date":"2026-09-01"}',
            flights.PROGRAMS,
        )
        groups = research.group_award_recommendations([
            {
                "id": "connection", "program": "aeroplan", "origin": "SFO", "destination": "CDG",
                "date": "2026-09-01", "cabin": "business", "points": 45000,
                "taxes_currency": "USD", "seats": 2, "stops": 1, "kind": "trip",
            },
            {
                "id": "nonstop", "program": "aeroplan", "origin": "SFO", "destination": "CDG",
                "date": "2026-09-01", "cabin": "business", "points": 50000,
                "taxes_currency": "USD", "seats": 2, "stops": 0, "kind": "trip",
            },
        ], brief, flights.PROGRAMS, [], 5)
        recommendations = groups[0]["tax_currency_buckets"][0]["recommendations"]
        self.assertEqual([item["id"] for item in recommendations], ["nonstop", "connection"])

    def test_summary_award_does_not_claim_an_exact_cash_comparison(self):
        quotes, _ = research.parse_manual_cash_quotes([{
            "total": 1000, "currency": "USD", "amount_scope": "total",
            "origin": "SFO", "destination": "JFK", "departure_date": "2026-09-01",
            "cabin": "business", "passengers": 1,
            "same_itinerary": True, "fare_inclusions_match": True,
            "observed_at": "2026-01-01T12:00:00Z",
            "booking_url": "https://www.google.com/travel/flights",
        }])
        comparison = research.compare_award_to_cash(
            {
                "kind": "summary", "origin": "SFO", "destination": "JFK", "date": "2026-09-01",
                "cabin": "business", "points": 50000, "taxes_cents": 500, "taxes_currency": "USD",
            }, quotes, 1, research.transfer_options("aeroplan", 50000)
        )
        self.assertEqual(comparison[0]["state"], "not_comparable")
        self.assertIn("summary-only", comparison[0]["reason"])

    def test_manual_import_never_adopts_a_live_fare_confidence_label(self):
        quotes, issues = research.parse_manual_cash_quotes([{
            "total": 1000, "currency": "USD", "amount_scope": "total",
            "source_confidence": "live_api",
            "observed_at": "2026-01-01T12:00:00Z",
            "booking_url": "https://www.google.com/travel/flights",
        }])
        self.assertEqual(issues, [])
        self.assertEqual(quotes[0]["evidence_origin"], "user_import")
        self.assertEqual(quotes[0]["source_confidence"], "manual_unverified")

    def test_nonfinite_or_subcent_cash_amounts_are_rejected_without_crashing(self):
        payloads = [
            json.loads('{"total": NaN, "currency": "USD", "amount_scope": "total"}'),
            json.loads('{"total": Infinity, "currency": "USD", "amount_scope": "total"}'),
            {"total": 0.001, "currency": "USD", "amount_scope": "total"},
        ]
        quotes, issues = research.parse_manual_cash_quotes(payloads)
        self.assertEqual(quotes, [])
        self.assertEqual(len(issues), 3)
        self.assertTrue(all("positive total" in issue["reason"] for issue in issues))

    def test_cash_cpp_requires_timestamp_and_evidence_and_is_user_asserted(self):
        quotes, issues = research.parse_manual_cash_quotes([{
            "total": 1000, "currency": "USD", "amount_scope": "total",
            "origin": "SFO", "destination": "JFK", "departure_date": "2026-09-01",
            "cabin": "business", "passengers": 1,
            "same_itinerary": True, "fare_inclusions_match": True,
        }])
        self.assertTrue(issues)
        comparison = research.compare_award_to_cash(
            {
                "kind": "trip", "origin": "SFO", "destination": "JFK", "date": "2026-09-01",
                "cabin": "business", "points": 50000, "taxes_cents": 500, "taxes_currency": "USD",
            },
            quotes,
            1,
            research.transfer_options("aeroplan", 50000),
        )
        self.assertEqual(comparison[0]["state"], "not_comparable")
        self.assertIn("observed_at", comparison[0]["reason"])

    def test_unknown_tax_currency_bucket_sorts_after_known_currency(self):
        brief = research.parse_trip_brief(
            '{"origin":"SFO","destination":"CDG","departure_date":"2026-09-01"}',
            flights.PROGRAMS,
        )
        groups = research.group_award_recommendations([
            {
                "id": "unknown", "program": "aeroplan", "origin": "SFO", "destination": "CDG",
                "date": "2026-09-01", "cabin": "business", "points": 50000,
                "taxes_currency": None, "seats": 1, "stops": 0, "kind": "summary",
            },
            {
                "id": "usd", "program": "aeroplan", "origin": "SFO", "destination": "CDG",
                "date": "2026-09-01", "cabin": "business", "points": 50000,
                "taxes_currency": "USD", "seats": 1, "stops": 0, "kind": "summary",
            },
        ], brief, flights.PROGRAMS, [], 5)
        self.assertEqual(
            [bucket["tax_currency"] for bucket in groups[0]["tax_currency_buckets"]],
            ["USD", None],
        )

    def test_cash_currency_mismatch_does_not_calculate_cpp(self):
        quotes, _ = research.parse_manual_cash_quotes([{
            "total": 1000, "currency": "EUR", "amount_scope": "total",
            "origin": "SFO", "destination": "JFK", "departure_date": "2026-09-01",
            "cabin": "business", "passengers": 1,
            "same_itinerary": True, "fare_inclusions_match": True,
            "observed_at": "2026-01-01T12:00:00Z",
            "booking_url": "https://www.google.com/travel/flights",
        }])
        comparison = research.compare_award_to_cash(
            {
                "kind": "trip", "origin": "SFO", "destination": "JFK", "date": "2026-09-01",
                "cabin": "business", "points": 50000, "taxes_cents": 500,
                "taxes_currency": "USD", "taxes_per_passenger": True,
                "segments": [{
                    "origin": "SFO", "destination": "JFK",
                    "departs_at": "2026-09-01T08:00:00Z", "arrives_at": "2026-09-01T16:00:00Z",
                }],
            },
            quotes,
            1,
            research.transfer_options("aeroplan", 50000),
        )
        self.assertEqual(comparison[0]["state"], "not_comparable")
        self.assertIn("no currency conversion", comparison[0]["reason"])


class CacheAndRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = flights.Database(Path(self.temp.name) / "research.sqlite3")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def _stored_search(self, *, max_results=100, summary_only=False, objects=None):
        args = argparse.Namespace(
            origins="SFO",
            destinations="CDG",
            start_date="2026-09-01",
            end_date="2026-09-01",
            cabins="business",
            sources="",
            carriers="",
            direct=False,
            seats=1,
            summary_only=summary_only,
            max_results=max_results,
        )
        search_id = self.db.create_search(args)
        count = self.db.store_search_payload(search_id, objects or [SAMPLE])
        self.db.finish_search(search_id, status="complete", result_count=count)
        return search_id

    def _args(self, *extra):
        parser = flights.make_parser()
        args = parser.parse_args([
            "research", "--provider", "seats.aero",
            '{"origin":"SFO","destination":"CDG","departure_date":"2026-09-01"}',
            "--json",
            *extra,
        ])
        flights.ensure_positive(args)
        return args

    def test_matching_cache_honors_ttl(self):
        search_id = self._stored_search()
        now = dt.datetime.now(dt.timezone.utc)
        old = (now - dt.timedelta(hours=2)).isoformat()
        self.db.conn.execute("UPDATE search_runs SET completed_at=? WHERE id=?", (old, search_id))
        self.db.conn.commit()
        matching = self.db.find_matching_search(
            origins="SFO", destinations="CDG", start_date="2026-09-01", end_date="2026-09-01",
            cabins="business", max_age_seconds=None, now=now,
        )
        self.assertEqual(matching["id"], search_id)
        self.assertIsNone(self.db.find_matching_search(
            origins="SFO", destinations="CDG", start_date="2026-09-01", end_date="2026-09-01",
            cabins="business", max_age_seconds=60, now=now,
        ))

    def test_cache_only_research_makes_no_api_request(self):
        self._stored_search()
        fetcher = mock.Mock(side_effect=AssertionError("fetch must not run without --fetch"))
        report = flights.run_research(self.db, self._args(), fetcher=fetcher)
        self.assertEqual(report["award_search"]["status"], "cache_hit")
        self.assertEqual(report["award_search"]["candidate_count"], 1)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM api_requests").fetchone()[0], 0)
        fetcher.assert_not_called()
        recommendation = report["award_search"]["recommendations_by_program"][0]["tax_currency_buckets"][0]["recommendations"][0]
        self.assertEqual(recommendation["research_evidence"]["provider"], "seats.aero")
        self.assertEqual(recommendation["transfer_access"], [])
        self.assertEqual(report["brief"]["transfer_sources"]["source"], "not_configured")

    def test_result_capped_cache_is_partial_and_explicit_fetch_is_not_skipped(self):
        capped_objects = []
        for index in range(10):
            payload = json.loads(json.dumps(SAMPLE))
            payload["ID"] = f"capped-{index}"
            payload["AvailabilityTrips"][0]["ID"] = f"capped-trip-{index}"
            capped_objects.append(payload)
        self._stored_search(max_results=10, objects=capped_objects)

        cache_only = flights.run_research(self.db, self._args())
        self.assertEqual(cache_only["status"], "partial")
        self.assertEqual(cache_only["award_search"]["status"], "cache_partial")
        coverage = cache_only["award_search"]["cache"]["coverage"]
        self.assertTrue(coverage["result_cap_reached"])
        self.assertTrue(coverage["may_be_truncated"])
        self.assertIn("partial", cache_only["award_search"]["next_action"])

        failing_fetcher = mock.Mock(side_effect=flights.ApiError("outage", 503))
        refreshed = flights.run_research(
            self.db, self._args("--fetch"), fetcher=failing_fetcher, prepare_api=lambda: None
        )
        failing_fetcher.assert_called_once()
        self.assertEqual(refreshed["award_search"]["status"], "fetch_failed_using_cache")

    def test_cache_selection_prefers_a_larger_result_capacity(self):
        larger_id = self._stored_search(max_results=100)
        capped_objects = []
        for index in range(10):
            payload = json.loads(json.dumps(SAMPLE))
            payload["ID"] = f"newer-capped-{index}"
            payload["AvailabilityTrips"][0]["ID"] = f"newer-capped-trip-{index}"
            capped_objects.append(payload)
        self._stored_search(max_results=10, objects=capped_objects)
        matching = self.db.find_matching_search(
            origins="SFO", destinations="CDG", start_date="2026-09-01", end_date="2026-09-01",
            cabins="business", required_result_limit=100,
        )
        self.assertEqual(matching["id"], larger_id)

    def test_text_report_shows_provenance_transfer_and_cash_explanation(self):
        self._stored_search()
        quote = json.dumps({
            "total": 1000, "currency": "USD", "amount_scope": "total",
            "origin": "SFO", "destination": "CDG", "departure_date": "2026-09-01",
            "cabin": "business", "passengers": 1,
            "same_itinerary": True, "fare_inclusions_match": True,
            "observed_at": "2026-01-01T12:00:00Z",
            "booking_url": "https://www.google.com/travel/flights",
        })
        report = flights.run_research(
            self.db,
            self._args("--cash-quote", quote, "--transfer-profile", json.dumps(GENERIC_TRANSFER_PROFILE)),
        )
        rendered = flights.research_text(report)
        self.assertIn("Field sources:", rendered)
        self.assertIn("Transfer sources: Example Rewards", rendered)
        self.assertIn("Transfer reference", rendered)
        self.assertIn("route-consistent flight-level itinerary", rendered)

    def test_transfer_profile_file_accepts_generic_profile_without_defaulting_issuers(self):
        self._stored_search()
        profile_path = Path(self.temp.name) / "transfer-profile.json"
        profile_path.write_text(json.dumps({"profiles": [GENERIC_TRANSFER_PROFILE]}), encoding="utf-8")
        with mock.patch.object(flights, "TRUSTED_IMPORT_ROOTS", (Path(self.temp.name),)):
            report = flights.run_research(
                self.db, self._args("--transfer-profile-file", str(profile_path))
            )
        selected = report["transfer_reference"]["selected_profiles"]
        self.assertEqual([profile["id"] for profile in selected], ["example_rewards"])
        recommendation = report["award_search"]["recommendations_by_program"][0]["tax_currency_buckets"][0]["recommendations"][0]
        self.assertEqual([item["point_source"] for item in recommendation["transfer_access"]], ["example_rewards"])

    def test_transfer_profile_file_rejects_environment_file(self):
        environment_file = Path(self.temp.name) / ".env"
        environment_file.write_text("this-file-must-not-be-read", encoding="utf-8")
        with mock.patch.object(flights, "TRUSTED_IMPORT_ROOTS", (Path(self.temp.name),)):
            report = flights.run_research(
                self.db, self._args("--transfer-profile-file", str(environment_file))
            )
        self.assertIn("transfer_sources", {item["field"] for item in report["follow_up_fields"]})
        self.assertEqual(report["transfer_reference"]["selected_profiles"], [])

    def test_explicit_fetch_is_bounded_and_api_failure_is_partial(self):
        seen = {}

        def failing_fetcher(_db, args):
            seen["single_page"] = args.single_page
            seen["max_results"] = args.max_results
            raise flights.ApiError("test provider outage", 503)

        report = flights.run_research(
            self.db, self._args("--fetch"), fetcher=failing_fetcher, prepare_api=lambda: None
        )
        self.assertTrue(seen["single_page"])
        self.assertLessEqual(seen["max_results"], research.MAX_FETCH_RESULTS)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["award_search"]["status"], "fetch_failed")
        self.assertEqual(report["cash_search"]["status"], "ready_for_browser")

    def test_fetch_bounds_are_reported_as_follow_ups_without_loading_credentials(self):
        parser = flights.make_parser()
        args = parser.parse_args([
            "research", "--provider", "seats.aero",
            '{"origin":"SFO,LAX,JFK,ORD","destination":"CDG","departure_date":"2026-09-01"}',
            "--fetch", "--json",
        ])
        flights.ensure_positive(args)
        fetcher = mock.Mock(side_effect=AssertionError("fetch must be blocked"))
        prepare_api = mock.Mock(side_effect=AssertionError("credentials must not load"))

        report = flights.run_research(self.db, args, fetcher=fetcher, prepare_api=prepare_api)

        self.assertEqual(report["status"], "needs_clarification")
        self.assertEqual(report["award_search"]["fetch"]["status"], "blocked")
        self.assertIn("origin", {item["field"] for item in report["follow_up_fields"]})
        fetcher.assert_not_called()
        prepare_api.assert_not_called()

    def test_seats_adapter_rejects_unknown_program_ids_and_case_normalizes_its_limit(self):
        parser = flights.make_parser()
        too_small = parser.parse_args([
            "research", "--provider", "SEATS.AERO", "--max-results", "1",
            '{"origin":"SFO","destination":"CDG","departure_date":"2026-09-01"}', "--json",
        ])
        with self.assertRaises(flights.FlightError):
            flights.ensure_positive(too_small)

        args = parser.parse_args([
            "research", "--provider", "seats.aero",
            '{"origin":"SFO","destination":"CDG","departure_date":"2026-09-01",'
            '"points":{"programs":["not_a_seats_program"]}}',
            "--fetch", "--json",
        ])
        flights.ensure_positive(args)
        fetcher = mock.Mock(side_effect=AssertionError("unknown Seats program must not reach fetcher"))
        prepare_api = mock.Mock(side_effect=AssertionError("unknown Seats program must not load credentials"))
        report = flights.run_research(self.db, args, fetcher=fetcher, prepare_api=prepare_api)

        self.assertEqual(report["status"], "needs_clarification")
        self.assertIn("programs", {item["field"] for item in report["follow_up_fields"]})
        fetcher.assert_not_called()
        prepare_api.assert_not_called()

    def test_init_does_not_load_dotenv(self):
        output = io.StringIO()
        db_path = Path(self.temp.name) / "init.sqlite3"
        with mock.patch.object(flights, "load_dotenv", side_effect=AssertionError("init must not load dotenv")):
            with redirect_stdout(output):
                result = flights.main(["--db", str(db_path), "init"])
        self.assertEqual(result, 0)
        self.assertIn("Initialized", output.getvalue())

    def test_main_research_dry_run_does_not_load_dotenv(self):
        output = io.StringIO()
        db_path = Path(self.temp.name) / "main.sqlite3"
        with mock.patch.object(flights, "load_dotenv", side_effect=AssertionError("must not load")), \
             mock.patch.object(flights, "Database", side_effect=AssertionError("manual research must not open a database")):
            with redirect_stdout(output):
                result = flights.main([
                    "--db", str(db_path), "research", "--brief",
                    '{"origin":"SFO","destination":"CDG","departure_date":"2026-09-01"}', "--fetch", "--json",
                ])
        self.assertEqual(result, 0)
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["award_search"]["status"], "manual_import_empty")
        self.assertEqual(parsed["award_search"]["provider"], "manual_import")
        self.assertEqual(parsed["award_search"]["fetch"]["status"], "not_supported")


if __name__ == "__main__":
    unittest.main()
