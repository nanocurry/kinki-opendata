"""偽のフィードで collect_warning_xml.py を試す（気象庁にはつながない）。"""

from __future__ import annotations

import csv
import gzip
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import collect_warning_xml as cx  # noqa: E402

DATA = "https://www.data.jma.go.jp/developer/xml/data"
EXTRA_URL = cx.FEEDS["extra"][0]
OTHER_URL = cx.FEEDS["other"][0]


def entry(name, updated, author="大阪管区気象台"):
    return f"""<entry><title>題名</title><id>{DATA}/{name}</id><updated>{updated}</updated>
<author><name>{author}</name></author><link type="application/xml" href="{DATA}/{name}"/>
<content type="text">本文</content></entry>"""


def feed(*entries):
    return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" lang="ja"><title>長期</title><updated>2026-11-04T01:00:00Z</updated>
{''.join(entries)}</feed>""".encode("utf-8")


OSAKA = "20261104010000_0_VPWW55_270000.xml"
WAKAYAMA = "20261103192830_0_VPWW61_300000.xml"
TOKYO = "20261104010500_0_VPWW55_130000.xml"
OSAKA_OTHER = "20261104011000_0_VPFD51_270000.xml"  # 警報ではない種類
SEA_LOCAL = "20261104023503_0_VPCU51_040000.xml"  # 地方海上警報
SEA_GENERAL = "20261104000000_0_VPZU54_010000.xml"  # 全般海上警報
SEA_FORECAST = "20261104000000_0_VPCY51_270000.xml"  # 地方海上予報（今は取らない）


def extra_feed():
    return feed(
        entry(OSAKA, "2026-11-04T01:00:00Z"),
        entry(WAKAYAMA, "2026-11-03T19:28:30Z", "和歌山地方気象台"),
        entry(TOKYO, "2026-11-04T01:05:00Z", "気象庁本庁"),
        entry(OSAKA_OTHER, "2026-11-04T01:10:00Z"),
    )


def other_feed():
    return feed(
        entry(SEA_LOCAL, "2026-11-04T02:35:03Z", "仙台管区気象台"),
        entry(SEA_GENERAL, "2026-11-04T00:00:00Z", "気象庁本庁"),
        entry(SEA_FORECAST, "2026-11-04T00:00:00Z"),
    )


class FakeResponse:
    def __init__(self, content, code=200):
        self.content, self.code = content, code

    def raise_for_status(self):
        if self.code >= 400:
            raise RuntimeError(f"HTTP {self.code}")


class FakeSession:
    def __init__(self, extra=None, other=None, broken=(), feed_down=()):
        self.feeds = {EXTRA_URL: extra if extra is not None else extra_feed(),
                      OTHER_URL: other if other is not None else other_feed()}
        self.broken, self.feed_down = set(broken), set(feed_down)
        self.calls = []
        self.headers = {}

    def get(self, url, timeout):
        self.calls.append(url)
        if url in self.feeds:
            if url in self.feed_down:
                return FakeResponse(b"", code=503)
            return FakeResponse(self.feeds[url])
        name = url.rsplit("/", 1)[1]
        if name in self.broken:
            return FakeResponse(b"<html>error", code=200)
        return FakeResponse(f"<Report><Control><Title>{name}</Title></Control></Report>".encode())


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=cx.JST)


def by_feed(rows):
    return {r["feed"]: r for r in rows}


class CollectXmlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved_dir = cx.DATA_DIR
        cx.DATA_DIR = Path(self.tmp.name) / "warning_xml"
        self.saved_get = cx.get
        cx.get = lambda s, url: self.saved_get(s, url, retries=1, pause_sec=0)

    def tearDown(self):
        cx.DATA_DIR = self.saved_dir
        cx.get = self.saved_get
        self.tmp.cleanup()

    def run_once(self, session, now="2026-11-04T11:00:00"):
        return by_feed(cx.collect(session, now=at(now), pause_sec=0))

    def exists(self, area, name, month="2026-11"):
        return (cx.DATA_DIR / area / month / f"{name}.gz").exists()

    def read_log(self, month="2026-11"):
        with open(cx.DATA_DIR / f"fetch_log_{month}.csv", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def test_saves_land_warnings_nationwide(self):
        r = self.run_once(FakeSession())["extra"]
        self.assertEqual((r["feed_entries"], r["warning_entries"], r["kinki_warning_entries"], r["new_saved"]), (4, 3, 2, 3))
        with gzip.open(cx.DATA_DIR / "270000" / "2026-11" / f"{OSAKA}.gz", "rb") as f:
            self.assertIn(OSAKA.encode(), f.read())
        self.assertTrue(self.exists("300000", WAKAYAMA))
        self.assertTrue(self.exists("130000", TOKYO))
        self.assertFalse(any(cx.DATA_DIR.glob(f"*/*/{OSAKA_OTHER}.gz")))  # 警報でない種類は取らない
        self.assertEqual(r["feed_oldest_jst"], "2026-11-04T04:28")

    def test_saves_marine_warnings_but_not_forecasts(self):
        r = self.run_once(FakeSession())["other"]
        self.assertEqual((r["feed_entries"], r["warning_entries"], r["new_saved"]), (3, 2, 2))
        self.assertTrue(self.exists("040000", SEA_LOCAL))
        self.assertTrue(self.exists("010000", SEA_GENERAL))
        self.assertFalse(any(cx.DATA_DIR.glob(f"*/*/{SEA_FORECAST}.gz")))

    def test_log_has_one_row_per_feed(self):
        self.run_once(FakeSession())
        self.assertEqual([r["feed"] for r in self.read_log()], ["extra", "other"])

    def test_target_areas_filter(self):
        saved = cx.TARGET_AREAS
        cx.TARGET_AREAS = set(cx.AREAS)
        try:
            rows = self.run_once(FakeSession())
            self.assertEqual((rows["extra"]["new_saved"], rows["other"]["new_saved"]), (2, 0))
            self.assertFalse((cx.DATA_DIR / "130000").exists())
        finally:
            cx.TARGET_AREAS = saved

    def test_download_cap_is_shared_across_feeds(self):
        saved = cx.MAX_DOWNLOADS
        cx.MAX_DOWNLOADS = 4
        try:
            rows = self.run_once(FakeSession())
            self.assertEqual((rows["extra"]["new_saved"], rows["extra"]["deferred"]), (3, 0))
            self.assertEqual((rows["other"]["new_saved"], rows["other"]["deferred"]), (1, 1))
            rows = self.run_once(FakeSession(), now="2026-11-04T12:00:00")
            self.assertEqual((rows["other"]["new_saved"], rows["other"]["deferred"]), (1, 0))
        finally:
            cx.MAX_DOWNLOADS = saved

    def test_download_cap_takes_oldest_first(self):
        saved = cx.MAX_DOWNLOADS
        cx.MAX_DOWNLOADS = 2
        try:
            r = self.run_once(FakeSession())["extra"]
            self.assertEqual((r["new_saved"], r["deferred"]), (2, 1))
            self.assertTrue(self.exists("300000", WAKAYAMA))  # 最も古い
        finally:
            cx.MAX_DOWNLOADS = saved

    def test_one_feed_down_does_not_stop_the_other(self):
        rows = self.run_once(FakeSession(feed_down={OTHER_URL}))
        self.assertEqual(rows["extra"]["new_saved"], 3)
        self.assertEqual((rows["other"]["feed_entries"], rows["other"]["failed"]), (0, 1))

    def test_second_run_downloads_nothing_again(self):
        self.run_once(FakeSession())
        s = FakeSession()
        rows = self.run_once(s, now="2026-11-04T12:00:00")
        self.assertEqual((rows["extra"]["new_saved"], rows["other"]["new_saved"]), (0, 0))
        self.assertEqual(s.calls, [EXTRA_URL, OTHER_URL])  # 電文は取り直さない

    def test_broken_body_is_not_saved_and_retried_next_time(self):
        r = self.run_once(FakeSession(broken={OSAKA}))["extra"]
        self.assertEqual((r["new_saved"], r["failed"]), (2, 1))
        r = self.run_once(FakeSession(), now="2026-11-04T12:00:00")["extra"]
        self.assertEqual(r["new_saved"], 1)

    def test_gap_is_judged_per_feed(self):
        self.run_once(FakeSession(), now="2026-11-01T00:00:00")
        rows = self.run_once(FakeSession(), now="2026-11-04T12:00:00")
        self.assertEqual((rows["extra"]["gap_suspected"], rows["other"]["gap_suspected"]), (1, 1))
        rows = self.run_once(FakeSession(), now="2026-11-04T13:00:00")
        self.assertEqual((rows["extra"]["gap_suspected"], rows["other"]["gap_suspected"]), (0, 0))

    def test_new_marine_feed_has_no_gap_on_its_first_run(self):
        # extra だけの古い記録しかないとき、other は「前回なし」として扱う
        path = cx.DATA_DIR / "fetch_log_2026-11.csv"
        path.parent.mkdir(parents=True)
        old = cx.LOG_FIELDS[:10]
        path.write_text(
            ",".join(old) + "\n" + ",".join(["2026-11-01T00:00:00+0900", "5009", "", "", "374", "1500", "0", "0", "4511", "2637"]) + "\n",
            encoding="utf-8",
        )
        rows = self.run_once(FakeSession(), now="2026-11-04T12:00:00")
        self.assertEqual(rows["extra"]["gap_suspected"], 1)
        self.assertEqual(rows["other"]["gap_suspected"], 0)

    def test_old_log_without_feed_column_is_rewritten(self):
        path = cx.DATA_DIR / "fetch_log_2026-11.csv"
        path.parent.mkdir(parents=True)
        old = cx.LOG_FIELDS[:8]
        path.write_text(",".join(old) + "\n" + ",".join(["2026-11-04T11:00:00+0900", "5027", "", "", "374", "374", "0", "0"]) + "\n", encoding="utf-8")
        self.run_once(FakeSession(), now="2026-11-04T12:00:00")
        rows = self.read_log()
        self.assertEqual(len(rows), 3)
        self.assertEqual((rows[0]["new_saved"], rows[0]["feed"]), ("374", ""))
        self.assertEqual([r["feed"] for r in rows[1:]], ["extra", "other"])

    def test_log_is_written_per_month(self):
        self.run_once(FakeSession(), now="2026-11-30T23:30:00")
        self.run_once(FakeSession(), now="2026-12-01T00:30:00")
        self.assertEqual(len(self.read_log("2026-11")), 2)
        self.assertEqual(len(self.read_log("2026-12")), 2)

    def test_empty_feeds(self):
        rows = self.run_once(FakeSession(extra=feed(), other=feed()))
        self.assertEqual((rows["extra"]["feed_entries"], rows["other"]["new_saved"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
