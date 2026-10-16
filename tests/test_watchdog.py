"""Offline tests. No network, no Apify call, no cost: the HTTP layer is replaced."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import watchdog  # noqa: E402


def write_cfg(data) -> Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(data, handle)
    handle.close()
    return Path(handle.name)


GOOD = {"ads": {"domains": ["Nike.com"], "max_ads_per_domain": 20, "max_charge_usd": 0.5},
        "jobs": {"companies": ["stripe.com"], "max_charge_usd": 0.5}, "max_total_usd_per_run": 1.0}

ADS_ITEMS = [{"domain": "nike.com", "advertiser_name": "Nike, Inc.", "ad_id": "CR1", "format": "text",
              "first_shown": "2026-10-14T08:00:00Z", "transparency_url": "https://adstransparency.google.com/x"}]
JOBS_ITEMS = [
    {"record_type": "company_summary", "company_domain": "stripe.com", "is_first_run": False, "new_jobs": 1,
     "closed_jobs": 1, "total_active_jobs": 700, "closed_job_titles": ["Old role"]},
    {"record_type": "job", "company_domain": "stripe.com", "title": "Abuse Investigator", "location": "Dublin",
     "apply_url": "https://stripe.com/jobs/1", "status": "NEW"},
]


class ConfigTests(unittest.TestCase):
    def test_good_config_loads_and_lowercases(self):
        cfg = watchdog.load_config(write_cfg(GOOD))
        self.assertEqual(cfg["ads_domains"], ["nike.com"])

    def test_empty_config_rejected(self):
        with self.assertRaises(watchdog.ConfigError):
            watchdog.load_config(write_cfg({"ads": {"domains": []}, "jobs": {"companies": []}}))

    def test_bad_domain_rejected(self):
        for bad in ("https://nike.com/x", "nike", "nike.com; rm -rf /", "a b.com"):
            with self.assertRaises(watchdog.ConfigError, msg=bad):
                watchdog.load_config(write_cfg({"ads": {"domains": [bad]}}))

    def test_caps_must_fit_the_total(self):
        data = json.loads(json.dumps(GOOD))
        data["max_total_usd_per_run"] = 0.5
        with self.assertRaises(watchdog.ConfigError):
            watchdog.load_config(write_cfg(data))

    def test_too_many_domains_rejected(self):
        domains = [f"site{i}.com" for i in range(watchdog.MAX_DOMAINS + 1)]
        with self.assertRaises(watchdog.ConfigError):
            watchdog.load_config(write_cfg({"ads": {"domains": domains}}))

    def test_prefixed_board_slug_allowed(self):
        cfg = watchdog.load_config(write_cfg({"jobs": {"companies": ["greenhouse:stripe", "linear.app"]}}))
        self.assertEqual(len(cfg["companies"]), 2)

    def test_worst_case_uses_published_rates(self):
        cfg = watchdog.load_config(write_cfg(GOOD))
        self.assertAlmostEqual(watchdog.worst_case_ads_usd(cfg), 0.005 + 20 * 0.0015)


class InputTests(unittest.TestCase):
    def test_ads_input_asks_only_for_new_ads(self):
        cfg = watchdog.load_config(write_cfg(GOOD))
        run_input = watchdog.ads_input(cfg)
        self.assertTrue(run_input["only_new_ads"])
        self.assertEqual(run_input["mode"], "ads")

    def test_jobs_input_tracks_changes_with_a_state_id(self):
        cfg = watchdog.load_config(write_cfg(GOOD))
        run_input = watchdog.jobs_input(cfg)
        self.assertTrue(run_input["only_changes"] and run_input["track_diff"])
        self.assertEqual(run_input["state_id"], "competitor-watchdog")


class FormatTests(unittest.TestCase):
    def test_ads_message(self):
        text = "\n".join(watchdog.format_ads(ADS_ITEMS))
        self.assertIn("1 new ad", text)
        self.assertIn("Nike, Inc.", text)

    def test_no_new_ads(self):
        self.assertEqual(watchdog.format_ads([]), ["Ads: no new ads."])

    def test_jobs_message_lists_new_and_closed(self):
        text = "\n".join(watchdog.format_jobs(JOBS_ITEMS))
        self.assertIn("1 new, 1 closed", text)
        self.assertIn("Abuse Investigator", text)
        self.assertIn("closed: Old role", text)

    def test_first_run_is_labelled_a_starting_point(self):
        items = [{"record_type": "company_summary", "company_domain": "x.com", "is_first_run": True, "total_active_jobs": 9}]
        self.assertIn("starting point", watchdog.format_jobs(items)[0])

    def test_quiet_day_says_no_changes(self):
        items = [{"record_type": "company_summary", "company_domain": "x.com", "is_first_run": False, "new_jobs": 0, "closed_jobs": 0}]
        self.assertEqual(watchdog.format_jobs(items), ["Jobs: no changes."])


class RunTests(unittest.TestCase):
    def setUp(self):
        self.cfg_path = write_cfg(GOOD)

    def run_main(self, argv, env):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(out), redirect_stderr(err):
            code = watchdog.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_calls_nothing(self):
        with mock.patch.object(watchdog, "api", side_effect=AssertionError("network call")):
            code, out, _ = self.run_main(["--config", str(self.cfg_path), "--dry-run"], {})
        self.assertEqual(code, 0)
        self.assertIn("nothing has been called", out)

    def test_missing_token_stops_before_any_call(self):
        with mock.patch.object(watchdog, "api", side_effect=AssertionError("network call")):
            code, _, err = self.run_main(["--config", str(self.cfg_path)], {})
        self.assertEqual(code, 2)
        self.assertIn("APIFY_TOKEN", err)

    def test_full_run_with_cap_and_no_token_in_output(self):
        calls = []

        def fake_api(method, path, token, body=None, params=None):
            calls.append((method, path, params))
            if method == "POST":
                return {"data": {"id": "run1", "status": "SUCCEEDED", "defaultDatasetId": "ds1"}}
            return ADS_ITEMS if "ads" in calls[-2][1] else JOBS_ITEMS

        with mock.patch.object(watchdog, "api", side_effect=fake_api), mock.patch.object(watchdog, "post_slack") as slack:
            code, out, err = self.run_main(["--config", str(self.cfg_path)],
                                           {"APIFY_TOKEN": "apify_api_SECRET123", "SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/T/B/x"})
        self.assertEqual(code, 0)
        posts = [c for c in calls if c[0] == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertTrue(all(c[2]["maxTotalChargeUsd"] == 0.5 for c in posts))
        slack.assert_called_once()
        self.assertNotIn("SECRET123", out + err)

    def test_baseline_run_does_not_post(self):
        def fake_api(method, path, token, body=None, params=None):
            if method == "POST":
                return {"data": {"id": "r", "status": "SUCCEEDED", "defaultDatasetId": "d"}}
            return []

        with mock.patch.object(watchdog, "api", side_effect=fake_api), mock.patch.object(watchdog, "post_slack") as slack:
            code, out, _ = self.run_main(["--config", str(self.cfg_path), "--baseline"],
                                         {"APIFY_TOKEN": "t", "SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/T/B/x"})
        self.assertEqual(code, 0)
        slack.assert_not_called()
        self.assertIn("Baseline run", out)

    def test_failed_run_is_reported_and_never_retried(self):
        calls = []

        def fake_api(method, path, token, body=None, params=None):
            calls.append(method)
            return {"data": {"id": "r", "status": "FAILED", "defaultDatasetId": "d"}}

        with mock.patch.object(watchdog, "api", side_effect=fake_api):
            code, out, _ = self.run_main(["--config", str(self.cfg_path)], {"APIFY_TOKEN": "t"})
        self.assertEqual(code, 1)
        self.assertIn("FAILED", out)
        self.assertEqual(calls.count("POST"), 2)  # one start per Actor, no retry

    def test_slack_url_must_be_slack(self):
        with self.assertRaises(RuntimeError):
            watchdog.post_slack("https://evil.example/hook", "x")


if __name__ == "__main__":
    unittest.main()
