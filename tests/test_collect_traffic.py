"""偽の API で collect_traffic.py を試す（本物の提供元にはつながない）。

    python -m unittest discover -s tests
"""

from __future__ import annotations

import csv
import gzip
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import collect_traffic as ct  # noqa: E402

COUNTERS = [3110010 + i for i in range(5)]


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class FakeSession:
    """時間コードの範囲を読んで、観測点 COUNTERS 分の偽データを返す。

    missing:  返さない時間コード（HHMM）
    thin:     観測点を1つしか返さない時間コード（HHMM）
    cap:      1回に返す件数の上限（打ち切りの再現）
    empty:    何も返さない
    """

    def __init__(self, missing=(), thin=(), cap=None, empty=False):
        self.missing, self.thin, self.cap, self.empty = set(missing), set(thin), cap, empty
        self.calls = []
        self.headers = {}

    def get(self, url, params, timeout):
        self.calls.append(params)
        cql = params["cql_filter"]
        lo =[p for p in cql.split(" AND ") if p.startswith("時間コード>=")][0].split(">=")[1]
        hi = [p for p in cql.split(" AND ") if p.startswith("時間コード<=")][0].split("<=")[1]
        road = int([p for p in cql.split(" AND ") if p.startswith("道路種別=")][0].split("=")[1])
        feats = []
        if not self.empty:
            for code in ct._codes_between(lo, hi):
                if code[-4:] in self.missing:
                    continue
                counters = COUNTERS[:1] if code[-4:] in self.thin else COUNTERS
                for cid in counters:
                    feats.append(
                        {
                            "type": "Feature",
                            "geometry": {"type": "Point", "coordinates": [135.5, 34.6]},
                            "properties": {
                                "時間コード": int(code),
                                "常時観測点コード": cid,
                                "道路種別": road,
                                "地方整備局等番号": 86,
                                "上り・小型交通量": 10,
                            },
                        }
                    )
        matched = len(feats)
        if self.cap is not None and len(feats) > self.cap:
            feats = feats[: self.cap]
        return FakeResponse(
            {"type": "FeatureCollection", "features": feats, "numberMatched": matched, "numberReturned": len(feats)}
        )


def fetcher(session):
    return ct.Fetcher(session=session, pause_sec=0)


class FetchDayTest(unittest.TestCase):
    day = date(2026, 11, 1)

    def test_complete_day(self):
        s = FakeSession()
        r = ct.fetch_day(fetcher(s), self.day, "loop", 3)
        self.assertEqual(len(r.rows), 288 * len(COUNTERS))
        self.assertEqual(r.missing_codes, [])
        self.assertEqual(len(s.calls), 24)  # 1時間ずつ
        self.assertEqual(r.columns[-2:], ["経度", "緯度"])
        self.assertEqual(ct.decide_status(r, age_days=1), "complete")

    def test_missing_and_thin_codes_count_as_missing(self):
        s = FakeSession(missing={"0005", "1230"}, thin={"0800"})
        r = ct.fetch_day(fetcher(s), self.day, "loop", 3)
        self.assertEqual([c[-4:] for c in r.missing_codes], ["0005", "0800", "1230"])

    def test_truncated_hour_is_split_into_5min(self):
        s = FakeSession(cap=len(COUNTERS) * 6)  # 1時間分（12時刻）は返しきれない
        r = ct.fetch_day(fetcher(s), self.day, "loop", 3)
        self.assertEqual(len(r.rows), 288 * len(COUNTERS))
        self.assertEqual(len(s.calls), 24 + 288)

    def test_truncated_single_code_raises(self):
        s = FakeSession(cap=2)
        with self.assertRaises(RuntimeError):
            ct.fetch_day(fetcher(s), self.day, "loop", 3)


class PointOfTest(unittest.TestCase):
    def test_shapes(self):
        self.assertEqual(ct.point_of({"type": "Point", "coordinates": [135.5, 34.6]}), (135.5, 34.6))
        self.assertEqual(ct.point_of({"type": "MultiPoint", "coordinates": [[135.5, 34.6]]}), (135.5, 34.6))
        self.assertEqual(ct.point_of({"type": "Point", "coordinates": []}), (None, None))
        self.assertEqual(ct.point_of(None), (None, None))


class DecideStatusTest(unittest.TestCase):
    def result(self, n_missing, rows=True):
        r = ct.DayResult(day=date(2026, 11, 1), layer="loop", road_type=3)
        r.rows = [{}] if rows else []
        r.missing_codes = ["x"] * n_missing
        return r

    def test_rules(self):
        self.assertEqual(ct.decide_status(self.result(0), 1), "complete")
        self.assertIsNone(ct.decide_status(self.result(3), 1))  # 昨日は待つ
        self.assertEqual(ct.decide_status(self.result(3), 2), "complete_with_gaps")  # 5%以内
        self.assertIsNone(ct.decide_status(self.result(100), 10))  # 多すぎる欠けは待つ
        self.assertEqual(ct.decide_status(self.result(100), 28), "partial_before_expiry")
        self.assertIsNone(ct.decide_status(self.result(0, rows=False), 5))
        self.assertEqual(ct.decide_status(self.result(0, rows=False), 29), "empty_before_expiry")


class DaysToFetchTest(unittest.TestCase):
    today = date(2026, 11, 3)

    def test_order_near_expiry_first_then_newest(self):
        days = ct.days_to_fetch(self.today, set(), max_days=5)
        self.assertEqual(days[:3], [self.today - timedelta(days=a) for a in (30, 29, 28)])
        self.assertEqual(days[3:], [self.today - timedelta(days=1), self.today - timedelta(days=2)])

    def test_skips_done_days(self):
        d = (self.today - timedelta(days=1)).isoformat()
        done = {(d, layer, str(road)) for layer in ct.LAYERS for road in ct.ROAD_TYPES}
        self.assertNotIn(self.today - timedelta(days=1), ct.days_to_fetch(self.today, done, 50))

    def test_half_done_day_is_fetched_again(self):
        d = (self.today - timedelta(days=1)).isoformat()
        done = {(d, "loop", "3")}
        self.assertIn(self.today - timedelta(days=1), ct.days_to_fetch(self.today, done, 50))


class EndToEndTest(unittest.TestCase):
    """fetch → record を一時フォルダで通す。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = {
            "OUT_DIR": root / "out",
            "MANIFEST": root / "data" / "manifest_traffic.csv",
            "PENDING": root / "out" / "pending_manifest.csv",
            "REPO_ROOT": root,
        }
        self.saved = {k: getattr(ct, k) for k in self.patches}
        for k, v in self.patches.items():
            setattr(ct, k, v)

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(ct, k, v)
        self.tmp.cleanup()

    def run_fetch(self, session, now="2026-11-03T08:00:00+09:00", max_days=2):
        args = ct.argparse.Namespace(max_days=max_days, pause=0, time_budget_min=60, now=now)
        ct.cmd_fetch(args, session=session)

    def mark_done_except(self, keep_ages, today=date(2026, 11, 3)):
        """keep_ages 以外の日を「保存済み」として台帳に書く。"""
        rows = []
        for age in range(1, ct.RETENTION_DAYS + 1):
            if age in keep_ages:
                continue
            d = (today - timedelta(days=age)).isoformat()
            for layer in ct.LAYERS:
                for road in ct.ROAD_TYPES:
                    rows.append({k: "" for k in ct.MANIFEST_FIELDS} | {
                        "date": d, "layer": layer, "road_type": str(road), "status": "complete"})
        ct.MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        with open(ct.MANIFEST, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=ct.MANIFEST_FIELDS, lineterminator="\n")
            w.writeheader()
            w.writerows(rows)

    def test_first_run_starts_from_days_about_to_expire(self):
        self.run_fetch(FakeSession())
        dates = sorted({r["date"] for r in ct.read_manifest(ct.PENDING)})
        self.assertEqual(dates, ["2026-10-04", "2026-10-05"])

    def test_fetch_record_and_skip_next_time(self):
        self.mark_done_except({1, 2, 3})
        self.run_fetch(FakeSession())
        out = sorted(p.name for p in ct.OUT_DIR.glob("*.csv.gz"))
        # 2日 × 2レイヤー × 2道路種別
        self.assertEqual(len(out), 8)
        self.assertIn("kinki_loop_5m_road3_20261102.csv.gz", out)
        with gzip.open(ct.OUT_DIR / "kinki_loop_5m_road3_20261102.csv.gz", "rt", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 288 * len(COUNTERS))
        self.assertEqual(rows[0]["時間コード"], "202611020000")

        ct.cmd_record(None)
        manifest = ct.read_manifest()
        new = [r for r in manifest if r["date"] in ("2026-11-01", "2026-11-02")]
        self.assertEqual(len(new), 8)
        self.assertTrue(all(r["rows"] == str(288 * len(COUNTERS)) for r in new))

        # 2回目は保存済みの日を取りに行かず、残りの日（10/31）だけを取る
        for p in ct.OUT_DIR.glob("*"):
            p.unlink()
        self.run_fetch(FakeSession())
        dates = {r["date"] for r in ct.read_manifest(ct.PENDING)}
        self.assertEqual(dates, {"2026-10-31"})

    def test_yesterday_with_gaps_waits_and_is_not_recorded(self):
        self.mark_done_except({1})
        self.run_fetch(FakeSession(missing={"2355"}), max_days=1)
        self.assertEqual(list(ct.OUT_DIR.glob("*.csv.gz")), [])
        key = lambda r: (r["date"], r["layer"], r["road_type"])  # noqa: E731
        before = sorted(ct.read_manifest(), key=key)
        ct.cmd_record(None)
        self.assertEqual(sorted(ct.read_manifest(), key=key), before)
        self.assertNotIn("2026-11-02", {r["date"] for r in ct.read_manifest()})

    def test_old_day_with_many_gaps_is_saved_before_it_expires(self):
        self.mark_done_except({29})
        self.run_fetch(FakeSession(missing={f"{h:02d}00" for h in range(24)} | {"0005", "0010"}), max_days=1)
        pending = ct.read_manifest(ct.PENDING)
        self.assertEqual({r["status"] for r in pending}, {"partial_before_expiry"})

    def test_same_content_gives_same_file(self):
        self.mark_done_except({1})
        self.run_fetch(FakeSession(), max_days=1)
        first = (ct.OUT_DIR / "kinki_loop_5m_road3_20261102.csv.gz").read_bytes()
        self.run_fetch(FakeSession(), max_days=1)
        self.assertEqual(first, (ct.OUT_DIR / "kinki_loop_5m_road3_20261102.csv.gz").read_bytes())


if __name__ == "__main__":
    unittest.main()
