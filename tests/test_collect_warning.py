"""偽の応答で collect_warning.py を試す（気象庁にはつながない）。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import collect_warning as cw  # noqa: E402

JST = cw.JST


def body(report="2026-10-30T22:45:00+09:00", status="解除"):
    return [
        {
            "reportDatetime": report,
            "publishingOffice": "大阪管区気象台",
            "warning": {"class10Items": [{"areaCode": "270000", "kinds": [{"code": "14", "status": status}]}]},
            "dataTypeCode": "VPWW61",
        }
    ]


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payloads):
        self.payloads = payloads  # code -> payload（例外を入れると失敗）
        self.headers = {}

    def get(self, url, timeout):
        code = url.rsplit("/", 1)[1].removesuffix(".json")
        payload = self.payloads.get(code, body())
        if isinstance(payload, Exception):
            return FakeResponse(None, status=503)
        return FakeResponse(payload)


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved_dir = cw.DATA_DIR
        cw.DATA_DIR = Path(self.tmp.name) / "warning"
        self.saved_fetch = cw.fetch
        cw.fetch = lambda s, c: self.saved_fetch(s, c, retries=2, pause_sec=0)

    def tearDown(self):
        cw.DATA_DIR = self.saved_dir
        cw.fetch = self.saved_fetch
        self.tmp.cleanup()

    def run_once(self, payloads, now="2026-11-03T17:00:00+09:00"):
        return cw.collect(FakeSession(payloads), now=datetime.fromisoformat(now), pause_sec=0)

    def lines(self, code, month="2026-11"):
        path = cw.DATA_DIR / code / f"{month}.jsonl"
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def test_first_run_saves_all_areas(self):
        results = self.run_once({})
        self.assertEqual(set(results.values()), {"saved"})
        rec = self.lines("270000")[0]
        self.assertEqual(rec["latest_report_datetime"], "2026-10-30T22:45:00+09:00")
        self.assertEqual(rec["body"], body())

    def test_same_content_is_not_saved_again(self):
        self.run_once({})
        results = self.run_once({}, now="2026-11-03T17:10:00+09:00")
        self.assertEqual(set(results.values()), {"same"})
        self.assertEqual(len(self.lines("270000")), 1)

    def test_key_order_does_not_count_as_change(self):
        self.run_once({})
        reordered = [dict(reversed(list(body()[0].items())))]
        results = self.run_once({"270000": reordered}, now="2026-11-03T17:10:00+09:00")
        self.assertEqual(results["270000"], "same")

    def test_new_report_is_appended(self):
        self.run_once({})
        new = body(report="2026-11-04T05:00:00+09:00", status="発表")
        self.run_once({"270000": new}, now="2026-11-04T05:10:00+09:00")
        recs = self.lines("270000")
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[1]["latest_report_datetime"], "2026-11-04T05:00:00+09:00")

    def test_change_detected_across_months(self):
        self.run_once({}, now="2026-10-31T23:55:00+09:00")
        results = self.run_once({}, now="2026-11-01T00:05:00+09:00")
        self.assertEqual(results["270000"], "same")

    def test_bad_body_and_failure_are_not_saved_and_others_continue(self):
        results = self.run_once({"270000": {"error": "x"}, "260000": RuntimeError()})
        self.assertEqual(results["270000"], "failed")
        self.assertEqual(results["260000"], "failed")
        self.assertEqual(results["280000"], "saved")
        self.assertEqual(self.lines("270000"), [])


if __name__ == "__main__":
    unittest.main()
