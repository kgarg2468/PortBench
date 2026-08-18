"""Tests for the aggregator. Stdlib only.

    python3 -m analysis.tests        (from the repo root)
    python3 analysis/tests.py

The fixtures under analysis/fixtures/ are synthetic and hand-built to hit the cases that are
easy to get wrong and invisible when they are wrong: an excluded task quietly inflating a
denominator, a transport error scored as a failure, a backfill run that does not take effect,
a multi-code attempt double-counted, a $0.00 price tag on a run that reported no usage.

The expected numbers below are derived by hand from the fixtures, not from the code. If a
change to aggregate.py moves one of them, the burden is on the change.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import aggregate, buckets  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXCLUSIONS = FIXTURES / "exclusions.json"


def build(models=None, runs="full-*.jsonl"):
    return aggregate.aggregate(FIXTURES, EXCLUSIONS, models, runs)


def stats_by_model(built):
    return {s.model: s for s in built["_stats"]}


class TestBucketMapping(unittest.TestCase):

    def test_known_codes_land_in_their_bucket(self):
        self.assertEqual(buckets.bucket_for_code("E0382"), "borrow")
        self.assertEqual(buckets.bucket_for_code("E0597"), "borrow")
        self.assertEqual(buckets.bucket_for_code("E0308"), "types")
        self.assertEqual(buckets.bucket_for_code("E0599"), "types")
        self.assertEqual(buckets.bucket_for_code("E0432"), "imports")
        self.assertEqual(buckets.bucket_for_code("E0433"), "imports")

    def test_unknown_code_falls_back_to_other_compile(self):
        self.assertEqual(buckets.bucket_for_code("E9999"), "other_compile")
        self.assertEqual(buckets.bucket_for_code(""), "other_compile")

    def test_multi_code_attempt_uses_the_first_code_not_the_lowest(self):
        # The list is deliberately not in ascending order here: the rule is "index 0", so a
        # borrow code in front of a type code must bucket as borrow.
        self.assertEqual(
            buckets.bucket_for_attempt("COMPILE_ERROR", ["E0382", "E0308"]), "borrow")
        self.assertEqual(
            buckets.bucket_for_attempt("COMPILE_ERROR", ["E0308", "E0382"]), "types")

    def test_compile_error_without_codes_is_other_compile(self):
        self.assertEqual(buckets.bucket_for_attempt("COMPILE_ERROR", []), "other_compile")
        self.assertEqual(buckets.bucket_for_attempt("COMPILE_ERROR", None), "other_compile")

    def test_verdict_beats_codes(self):
        # A TEST_FAIL that happens to carry a scraped rustc code is still test logic, and a
        # TIMEOUT that carries one is still infrastructure.
        self.assertEqual(buckets.bucket_for_attempt("TEST_FAIL", ["E0308"]), "test_fail")
        self.assertEqual(buckets.bucket_for_attempt("TIMEOUT", ["E0308"]), "harness")
        self.assertEqual(buckets.bucket_for_attempt("EXTRACTION_ERROR", []), "harness")
        self.assertEqual(buckets.bucket_for_attempt("TRANSPORT_ERROR", []), "harness")
        self.assertEqual(buckets.bucket_for_attempt("SUITE_ERROR", []), "harness")

    def test_repair_eligible_mirrors_the_harness(self):
        # If harness/run.py:REPAIR_ELIGIBLE changes, this table has to follow it or
        # repair_missing starts flagging tasks that ended exactly as designed.
        self.assertEqual(set(buckets.REPAIR_ELIGIBLE),
                         {"COMPILE_ERROR", "TEST_FAIL", "SUITE_ERROR"})
        self.assertNotIn("TIMEOUT", buckets.REPAIR_ELIGIBLE)

    def test_every_listed_code_maps_back_to_its_own_bucket(self):
        for bucket in buckets.BUCKETS:
            for code in bucket["codes"]:
                self.assertEqual(buckets.bucket_for_code(code), bucket["id"],
                                 f"{code} should be in {bucket['id']}")


class TestCost(unittest.TestCase):

    def test_null_tokens_give_null_cost_never_zero(self):
        self.assertIsNone(buckets.cost_usd("fable", None, None))
        self.assertIsNone(buckets.cost_usd("fable", 1000, None))
        self.assertIsNone(buckets.cost_usd("fable", None, 1000))

    def test_unpriced_model_gives_null_cost(self):
        self.assertIsNone(buckets.cost_usd("some-unreleased-model", 1000, 1000))

    def test_priced_model_converts_per_mtok(self):
        price = buckets.price_for("fable")
        self.assertAlmostEqual(
            buckets.cost_usd("fable", 1_000_000, 1_000_000), price["in"] + price["out"])

    def test_model_with_no_usage_at_all_reports_null_cost_never_zero(self):
        # Every attempt in this synthetic model reported null usage.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ghost").mkdir()
            (root / "ghost" / "full-ghost-01.jsonl").write_text(json.dumps({
                "run_id": "full-ghost-01", "model": "haiku", "task_id": "T_PASS0",
                "project": "demo", "attempt": 0, "verdict": "PASS",
                "error_class_hint": [], "failing_tests": [],
                "tokens_in": None, "tokens_out": None, "duration_s": 1.0,
            }) + "\n", encoding="utf-8")
            built = aggregate.aggregate(root, EXCLUSIONS, None, "full-*.jsonl")
        record = built["leaderboard"]["models"][0]
        self.assertIsNone(record["cost_usd"])
        self.assertNotEqual(record["cost_usd"], 0)
        self.assertFalse(record["cost_available"])
        self.assertFalse(record["cost_partial"])
        self.assertEqual(record["cost_priced_attempts"], 0)
        self.assertIsNone(record["tokens"]["in"])
        self.assertEqual(record["tokens"]["attempts_without_usage"], 1)

    def test_fixture_cost_sums_only_attempts_that_reported_usage(self):
        alpha = stats_by_model(build())["fable"]
        # 11 scored attempts, one of which (T_EXTRACT) reported no usage.
        self.assertEqual(alpha.token_attempts, 10)
        self.assertEqual(alpha.token_missing_attempts, 1)
        self.assertEqual(alpha.tokens_in, 10_000)
        self.assertEqual(alpha.tokens_out, 5_000)
        price = buckets.price_for("fable")
        self.assertAlmostEqual(
            alpha.cost, 10_000 / 1e6 * price["in"] + 5_000 / 1e6 * price["out"])

    def test_partial_usage_is_flagged_as_a_floor(self):
        record = {m["model"]: m for m in build()["leaderboard"]["models"]}["fable"]
        self.assertTrue(record["cost_available"])
        self.assertTrue(record["cost_partial"])
        self.assertEqual(record["cost_priced_attempts"], 10)
        self.assertEqual(record["cost_unpriced_attempts"], 1)

    def test_fully_priced_model_is_not_flagged_partial(self):
        record = {m["model"]: m for m in build()["leaderboard"]["models"]}["gpt-5.6-terra"]
        self.assertTrue(record["cost_available"])
        self.assertFalse(record["cost_partial"])
        self.assertEqual(record["cost_unpriced_attempts"], 0)

    def test_timeout_tokens_are_counted(self):
        # The TIMEOUT task stays scored, so its 1000/500 must be in the totals. Dropping it
        # would show up as 9000/4500 here.
        alpha = stats_by_model(build())["fable"]
        self.assertIn("T_TIMEOUT", alpha.scored)
        self.assertEqual(alpha.tokens_in, 10_000)


class TestExclusions(unittest.TestCase):

    def test_task_ids_normalise_across_the_txt_suffix(self):
        excluded, effective = aggregate.load_exclusions(EXCLUSIONS)
        self.assertEqual(excluded, {"T_EXCL_1", "T_EXCL_2"})
        self.assertEqual(effective, 9)
        self.assertEqual(aggregate.normalize_task_id("T_EXCL_1.txt"), "T_EXCL_1")
        self.assertEqual(aggregate.normalize_task_id("T_EXCL_1"), "T_EXCL_1")

    def test_excluded_tasks_leave_every_denominator(self):
        alpha = stats_by_model(build())["fable"]
        self.assertEqual(alpha.raw_tasks, 11)
        self.assertEqual(alpha.effective_tasks, 9)          # 11 raw - 2 excluded
        self.assertNotIn("T_EXCL_1", alpha.scored)
        self.assertNotIn("T_EXCL_2", alpha.scored)

    def test_an_excluded_pass_does_not_inflate_the_pass_count(self):
        # T_EXCL_2 is a PASS in the fixture; if exclusion were applied after counting it
        # would show up here.
        alpha = stats_by_model(build())["fable"]
        self.assertEqual(alpha.passed_oneshot, 2)           # T_PASS0 + backfilled T_BACKFILL

    def test_dataset_block_reports_raw_and_effective(self):
        dataset = build()["leaderboard"]["dataset"]
        self.assertEqual(dataset["tasks_raw"], 11)
        self.assertEqual(dataset["tasks_excluded"], 2)
        self.assertEqual(dataset["tasks_total"], 9)
        self.assertEqual(dataset["declared_effective_task_count"], 9)


class TestNoData(unittest.TestCase):

    def test_only_transport_error_is_no_data(self):
        self.assertEqual(tuple(buckets.NO_DATA_VERDICTS), ("TRANSPORT_ERROR",))

    def test_transport_error_with_no_repair_leaves_the_denominator(self):
        alpha = stats_by_model(build())["fable"]
        self.assertEqual(alpha.no_data, ["T_NODATA"])
        self.assertNotIn("T_NODATA", alpha.scored)
        self.assertEqual(alpha.n, 8)                        # 9 effective - 1 no-data

    def test_a_lone_attempt_zero_timeout_is_a_failure_not_no_data(self):
        # The suite hung on code the model actually produced. It stays in the denominator,
        # counts as a one-shot failure, and is bucketed — dropping it would inflate the rate.
        alpha = stats_by_model(build())["fable"]
        self.assertIn("T_TIMEOUT", alpha.scored)
        self.assertNotIn("T_TIMEOUT", alpha.no_data)
        self.assertEqual(alpha.scored["T_TIMEOUT"][0]["verdict"], "TIMEOUT")
        self.assertEqual(alpha.tax["attempt_0"]["harness"], 2)   # T_TIMEOUT + T_EXTRACT
        self.assertEqual(alpha.harness_breakdown["attempt_0"],
                         {"EXTRACTION_ERROR": 1, "TIMEOUT": 1})

    def test_a_lone_timeout_is_terminal_not_an_incomplete_repair(self):
        # TIMEOUT is not in the harness's REPAIR_ELIGIBLE set, so no repair round was ever
        # owed and the task is complete as recorded.
        alpha = stats_by_model(build())["fable"]
        self.assertNotIn("T_TIMEOUT", alpha.repair_missing)

    def test_repair_eligible_failure_without_a_repair_is_flagged(self):
        alpha = stats_by_model(build())["fable"]
        self.assertEqual(alpha.repair_missing, ["T_INCOMPLETE"])
        records = {m["model"]: m for m in build()["leaderboard"]["models"]}
        self.assertEqual(records["fable"]["repair_missing"], 1)
        self.assertEqual(records["fable"]["repair_missing_tasks"], ["T_INCOMPLETE"])
        # ...and it still counts as a one-shot failure rather than leaving the denominator.
        self.assertIn("T_INCOMPLETE", alpha.scored)

    def test_harness_breakdown_splits_the_infra_bucket_by_verdict(self):
        tax = {m["model"]: m for m in build()["taxonomy"]["models"]}["fable"]
        self.assertEqual(sum(tax["harness_breakdown"]["attempt_0"].values()),
                         tax["attempt_0"]["harness"])
        self.assertEqual(tax["harness_breakdown"]["attempt_1"], {})

    def test_no_data_count_reaches_the_leaderboard_record(self):
        records = {m["model"]: m for m in build()["leaderboard"]["models"]}
        self.assertEqual(records["fable"]["no_data"], 1)
        self.assertEqual(records["fable"]["no_data_tasks"], ["T_NODATA"])
        self.assertEqual(records["fable"]["tasks_attempted"], 8)

    def test_tasks_a_run_never_reached_are_reported_separately(self):
        beta = stats_by_model(build())["gpt-5.6-terra"]
        self.assertEqual(beta.n, 3)
        self.assertEqual(beta.not_attempted,
                         ["T_BACKFILL", "T_EXTRACT", "T_INCOMPLETE", "T_NODATA",
                          "T_TESTFAIL", "T_TIMEOUT"])


class TestBackfill(unittest.TestCase):

    def test_later_run_overrides_earlier_for_the_same_task_and_attempt(self):
        alpha = stats_by_model(build())["fable"]
        self.assertNotIn("T_BACKFILL", alpha.no_data)
        self.assertEqual(alpha.scored["T_BACKFILL"][0]["verdict"], "PASS")
        self.assertEqual(alpha.scored["T_BACKFILL"][0]["run_id"], "full-alpha-02")

    def test_without_the_backfill_run_the_task_is_no_data(self):
        # Same fixtures, only run 01 in scope.
        alpha = stats_by_model(build(runs="full-alpha-01.jsonl"))["fable"]
        self.assertEqual(alpha.no_data, ["T_BACKFILL", "T_NODATA"])
        self.assertEqual(alpha.n, 7)
        self.assertEqual(alpha.passed_oneshot, 1)

    def test_run_ids_are_recorded_on_the_model(self):
        records = {m["model"]: m for m in build()["leaderboard"]["models"]}
        self.assertEqual(records["fable"]["run_ids"], ["full-alpha-01", "full-alpha-02"])


class TestRepairMath(unittest.TestCase):

    def test_per_model_repair_stats(self):
        repair = {m["model"]: m for m in build()["leaderboard"]["repair"]["models"]}
        alpha = repair["fable"]
        self.assertEqual(alpha["failed_oneshot"], 6)        # 8 scored - 2 one-shot passes
        self.assertEqual(alpha["repairs_attempted"], 3)
        self.assertEqual(alpha["repairs_passed"], 1)        # T_REPAIR
        self.assertAlmostEqual(alpha["recovery_rate"], 1 / 6, places=4)
        self.assertAlmostEqual(alpha["recovery_rate_of_attempted"], 1 / 3, places=4)

        beta = repair["gpt-5.6-terra"]
        self.assertEqual(beta["failed_oneshot"], 2)
        self.assertEqual(beta["repairs_attempted"], 1)
        self.assertEqual(beta["repairs_passed"], 1)
        self.assertAlmostEqual(beta["recovery_rate"], 1 / 2)

    def test_pooled_repair_stats(self):
        pooled = build()["leaderboard"]["repair"]["pooled"]
        self.assertEqual(pooled["models"], 2)
        self.assertEqual(pooled["failed_oneshot"], 8)
        self.assertEqual(pooled["repairs_attempted"], 4)
        self.assertEqual(pooled["repairs_passed"], 2)
        # Rates are rounded to 4 dp on the way out; the site re-derives them from the counts.
        self.assertAlmostEqual(pooled["recovery_rate"], 2 / 8, places=4)

    def test_pass_after_repair_includes_one_shot_passes(self):
        records = {m["model"]: m for m in build()["leaderboard"]["models"]}
        self.assertEqual(records["fable"]["passed_oneshot"], 2)
        self.assertEqual(records["fable"]["passed_after_repair"], 3)
        self.assertEqual(records["gpt-5.6-terra"]["passed_oneshot"], 1)
        self.assertEqual(records["gpt-5.6-terra"]["passed_after_repair"], 2)


class TestTaxonomyOutput(unittest.TestCase):

    def test_buckets_sum_to_the_failure_count_for_each_attempt(self):
        built = build()
        lb = {m["model"]: m for m in built["leaderboard"]["models"]}
        for model in built["taxonomy"]["models"]:
            record = lb[model["model"]]
            for key in ("attempt_0", "attempt_1"):
                failures = sum(c for v, c in record["verdicts"][key].items() if v != "PASS")
                self.assertEqual(sum(model[key].values()), failures,
                                 f"{model['model']} {key}")

    def test_fixture_bucket_counts(self):
        tax = {m["model"]: m for m in build()["taxonomy"]["models"]}["fable"]
        self.assertEqual(tax["attempt_0"],
                         {"borrow": 2, "types": 0, "imports": 0, "other_compile": 1,
                          "test_fail": 1, "harness": 2})
        self.assertEqual(tax["attempt_1"],
                         {"borrow": 0, "types": 0, "imports": 1, "other_compile": 0,
                          "test_fail": 1, "harness": 0})

    def test_top_codes_count_every_code_on_an_attempt_zero_failure(self):
        top = {c["code"]: c for c in build()["taxonomy"]["top_codes"]}
        self.assertEqual(top["E0382"]["count"], 1)
        self.assertEqual(top["E0382"]["bucket"], "borrow")
        self.assertEqual(top["E0308"]["count"], 1)          # second code, still counted
        self.assertEqual(top["E9999"]["bucket"], "other_compile")
        self.assertNotIn("E0432", top)                      # attempt 1 only
        self.assertNotIn("E0502", top)                      # excluded task only

    def test_bucket_ids_match_what_the_site_colours(self):
        site_ids = ["borrow", "types", "imports", "other_compile", "test_fail", "harness"]
        self.assertEqual([b["id"] for b in build()["taxonomy"]["buckets"]], site_ids)


class TestOutputShape(unittest.TestCase):

    def test_timestamps_come_from_run_metadata(self):
        built = build()
        self.assertEqual(built["leaderboard"]["generated"], "2026-01-02T01:00:00+00:00")
        self.assertEqual(built["leaderboard"]["run_id"], "sweep-2026-01-02")
        self.assertEqual(built["trend"]["points"][0]["date"], "2026-01-02")

    def test_output_is_deterministic(self):
        first = json.dumps({k: v for k, v in build().items() if not k.startswith("_")},
                           sort_keys=False)
        second = json.dumps({k: v for k, v in build().items() if not k.startswith("_")},
                            sort_keys=False)
        self.assertEqual(first, second)

    def test_models_are_ordered_by_one_shot_passes(self):
        models = [m["model"] for m in build()["leaderboard"]["models"]]
        self.assertEqual(models, ["fable", "gpt-5.6-terra"])

    def test_models_filter(self):
        built = build(models={"fable"})
        self.assertEqual([m["model"] for m in built["leaderboard"]["models"]], ["fable"])

    def test_real_files_are_not_flagged_as_sample(self):
        built = build()
        for key in ("leaderboard", "taxonomy", "trend", "gallery"):
            self.assertNotIn("sample", built[key], key)

    def test_manifest_points_at_the_real_directory(self):
        manifest = build()["manifest"]
        self.assertEqual(manifest["dataset"], "real")
        self.assertEqual(manifest["files"]["leaderboard"], "real/leaderboard.json")

    def test_pricing_basis_is_carried_into_the_output(self):
        self.assertIn("list price", build()["leaderboard"]["pricing_basis"].lower())

    def test_gallery_entries_carry_the_fields_the_site_reads(self):
        gallery = build()["gallery"]
        self.assertEqual(gallery["code_capture"], "unavailable")
        self.assertTrue(gallery["entries"])
        required = ("id", "model", "model_label", "task_id", "project", "attempt", "verdict",
                    "bucket", "error_class_hint", "title", "takeaway", "model_code",
                    "compiler_error", "failing_tests")
        for entry in gallery["entries"]:
            for field in required:
                self.assertIn(field, entry)
            self.assertFalse(entry["model_code_available"])
            self.assertIn(entry["bucket"], [b["id"] for b in buckets.BUCKETS])
            self.assertNotEqual(entry["verdict"], "PASS")

    def test_write_output_lands_in_the_expected_paths(self):
        built = build()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            written = aggregate.write_output(out, built)
            self.assertTrue((out / "real" / "leaderboard.json").exists())
            self.assertTrue((out / "manifest.json").exists())
            self.assertEqual(len(written), 5)
            reloaded = json.loads((out / "real" / "leaderboard.json").read_text())
            self.assertEqual(reloaded["run_id"], "sweep-2026-01-02")


if __name__ == "__main__":
    unittest.main(verbosity=2)
