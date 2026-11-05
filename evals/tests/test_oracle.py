import unittest

from harness_fixtures import synthetic_case

from case_oracle import Oracle, OracleError, parse_aware, to_min


def m(ts):
    return to_min(parse_aware(ts))


class OracleTests(unittest.TestCase):
    def setUp(self):
        self.o = Oracle(synthetic_case()["oracle"])

    def test_full_valid_example(self):
        self.assertEqual(self.o.check_assignment({"S1": m("2026-11-10T10:00:00-08:00"),
                                                  "S2": m("2026-11-10T11:10:00-08:00")}, {}), [])

    def test_partial_with_valid_completion(self):
        self.assertEqual(self.o.check_partial({"S1": m("2026-11-10T10:20:00-08:00")}, {}), [])

    def test_partial_without_completion(self):
        problems = self.o.check_partial({"S1": m("2026-11-10T10:30:00-08:00")}, {})
        self.assertTrue(problems and "cannot be completed" in problems[0])

    def test_partial_unavailable_attendee(self):
        problems = self.o.check_partial({"S1": m("2026-11-10T09:30:00-08:00")}, {})
        self.assertIn("S1: int_a unavailable", problems)

    def test_gap_violation_in_full_assignment(self):
        problems = self.o.check_assignment({"S1": m("2026-11-10T10:00:00-08:00"),
                                            "S2": m("2026-11-10T11:00:00-08:00")}, {})
        self.assertTrue(any("gap S1->S2" in p for p in problems))

    def test_unknown_session(self):
        self.assertTrue(self.o.check_partial({"S9": 0}, {}))

    def test_bad_spec_raises(self):
        with self.assertRaises(OracleError):
            Oracle({"search_window": {"start": "x", "end": "y"}})

    def test_pool_requires_named_members(self):
        spec = synthetic_case()["oracle"]
        spec["participants"]["int_c"] = {"available": [["2026-11-10T10:00:00-08:00", "2026-11-10T12:00:00-08:00"]]}
        spec["sessions"][1] = {"id": "S2", "duration_min": 30, "attendees": ["candidate"],
                               "pool": {"from": ["int_b", "int_c"], "count": 1}}
        o = Oracle(spec)
        starts = {"S1": m("2026-11-10T10:00:00-08:00"), "S2": m("2026-11-10T11:10:00-08:00")}
        self.assertTrue(o.check_assignment(starts, {}))
        self.assertEqual(o.check_assignment(starts, {"S2": ["int_c"]}), [])
        self.assertTrue(o.check_assignment(starts, {"S2": ["int_a"]}))


if __name__ == "__main__":
    unittest.main()
