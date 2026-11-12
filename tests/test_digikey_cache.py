"""DigiKey cache behaviour: failed lookups are retried, old answers expire, a rate limit stops the fetch.

Offline: the client is a stub; no HTTP, no credentials.
"""

import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from kicad_checker import digikey


def part(ref, mpn):
    return {"reference": ref, "value": "x", "footprint": "", "dnp": False, "sheet_path": "/",
            "fields": {"MPN": mpn}, "pins": {}}


def circuit(*mpns):
    return {"components": {f"U{i}": part(f"U{i}", m) for i, m in enumerate(mpns, 1)}}


OK_BODY = {"ProductsCount": 0, "ExactMatches": []}


class Stub:
    """keyword() answers from a list of (status, body); the last answer repeats."""

    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []

    def keyword(self, mpn):
        self.asked.append(mpn)
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def age(cache, mpn, days):
    path = digikey.cache_path(cache, mpn)
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["fetched_at"] = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    path.write_text(json.dumps(entry), encoding="utf-8")


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name)
        self.quiet = lambda *_: None

    def tearDown(self):
        self.tmp.cleanup()

    def test_failed_lookup_is_asked_again(self):
        client = Stub((500, {"title": "Server error"}), (200, OK_BODY))
        first = digikey.fetch(circuit("ABC123"), self.cache, client, log=self.quiet)
        self.assertEqual((first["fetched"], first["errors"]), (0, 1))
        # the failure is kept so the report can explain it ...
        self.assertEqual(digikey.load(self.cache, "ABC123")["http_status"], 500)
        # ... but it is not an answer: the next fetch asks again
        second = digikey.fetch(circuit("ABC123"), self.cache, client, log=self.quiet)
        self.assertEqual((second["fetched"], second["cached"], second["errors"]), (1, 0, 0))
        self.assertEqual(client.asked, ["ABC123", "ABC123"])
        third = digikey.fetch(circuit("ABC123"), self.cache, client, log=self.quiet)
        self.assertEqual(third["cached"], 1)
        self.assertEqual(len(client.asked), 2)

    def test_old_answer_is_renewed_and_fresh_one_kept(self):
        digikey.store(self.cache, "OLD1", 200, OK_BODY)
        digikey.store(self.cache, "NEW1", 200, OK_BODY)
        age(self.cache, "OLD1", 45)
        client = Stub((200, OK_BODY))
        summary = digikey.fetch(circuit("OLD1", "NEW1"), self.cache, client, log=self.quiet)
        self.assertEqual(client.asked, ["OLD1"])
        self.assertEqual((summary["fetched"], summary["cached"]), (1, 1))
        # 0 = keep any age
        age(self.cache, "OLD1", 400)
        client = Stub((200, OK_BODY))
        digikey.fetch(circuit("OLD1", "NEW1"), self.cache, client, log=self.quiet, max_age_days=0)
        self.assertEqual(client.asked, [])

    def test_failed_renewal_keeps_the_old_good_answer(self):
        digikey.store(self.cache, "OLD1", 200, {"ProductsCount": 1, "ExactMatches": [{"ManufacturerProductNumber": "OLD1"}]})
        age(self.cache, "OLD1", 45)
        summary = digikey.fetch(circuit("OLD1"), self.cache, Stub((503, {"title": "down"})), log=self.quiet)
        self.assertEqual(summary["errors"], 1)
        kept = digikey.load(self.cache, "OLD1")
        self.assertEqual(kept["http_status"], 200)
        self.assertEqual(len(kept["exact_matches"]), 1)

    def test_rate_limit_stops_the_fetch(self):
        digikey.store(self.cache, "A3", 200, OK_BODY)  # already answered: not counted as "not asked"
        client = Stub((200, OK_BODY), (429, {"title": "Too many requests"}))
        lines = []
        summary = digikey.fetch(circuit("A1", "A2", "A3", "A4", "A5"), self.cache, client, log=lines.append)
        self.assertEqual(client.asked, ["A1", "A2"])              # nothing asked after the 429
        self.assertEqual(summary, {"fetched": 1, "cached": 0, "errors": 1, "not_attempted": 2})
        self.assertIn("rate limit", lines[-1])
        self.assertIsNone(digikey.load(self.cache, "A4"))          # no error entries written for the rest

    def test_audit_reports_failed_lookup_and_data_age(self):
        digikey.store(self.cache, "A1", 429, {"title": "Too many requests"})
        digikey.store(self.cache, "A2", 200, OK_BODY)
        age(self.cache, "A2", 60)
        report = digikey.digikey_audit(circuit("A1", "A2"), self.cache)
        by_mpn = {r["mpn"]: r for r in report["results"]}
        self.assertIn("HTTP 429", by_mpn["A1"]["reason"])
        self.assertIn("retried", by_mpn["A1"]["reason"])
        self.assertGreater(report["data_age"]["oldest_days"], 59)
        self.assertIn("days old", digikey.digikey_markdown(report))


class ClientRateLimitTests(unittest.TestCase):
    def client(self, retry_after):
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            if request.full_url.endswith("/token"):
                return _Response(200, {"access_token": "tok"})
            headers = {} if retry_after is None else {"Retry-After": str(retry_after)}
            raise digikey.urllib.error.HTTPError(request.full_url, 429, "limit", headers, io.BytesIO(b"{}"))

        return digikey.DigiKeyClient("id", "secret", min_interval=0, opener=opener), calls

    def test_short_retry_after_waits_once_then_gives_up(self):
        client, calls = self.client(2)
        with mock.patch.object(digikey.time, "sleep") as sleep:
            status, _ = client.keyword("X")
        self.assertEqual(status, 429)
        sleep.assert_called_once_with(2.0)
        self.assertEqual(sum(u.endswith("/keyword") for u in calls), 2)

    def test_daily_quota_does_not_sleep(self):
        client, calls = self.client(36000)
        with mock.patch.object(digikey.time, "sleep") as sleep:
            status, _ = client.keyword("X")
        self.assertEqual(status, 429)
        sleep.assert_not_called()
        self.assertEqual(sum(u.endswith("/keyword") for u in calls), 1)

    def test_authentication_failure_does_not_print_the_raw_body(self):
        def opener(request, timeout):
            raise digikey.urllib.error.HTTPError(request.full_url, 401, "no", {},
                                                 io.BytesIO(json.dumps({"ErrorMessage": "x" * 900}).encode()))
        client = digikey.DigiKeyClient("id", "secret", min_interval=0, opener=opener)
        with self.assertRaises(ValueError) as caught:
            client.authenticate()
        self.assertLess(len(str(caught.exception)), 120)


class _Response(io.BytesIO):
    def __init__(self, status, data):
        super().__init__(json.dumps(data).encode())
        self.status = status


if __name__ == "__main__":
    unittest.main()
