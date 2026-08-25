import argparse
import sqlite3
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import flights  # noqa: E402


SAMPLE = {
    "ID": "avail-1",
    "RouteID": "route-1",
    "Route": {
        "OriginAirport": "SFO",
        "OriginRegion": "North America",
        "DestinationAirport": "JFK",
        "DestinationRegion": "North America",
        "Distance": 2582,
        "Source": "alaska",
    },
    "Date": "2026-09-01",
    "JAvailable": True,
    "JMileageCost": "45000",
    "JRemainingSeats": 2,
    "JAirlines": "AA",
    "JDirect": True,
    "YAvailable": True,
    "YMileageCost": "12500",
    "YRemainingSeats": 0,
    "YAirlines": "AA",
    "YDirect": True,
    "Source": "alaska",
    "AvailabilityTrips": [
        {
            "ID": "trip-1",
            "AvailabilityID": "avail-1",
            "Cabin": "business",
            "MileageCost": 45000,
            "TotalTaxes": 560,
            "TaxesCurrency": "USD",
            "TaxesCurrencySymbol": "$",
            "RemainingSeats": 2,
            "Stops": 0,
            "TotalDuration": 330,
            "Carriers": "AA",
            "FlightNumbers": "AA123",
            "DepartsAt": "2026-09-01T08:00:00Z",
            "ArrivesAt": "2026-09-01T16:30:00Z",
            "Source": "alaska",
            "AvailabilitySegments": [
                {
                    "ID": "segment-1",
                    "Order": 0,
                    "FlightNumber": "AA123",
                    "OriginAirport": "SFO",
                    "DestinationAirport": "JFK",
                    "DepartsAt": "2026-09-01T08:00:00Z",
                    "ArrivesAt": "2026-09-01T16:30:00Z",
                    "AircraftName": "Airbus A321",
                    "AircraftCode": "321",
                    "FareClass": "I",
                }
            ],
        }
    ],
}


class ValidationTests(unittest.TestCase):
    def test_normalizers(self):
        self.assertEqual(flights.validate_airports("sfo, lax,sfo"), "SFO,LAX")
        self.assertEqual(flights.validate_cabins("premium-economy,business"), "premium,business")
        self.assertEqual(flights.validate_cabins(""), "")
        self.assertEqual(flights.validate_programs(""), "")

    def test_bad_airport_rejected(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            flights.validate_airports("San Francisco")

    def test_nonfinite_ttl_and_cpp_are_rejected(self):
        parser = flights.make_parser()
        for value in ("nan", "inf", "-inf"):
            args = parser.parse_args([
                "research", '{"origin":"SFO","destination":"CDG","departure_date":"2026-09-01"}',
                f"--cache-ttl-hours={value}",
            ])
            with self.assertRaises(flights.FlightError):
                flights.ensure_positive(args)
        args = parser.parse_args(["results", "--cpp", "nan"])
        with self.assertRaises(flights.FlightError):
            flights.ensure_positive(args)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = flights.Database(Path(self.temp.name) / "test.sqlite3")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def make_search(self):
        args = argparse.Namespace(
            origins="SFO",
            destinations="JFK",
            start_date="2026-09-01",
            end_date="2026-09-01",
            cabins="business",
            sources="",
            carriers="",
            direct=False,
            seats=1,
            summary_only=False,
            max_results=10,
        )
        return self.db.create_search(args)

    def test_store_normalized_availability_trip_and_segment(self):
        search_id = self.make_search()
        count = self.db.store_search_payload(search_id, [SAMPLE])
        self.db.finish_search(search_id, status="complete", result_count=count)

        candidates = self.db.candidates(search_id=search_id)
        business = [item for item in candidates if item["cabin"] == "business"]
        economy = [item for item in candidates if item["cabin"] == "economy"]
        self.assertEqual(count, 1)
        self.assertEqual(len(business), 1)
        self.assertEqual(business[0]["kind"], "trip")
        self.assertEqual(business[0]["flight_numbers"], "AA123")
        self.assertEqual(business[0]["segments"][0]["aircraft_code"], "321")
        self.assertEqual(economy[0]["kind"], "summary")

    def test_deduplicates_search_result_ids(self):
        search_id = self.make_search()
        count = self.db.store_search_payload(search_id, [SAMPLE, SAMPLE])
        self.assertEqual(count, 1)
        linked = self.db.conn.execute(
            "SELECT COUNT(*) FROM search_results WHERE search_id=?", (search_id,)
        ).fetchone()[0]
        self.assertEqual(linked, 1)

    def test_new_summary_response_does_not_reuse_old_trip_detail(self):
        first_search = self.make_search()
        self.db.store_search_payload(first_search, [SAMPLE])
        self.db.finish_search(first_search, status="complete", result_count=1)
        self.assertEqual(
            [item["kind"] for item in self.db.candidates(search_id=first_search) if item["cabin"] == "business"],
            ["trip"],
        )

        refreshed = {**SAMPLE, "AvailabilityTrips": None}
        second_search = self.make_search()
        self.db.store_search_payload(second_search, [refreshed])
        self.db.finish_search(second_search, status="complete", result_count=1)
        refreshed_candidates = [
            item for item in self.db.candidates(search_id=second_search) if item["cabin"] == "business"
        ]
        self.assertEqual([item["kind"] for item in refreshed_candidates], ["summary"])

    def test_stale_trip_timestamp_is_suppressed_even_for_legacy_rows(self):
        search_id = self.make_search()
        self.db.store_search_payload(search_id, [SAMPLE])
        self.db.conn.execute(
            "UPDATE availability SET fetched_at='9999-01-01T00:00:00+00:00' WHERE id=?",
            (SAMPLE["ID"],),
        )
        self.db.conn.commit()
        candidates = [item for item in self.db.candidates(search_id=search_id) if item["cabin"] == "business"]
        self.assertEqual([item["kind"] for item in candidates], ["summary"])


class MigrationTests(unittest.TestCase):
    def test_existing_database_gains_trip_detail_generation_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "legacy.sqlite3"
            legacy = sqlite3.connect(path)
            legacy.execute(
                """
                CREATE TABLE availability (
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
                    booking_links_json TEXT,
                    raw_json TEXT NOT NULL
                )
                """
            )
            legacy.commit()
            legacy.close()

            db = flights.Database(path)
            try:
                columns = {row["name"] for row in db.conn.execute("PRAGMA table_info(availability)")}
                self.assertIn("trip_details_fetched_at", columns)
            finally:
                db.close()


class RankingTests(unittest.TestCase):
    def test_known_insufficient_seats_are_removed_but_unknown_remains(self):
        base = {
            "kind": "trip",
            "cabin": "business",
            "points": 50000,
            "taxes_cents": 500,
            "stops": 0,
            "direct": True,
            "date": "2026-09-01",
            "program": "aeroplan",
        }
        known = {**base, "id": "known", "seats": 1}
        unknown = {**base, "id": "unknown", "seats": 0}
        ranked = flights.rank_candidates([known, unknown], seats=2)
        self.assertEqual([item["id"] for item in ranked], ["unknown"])

    def test_best_sort_penalizes_stops(self):
        base = {
            "kind": "trip",
            "cabin": "business",
            "taxes_cents": 500,
            "seats": 2,
            "direct": True,
            "date": "2026-09-01",
            "program": "aeroplan",
        }
        nonstop = {**base, "id": "nonstop", "points": 50000, "stops": 0}
        connection = {**base, "id": "connection", "points": 48000, "stops": 1, "direct": False}
        ranked = flights.rank_candidates([connection, nonstop], seats=1, sort="best")
        self.assertEqual(ranked[0]["id"], "nonstop")


if __name__ == "__main__":
    unittest.main()
