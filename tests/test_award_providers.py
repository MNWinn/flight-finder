import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import award_providers  # noqa: E402
import flights  # noqa: E402
import research  # noqa: E402


def sample_offer(
    *,
    provider_id="licensed_alpha",
    provider_name="Licensed Alpha",
    provider_offer_id="offer-1",
    points=50000,
    availability_mode="manual_import",
    seats=2,
):
    return {
        "schema_version": 1,
        "offer_id": f"{provider_id}:offer:{provider_offer_id}",
        "provider": {"id": provider_id, "name": provider_name},
        "provider_offer_id": provider_offer_id,
        "redemption_program": {
            "id": "aeroplan",
            "name": "Air Canada Aeroplan",
            "provider_program_id": "ac-program",
        },
        "itinerary": {
            "origin": "SFO",
            "destination": "CDG",
            "departure_date": "2026-09-01",
            "segments": [{
                "origin": "SFO",
                "destination": "CDG",
                "departs_at": "2026-09-01T10:00:00Z",
                "arrives_at": "2026-09-02T05:00:00Z",
                "operating_carrier": "UA",
                "marketing_carrier": "UA",
                "flight_number": "UA990",
            }],
            "operating_carriers": ["UA"],
            "marketing_carriers": ["UA"],
        },
        "cabin": "business",
        "award": {"points": points, "per_passenger": True},
        "taxes": {"cents": 560, "currency": "USD", "symbol": "$", "per_passenger": True},
        "seat_availability": {"count": seats, "confidence": "reported"},
        "detail_level": "flight_level",
        "booking_links": [{"url": "https://example.test/book", "label": "Source link"}],
        "evidence": {
            "availability_mode": availability_mode,
            "source_updated_at": "2026-01-01T11:30:00Z",
            "fetched_at": "2026-01-01T12:00:00Z",
        },
    }


class AwardOfferContractTests(unittest.TestCase):
    def test_contract_namespaces_ids_and_never_treats_unknown_inventory_as_sold_out(self):
        payload = sample_offer()
        payload["offer_id"] = "opaque-id"
        payload["fees"] = payload.pop("taxes")
        payload["seat_availability"] = {"count": 0, "confidence": "reported"}
        offers, issues = award_providers.parse_award_offers([payload])

        self.assertEqual(issues, [])
        self.assertEqual(offers[0].offer_id, "licensed_alpha:opaque-id")
        normalized = offers[0].to_dict()
        self.assertEqual(normalized["taxes"]["currency"], "USD")
        self.assertIsNone(normalized["seat_availability"]["count"])
        self.assertEqual(normalized["seat_availability"]["confidence"], "unknown")
        self.assertIsNone(offers[0].to_candidate()["seats"])
        self.assertEqual(normalized["evidence"]["verification_status"], "not_independently_verified")

    def test_contract_rejects_missing_provenance_timestamp_and_bad_fee_currency(self):
        no_timestamp = sample_offer()
        no_timestamp["evidence"] = {"availability_mode": "manual_import"}
        bad_currency = sample_offer(provider_offer_id="offer-2")
        bad_currency["taxes"] = {"cents": 560, "currency": "US", "symbol": "$"}
        no_points = sample_offer(provider_offer_id="offer-3")
        del no_points["award"]["points"]

        offers, issues = award_providers.parse_award_offers([no_timestamp, bad_currency, no_points])

        self.assertEqual(offers, [])
        self.assertEqual(len(issues), 3)
        self.assertTrue(any("timestamp" in issue["reason"] for issue in issues))
        self.assertTrue(any("currency" in issue["reason"] for issue in issues))
        self.assertTrue(any("award.points" in issue["reason"] for issue in issues))

    def test_flight_level_requires_route_consistent_segments_and_tax_scope(self):
        empty_segment = sample_offer()
        empty_segment["itinerary"]["segments"] = [{}]
        missing_tax_scope = sample_offer(provider_offer_id="offer-2")
        del missing_tax_scope["taxes"]["per_passenger"]

        offers, issues = award_providers.parse_award_offers([empty_segment, missing_tax_scope])

        self.assertEqual(offers, [])
        self.assertTrue(any("flight_level itinerary.segments" in issue["reason"] for issue in issues))
        self.assertTrue(any("taxes.per_passenger" in issue["reason"] for issue in issues))

    def test_public_booking_urls_drop_query_and_reject_userinfo(self):
        payload = sample_offer()
        payload["booking_links"] = [{"url": "https://example.test/book?session=demo#fragment"}]
        offer = award_providers.normalize_award_offer(payload)
        self.assertEqual(offer.to_dict()["booking_links"], [{"url": "https://example.test/book"}])

        unsafe = sample_offer(provider_offer_id="offer-2")
        unsafe["booking_links"] = [{"url": "https://user:pass@example.test/book"}]
        _, issues = award_providers.parse_award_offers([unsafe])
        self.assertTrue(any("valid http(s) URL" in issue["reason"] for issue in issues))

    def test_provider_program_groups_do_not_conflate_same_program(self):
        alpha = award_providers.normalize_award_offer(sample_offer())
        beta = award_providers.normalize_award_offer(sample_offer(
            provider_id="licensed_beta",
            provider_name="Licensed Beta",
            provider_offer_id="offer-2",
            points=45000,
        ))
        brief = research.parse_trip_brief(
            {"origin": "SFO", "destination": "CDG", "departure_date": "2026-09-01"}, {}
        )
        groups = research.group_award_recommendations(
            [alpha.to_candidate(), beta.to_candidate()], brief, {}, [], 5
        )

        self.assertEqual([group["provider"] for group in groups], ["licensed_alpha", "licensed_beta"])
        self.assertEqual([group["program"] for group in groups], ["aeroplan", "aeroplan"])


class GenericResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = flights.Database(Path(self.temp.name) / "research.sqlite3")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def _args(self, *extra):
        parser = flights.make_parser()
        args = parser.parse_args([
            "research",
            '{"origin":"SFO","destination":"CDG","departure_date":"2026-09-01"}',
            "--json",
            *extra,
        ])
        flights.ensure_positive(args)
        return args

    def test_default_research_with_fetch_needs_no_key_or_provider_call(self):
        fetcher = mock.Mock(side_effect=AssertionError("default import must not fetch"))
        prepare_api = mock.Mock(side_effect=AssertionError("default import must not load credentials"))

        report = flights.run_research(
            None, self._args("--fetch"), fetcher=fetcher, prepare_api=prepare_api
        )

        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["award_search"]["provider"], "manual_import")
        self.assertEqual(report["award_search"]["status"], "manual_import_empty")
        self.assertEqual(report["award_search"]["fetch"]["status"], "not_supported")
        self.assertFalse(report["award_search"]["provenance"]["network_used"])
        fetcher.assert_not_called()
        prepare_api.assert_not_called()

    def test_inline_manual_import_preserves_source_claim_without_verifying_it(self):
        payload = sample_offer(availability_mode="provider_live_offer")
        report = flights.run_research(self.db, self._args("--award-offer", json.dumps(payload)))

        self.assertEqual(report["award_search"]["status"], "manual_import_ready")
        self.assertEqual(report["award_search"]["candidate_count"], 1)
        recommendation = report["award_search"]["recommendations_by_program"][0]["tax_currency_buckets"][0]["recommendations"][0]
        evidence = recommendation["research_evidence"]
        self.assertEqual(evidence["provider"], "licensed_alpha")
        self.assertEqual(evidence["provider_mode"], "provider_live_offer")
        self.assertEqual(evidence["provider_observation_state"], "provider_live_offer_claimed_by_import")
        self.assertEqual(evidence["verification_status"], "not_independently_verified")
        self.assertTrue(evidence["imported_manually"])

    def test_file_manual_import_and_environment_file_rejection(self):
        offer_file = Path(self.temp.name) / "award-offers.json"
        offer_file.write_text(json.dumps({"offers": [sample_offer()]}), encoding="utf-8")
        environment_path = Path(self.temp.name) / ".env"
        environment_path.write_text("must-not-be-read", encoding="utf-8")
        with mock.patch.object(flights, "TRUSTED_IMPORT_ROOTS", (Path(self.temp.name),)):
            report = flights.run_research(self.db, self._args("--award-offer-file", str(offer_file)))
            rejected = flights.run_research(self.db, self._args("--award-offer-file", str(environment_path)))
        self.assertEqual(report["award_imports"]["accepted_count"], 1)
        self.assertEqual(report["award_search"]["candidate_count"], 1)
        self.assertEqual(rejected["award_imports"]["accepted_count"], 0)
        self.assertTrue(any("must not be an environment file" in item["reason"] for item in rejected["award_imports"]["issues"]))

    def test_manifest_makes_optional_compatibility_boundary_visible(self):
        manifest = {item["id"]: item for item in flights.provider_manifest()}
        self.assertFalse(manifest["manual_import"]["requires_credentials"])
        self.assertTrue(manifest["seats.aero"]["requires_credentials"])
        self.assertTrue(manifest["seats.aero"]["requires_explicit_selection"])
        self.assertTrue(manifest["seats.aero"]["strict_program_catalog"])
        self.assertFalse(manifest["google_flights_handoff"]["network_access"])

    def test_manual_import_coverage_is_unknown_and_evidence_age_is_exposed(self):
        report = flights.run_research(
            self.db, self._args("--award-offer", json.dumps(sample_offer()))
        )
        coverage = report["award_search"]["coverage"]
        self.assertEqual(coverage["coverage_status"], "unknown_unverified")
        self.assertEqual(coverage["completeness"], "unknown")
        self.assertIsNone(coverage["may_be_truncated"])
        self.assertEqual(coverage["newest_evidence_at"], "2026-01-01T12:00:00+00:00")
        self.assertIsInstance(coverage["newest_evidence_age_seconds"], int)
        self.assertIn("Import coverage: unknown/unverified", flights.research_text(report))

    def test_free_text_generic_program_constraint_is_not_dropped_for_manual_imports(self):
        united = sample_offer()
        united["redemption_program"] = {
            "id": "united",
            "name": "United MileagePlus",
            "provider_program_id": "united-provider-id",
        }
        parser = flights.make_parser()
        args = parser.parse_args([
            "research", "SFO to CDG 2026-09-01 Aeroplan",
            "--award-offer", json.dumps(united), "--json",
        ])
        flights.ensure_positive(args)
        report = flights.run_research(self.db, args)

        self.assertEqual(report["brief"]["points"]["programs"], ["aeroplan"])
        self.assertEqual(report["brief"]["points"]["source"], "user")
        self.assertEqual(report["award_search"]["status"], "manual_import_no_match")
        self.assertEqual(report["award_search"]["candidate_count"], 0)

    def test_cpp_uses_declared_total_award_and_tax_scopes(self):
        payload = sample_offer()
        payload["award"]["per_passenger"] = False
        payload["taxes"]["per_passenger"] = False
        candidate = award_providers.normalize_award_offer(payload).to_candidate()
        quotes, issues = research.parse_manual_cash_quotes([{
            "total": 1200,
            "currency": "USD",
            "amount_scope": "total",
            "origin": "SFO",
            "destination": "CDG",
            "departure_date": "2026-09-01",
            "cabin": "business",
            "passengers": 2,
            "same_itinerary": True,
            "fare_inclusions_match": True,
            "observed_at": "2026-01-01T12:00:00Z",
            "booking_url": "https://example.test/search?session=demo",
        }])
        self.assertEqual(issues, [])
        comparisons = research.compare_award_to_cash(
            candidate,
            quotes,
            2,
            [{
                "status": "direct_reference",
                "source_points_to_transfer": 50000,
                "point_source": "example",
                "point_source_name": "Example",
            }],
        )
        self.assertEqual(comparisons[0]["state"], "user_asserted_comparable")
        self.assertEqual(comparisons[0]["award_points_total"], 50000)
        self.assertEqual(comparisons[0]["award_taxes_total_cents"], 560)
        self.assertEqual(comparisons[0]["cash_booking_url"], "https://example.test/search")

    def test_cpp_is_suppressed_when_an_adapter_cannot_establish_tax_scope(self):
        candidate = award_providers.normalize_award_offer(sample_offer()).to_candidate()
        candidate["taxes_per_passenger"] = None
        quotes, _ = research.parse_manual_cash_quotes([{
            "total": 1200,
            "currency": "USD",
            "amount_scope": "total",
            "origin": "SFO",
            "destination": "CDG",
            "departure_date": "2026-09-01",
            "cabin": "business",
            "passengers": 1,
            "same_itinerary": True,
            "fare_inclusions_match": True,
            "observed_at": "2026-01-01T12:00:00Z",
            "booking_url": "https://example.test/search",
        }])
        comparison = research.compare_award_to_cash(
            candidate,
            quotes,
            1,
            [{
                "status": "direct_reference",
                "source_points_to_transfer": 50000,
                "point_source": "example",
                "point_source_name": "Example",
            }],
        )
        self.assertEqual(comparison[0]["state"], "not_comparable")
        self.assertIn("tax scope", comparison[0]["reason"])

    def test_file_reader_uses_trusted_regular_non_symlink_roots_and_size_limit(self):
        trusted_root = Path(self.temp.name) / "trusted"
        trusted_root.mkdir()
        allowed = trusted_root / "allowed.json"
        allowed.write_text("{}", encoding="utf-8")
        outside = Path(self.temp.name) / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        symlink = trusted_root / "linked.json"
        os.symlink(allowed, symlink)
        oversized = trusted_root / "oversized.json"
        oversized.write_text("{}", encoding="utf-8")

        with mock.patch.object(flights, "TRUSTED_IMPORT_ROOTS", (trusted_root,)):
            self.assertEqual(flights._read_json_file(allowed, "test import"), {})
            with self.assertRaisesRegex(flights.FlightError, "trusted import root"):
                flights._read_json_file(outside, "test import")
            with self.assertRaisesRegex(flights.FlightError, "must not be a symlink"):
                flights._read_json_file(symlink, "test import")
            with mock.patch.object(flights, "MAX_IMPORT_FILE_BYTES", 1):
                with self.assertRaisesRegex(flights.FlightError, "import limit"):
                    flights._read_json_file(oversized, "test import")

    def test_unavailable_adapter_snapshot_is_partial_not_ready(self):
        class UnavailableAdapter:
            id = "unavailable_test"
            display_name = "Unavailable test adapter"
            program_catalog = {}
            limits = award_providers.ProviderLimits()

            def find_cached(self, request):
                return award_providers.AwardSearchSnapshot(
                    provider_id=self.id,
                    provider_name=self.display_name,
                )

            def fetch(self, request):
                return self.find_cached(request)

        parser = flights.make_parser()
        args = parser.parse_args([
            "research", "--provider", "unavailable_test",
            '{"origin":"SFO","destination":"CDG","departure_date":"2026-09-01"}', "--json",
        ])
        flights.ensure_positive(args)
        report = flights.run_research(
            None, args, provider_registry=award_providers.ProviderRegistry((UnavailableAdapter(),))
        )
        self.assertEqual(report["award_search"]["status"], "unavailable")
        self.assertEqual(report["status"], "partial")

    def test_custom_adapter_uses_its_own_bounds_and_shared_report_shape(self):
        class FakeLicensedAdapter:
            id = "licensed_fake"
            display_name = "Licensed Fake"
            program_catalog = {}
            limits = award_providers.ProviderLimits(
                supports_fetch=True,
                network_access=False,
                requires_explicit_selection=True,
                description="Test-only local adapter",
            )

            def __init__(self):
                self.fetched = False

            def find_cached(self, request):
                return award_providers.AwardSearchSnapshot(
                    provider_id=self.id,
                    provider_name=self.display_name,
                    status="cache_miss",
                    fetch_supported=True,
                )

            def fetch(self, request):
                self.fetched = True
                return award_providers.AwardSearchSnapshot(
                    provider_id=self.id,
                    provider_name=self.display_name,
                    status="fetched",
                    fetch_supported=True,
                    coverage={"may_be_truncated": False, "trip_details_requested": True},
                )

        adapter = FakeLicensedAdapter()
        registry = award_providers.ProviderRegistry((adapter,))
        parser = flights.make_parser()
        args = parser.parse_args([
            "research", "--provider", "licensed_fake",
            '{"origin":"SFO,LAX,JFK,ORD","destination":"CDG","departure_date":"2026-09-01"}',
            "--fetch", "--json",
        ])
        flights.ensure_positive(args)

        report = flights.run_research(self.db, args, provider_registry=registry)

        self.assertTrue(adapter.fetched)
        self.assertEqual(report["award_search"]["provider"], "licensed_fake")
        self.assertEqual(report["award_search"]["fetch"]["status"], "completed")
        self.assertEqual(report["follow_up_fields"], [])


if __name__ == "__main__":
    unittest.main()
