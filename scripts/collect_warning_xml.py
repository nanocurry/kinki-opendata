"""全国の気象警報・注意報の電文を、気象庁防災情報XMLの「随時・長期フィード」から集める。

（11/4 の初回は近畿6府県だけ。同じ日から全国。分析は近畿に絞る）

長期フィード（extra_l.xml）は毎時更新で、数日分の全入電を載せている。
そのため GitHub の定期実行が1〜2時間遅れても、電文を1件ずつ取りこぼさずに拾える
（ホームページのJSONは「今の最新」しか返さないので、実行が遅れると間の発表を失う）。

    python scripts/collect_warning_xml.py

保存先：
    data/warning_xml/{府県コード}/{YYYY-MM}/{電文のファイル名}.gz   電文そのもの（加工なし、gzip）
    data/warning_xml/fetch_log_{YYYY-MM}.csv                         実行ごとの記録
"""

from __future__ import annotations

import csv
import gzip
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

JST = timezone(timedelta(hours=9), "JST")
FEED_URL = "https://www.data.jma.go.jp/developer/xml/feed/extra_l.xml"
ATOM = {"a": "http://www.w3.org/2005/Atom"}

# 集める府県（発表官署の地域コード。電文のファイル名の末尾に入っている）。None なら全国
TARGET_AREAS: set[str] | None = None
# 1回の実行で電文を取る件数の上限。残りは次の回に回す（フィードには7日分残る）
MAX_DOWNLOADS = 1500

# 近畿の府県（分析で使う。ログの表示にも使う）
AREAS = {
    "270000": "大阪府",
    "260000": "京都府",
    "280000": "兵庫県",
    "290000": "奈良県",
    "300000": "和歌山県",
    "250000": "滋賀県",
}
# 気象警報・注意報の電文の種類（旧体系 VPWW53/54、令和8年体系 VPWW55〜 などをまとめて拾う）
TYPE_PREFIX = "VPWW"
# 例：20261103192830_0_VPWW54_300000.xml
FILE_RE = re.compile(r"(?P<name>\d{14}_\d+_(?P<type>[A-Z0-9]+)_(?P<area>\d{6})\.xml)$")

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "warning_xml"
LOG_FIELDS = [
    "run_at_jst",
    "feed_entries",
    "feed_oldest_jst",
    "feed_newest_jst",
    "kinki_warning_entries",
    "new_saved",
    "failed",
    "gap_suspected",
    "warning_entries",
    "deferred",
]


def to_jst(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(JST)


def get(session: requests.Session, url: str, retries: int = 3, pause_sec: float = 5.0) -> bytes:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=60)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(pause_sec * (attempt + 1))
    raise RuntimeError(f"{url} を取れなかった: {last_error}")


def parse_feed(xml_bytes: bytes) -> list[dict]:
    """フィードの entry を {updated, url, name, type, area} の一覧にする。"""
    root = ET.fromstring(xml_bytes)
    entries = []
    for e in root.findall("a:entry", ATOM):
        updated = e.findtext("a:updated", default="", namespaces=ATOM)
        link = e.find("a:link", ATOM)
        url = link.get("href", "") if link is not None else ""
        m = FILE_RE.search(url)
        entries.append(
            {
                "updated": to_jst(updated) if updated else None,
                "url": url,
                "name": m["name"] if m else "",
                "type": m["type"] if m else "",
                "area": m["area"] if m else "",
            }
        )
    return entries


def saved_path(entry: dict) -> Path:
    month = f"{entry['name'][:4]}-{entry['name'][4:6]}"
    return DATA_DIR / entry["area"] / month / f"{entry['name']}.gz"


def last_run_at() -> datetime | None:
    """前回の実行の時刻（取りこぼしの疑いを判定するため）。"""
    logs = sorted(DATA_DIR.glob("fetch_log_*.csv"), reverse=True)
    for path in logs:
        with open(path, newline="", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r["feed_entries"] not in ("", "0")]
        if rows:
            return datetime.fromisoformat(rows[-1]["run_at_jst"])
    return None


def append_log(row: dict, now: datetime) -> None:
    path = DATA_DIR / f"fetch_log_{now:%Y-%m}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    old_rows: list[dict] = []
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames != LOG_FIELDS:  # 列が増えたときは、前の行を新しい列で書き直す
                old_rows = list(reader)
    if old_rows:
        path.unlink()
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS, restval="", lineterminator="\n")
        if new:
            w.writeheader()
        w.writerows(old_rows)
        w.writerow(row)


def area_name(code: str) -> str:
    return AREAS.get(code, code)


def collect(session: requests.Session, now: datetime | None = None, pause_sec: float = 0.3) -> dict:
    now = now or datetime.now(JST)
    previous = last_run_at()
    entries = parse_feed(get(session, FEED_URL))
    times = [e["updated"] for e in entries if e["updated"]]
    targets = [
        e for e in entries
        if e["area"] and e["type"].startswith(TYPE_PREFIX) and (TARGET_AREAS is None or e["area"] in TARGET_AREAS)
    ]
    kinki = sum(1 for e in targets if e["area"] in AREAS)
    todo = [e for e in targets if not saved_path(e).exists()]
    # 古い順に取る（上限で残すのは新しい分＝フィードに長く残る分）
    todo.sort(key=lambda e: e["name"])
    deferred = max(0, len(todo) - MAX_DOWNLOADS)
    todo = todo[:MAX_DOWNLOADS]

    saved = failed = 0
    for e in todo:
        path = saved_path(e)
        time.sleep(pause_sec)
        try:
            body = get(session, e["url"])
            ET.fromstring(body)  # XML として読めないもの（エラーのページなど）は保存しない
        except Exception as exc:  # noqa: BLE001 - 1件の失敗で止めない。次の回に取り直す
            print(f"失敗：{e['name']}（{exc}）")
            failed += 1
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
            gz.write(body)
        if e["area"] in AREAS:
            print(f"保存：{area_name(e['area'])} {e['type']} {e['updated']:%Y-%m-%d %H:%M}（{e['name']}）")
        saved += 1

    # 前回の実行より後の分しかフィードに残っていなければ、間の電文を取りこぼしたかもしれない
    oldest = min(times) if times else None
    gap = bool(previous and oldest and oldest > previous)
    if gap:
        print(f"注意：フィードの最も古い入電（{oldest:%Y-%m-%d %H:%M}）が前回の実行（{previous:%Y-%m-%d %H:%M}）より新しい。間を取りこぼした可能性がある")

    row = {
        "run_at_jst": now.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "feed_entries": len(entries),
        "feed_oldest_jst": oldest.strftime("%Y-%m-%dT%H:%M") if oldest else "",
        "feed_newest_jst": max(times).strftime("%Y-%m-%dT%H:%M") if times else "",
        "kinki_warning_entries": kinki,
        "new_saved": saved,
        "failed": failed,
        "gap_suspected": int(gap),
        "warning_entries": len(targets),
        "deferred": deferred,
    }
    append_log(row, now)
    print(
        f"フィード {len(entries)} 件（{row['feed_oldest_jst']}〜{row['feed_newest_jst']}）、"
        f"警報 {len(targets)} 件（うち近畿 {kinki}）、新しく保存 {saved} 件、失敗 {failed} 件、次の回に回した {deferred} 件"
    )
    return row


def main() -> int:
    session = requests.Session()
    repo = os.environ.get("GITHUB_REPOSITORY", "local")
    session.headers["User-Agent"] = f"kinki-opendata-collector (+https://github.com/{repo})"
    row = collect(session)
    return 1 if row["feed_entries"] == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
