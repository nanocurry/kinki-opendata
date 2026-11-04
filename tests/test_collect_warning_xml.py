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


def entry(name, updated, author="大阪管区気象台"):
    return f"""<entry><title>気象警報・注意報</title><id>urn:uuid:{name}</id><updated>{updated}</updated>
<author><name>{author}</name></author><link type="application/xml" href="{DATA}/{name}"/>
<content type="text">本文</content></entry>"""


def feed(*entries):
    return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" lang="ja"><title>随時</title><updated>2026-11-04T01:00:00Z</updated>
{''.join(entries)}</feed>""".encode("utf-8")


OSAKA = "20261104010000_0_VPWW55_270000.xml"
WAKAYAMA = "20261103192830_0_VPWW61_300000.xml"
TOKYO = "20261104010500_0_VPWW55_130000.xml"
OSAKA_OTHER = "20261104011000_0_VPFD51_270000.xml"  # 警報ではない種類


class FakeResponse:
    def __init__(self, content, code=200):
        self.content, self.code = content, code

    def raise_for_status(self):
        if self.code >= 400:
            raise RuntimeError(f"HTTP {self.code}")


class FakeSession:
    def __init__(self, feed_bytes, broken=()):
        self.feed_bytes, self.broken = feed_bytes, set(broken)
        self.calls = []
        self.headers = {}

    def get(self, url, timeout):
        self.calls.append(url)
        if url == cx.FEED_URL:
            return FakeResponse(self.feed_bytes)
        name = url.rsplit("/", 1)[1]
        if name in self.broken:
            return FakeResponse(b"<html>error", code=200)
        return FakeResponse(f"<Report><Control><Title>{name}</Title></Control></Report>".encode())


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=cx.JST)


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
        return cx.collect(session, now=at(now), pause_sec=0)

    def standard_feed(self):
        return feed(
            entry(OSAKA, "2026-11-04T01:00:00Z"),
            entry(WAKAYAMA, "2026-11-03T19:28:30Z", "和歌山地方気象台"),
            entry(TOKYO, "2026-11-04T01:05:00Z", "気象庁本庁"),
            entry(OSAKA_OTHER, "2026-11-04T01:10:00Z"),
        )

    def test_saves_warnings_nationwide(self):
        row = self.run_once(FakeSession(self.standard_feed()))
        self.assertEqual(
            (row["feed_entries"], row["warning_entries"], row["kinki_warning_entries"], row["new_saved"]), (4, 3, 2, 3)
        )
        osaka = cx.DATA_DIR / "270000" / "2026-11" / f"{OSAKA}.gz"
        with gzip.open(osaka, "rb") as f:
            self.assertIn(OSAKA.encode(), f.read())
        self.assertTrue((cx.DATA_DIR / "300000" / "2026-11" / f"{WAKAYAMA}.gz").exists())
        self.assertTrue((cx.DATA_DIR / "130000" / "2026-11" / f"{TOKYO}.gz").exists())
        self.assertFalse(any(cx.DATA_DIR.glob(f"*/*/{OSAKA_OTHER}.gz")))  # 警報でない種類は取らない

    def test_target_areas_filter(self):
        saved = cx.TARGET_AREAS
        cx.TARGET_AREAS = set(cx.AREAS)
        try:
            row = self.run_once(FakeSession(self.standard_feed()))
            self.assertEqual(row["new_saved"], 2)
            self.assertFalse((cx.DATA_DIR / "130000").exists())
        finally:
            cx.TARGET_AREAS = saved

    def test_download_cap_defers_the_rest_to_next_run(self):
        saved = cx.MAX_DOWNLOADS
        cx.MAX_DOWNLOADS = 2
        try:
            row = self.run_once(FakeSession(self.standard_feed()))
            self.assertEqual((row["new_saved"], row["deferred"]), (2, 1))
            # 古い順に取るので、最も古い和歌山は1回目に取れている
            self.assertTrue((cx.DATA_DIR / "300000" / "2026-11" / f"{WAKAYAMA}.gz").exists())
            row = self.run_once(FakeSession(self.standard_feed()), now="2026-11-04T12:00:00")
            self.assertEqual((row["new_saved"], row["deferred"]), (1, 0))
        finally:
            cx.MAX_DOWNLOADS = saved

    def test_old_log_with_fewer_columns_is_rewritten(self):
        path = cx.DATA_DIR / "fetch_log_2026-11.csv"
        path.parent.mkdir(parents=True)
        old = cx.LOG_FIELDS[:8]
        path.write_text(",".join(old) + "\n" + ",".join(["2026-11-04T11:00:00+0900", "5027", "", "", "374", "374", "0", "0"]) + "\n", encoding="utf-8")
        self.run_once(FakeSession(self.standard_feed()), now="2026-11-04T12:00:00")
        with open(path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["new_saved"], "374")
        self.assertEqual(rows[0]["deferred"], "")
        self.assertEqual(rows[1]["warning_entries"], "3")
        self.assertEqual(rows[1]["feed_oldest_jst"], "2026-11-04T04:28")

    def test_second_run_downloads_nothing_again(self):
        self.run_once(FakeSession(self.standard_feed()))
        s = FakeSession(self.standard_feed())
        row = self.run_once(s, now="2026-11-04T12:00:00")
        self.assertEqual(row["new_saved"], 0)
        self.assertEqual(s.calls, [cx.FEED_URL])  # 電文は取り直さない

    def test_broken_body_is_not_saved_and_retried_next_time(self):
        row = self.run_once(FakeSession(self.standard_feed(), broken={OSAKA}))
        self.assertEqual((row["new_saved"], row["failed"]), (2, 1))
        row = self.run_once(FakeSession(self.standard_feed()), now="2026-11-04T12:00:00")
        self.assertEqual(row["new_saved"], 1)

    def test_gap_is_flagged_when_feed_starts_after_previous_run(self):
        self.run_once(FakeSession(self.standard_feed()), now="2026-11-01T00:00:00")
        row = self.run_once(FakeSession(self.standard_feed()), now="2026-11-04T12:00:00")
        self.assertEqual(row["gap_suspected"], 1)
        row = self.run_once(FakeSession(self.standard_feed()), now="2026-11-04T13:00:00")
        self.assertEqual(row["gap_suspected"], 0)

    def test_log_is_written_per_month(self):
        self.run_once(FakeSession(self.standard_feed()), now="2026-11-30T23:30:00")
        self.run_once(FakeSession(self.standard_feed()), now="2026-12-01T00:30:00")
        for month in ("2026-11", "2026-12"):
            with open(cx.DATA_DIR / f"fetch_log_{month}.csv", encoding="utf-8") as f:
                self.assertEqual(len(list(csv.DictReader(f))), 1)

    def test_empty_feed(self):
        row = self.run_once(FakeSession(feed()))
        self.assertEqual((row["feed_entries"], row["new_saved"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
