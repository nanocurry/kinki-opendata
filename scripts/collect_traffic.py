"""近畿の直轄国道・高速の交通量（5分値）を日ごとに集めて保存する。

提供元（国交省 交通量API / JARTIC）は 5分値を約1か月しか残さないため、
まだ残っている日のうち、まだ保存していない日を取りに行く。

使い方（GitHub Actions から呼ぶ。手元で試すときも同じ）:
    python scripts/collect_traffic.py fetch   # 取得して out/ に書く
    python scripts/collect_traffic.py record  # out/ の結果を data/manifest_traffic.csv に追記する

fetch と record を分けているのは、ファイルのアップロードに成功してから
「保存済み」と記録するため（アップロードに失敗したら次の実行でやり直す）。
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

JST = ZoneInfo("Asia/Tokyo")

API_URL = "https://api.jartic-open-traffic.org/geoserver"

# 近畿のおおよその範囲（兵庫の西端〜滋賀の東端、和歌山の南端〜京都・兵庫の北端）。
# 経度 134.2〜136.5、緯度 33.4〜35.9。境目では中部・中国の観測点が少し混ざる。
KINKI_BBOX = (134.2, 33.4, 136.5, 35.9)

# 5分値のレイヤー。loop = 常設のトラフィックカウンター、cctv = CCTV映像の AI カウンター
LAYERS = {
    "loop": "t_travospublic_measure_5m",
    "cctv": "t_travospublic_measure_5m_img",
}
# 道路種別：3 = 一般国道（直轄）、1 = 高速自動車国道
ROAD_TYPES = (3, 1)

CODES_PER_DAY = 288  # 5分 × 24時間
RETENTION_DAYS = 30  # 提供元が5分値を残すおおよその日数
ACCEPT_PARTIAL_AT_AGE = 28  # この日数より古い日は、欠けがあっても消える前に保存する
MAX_MISSING_RATIO = 0.05  # 一昨日より古い日は、欠けが5%以内ならそのまま保存する
LOW_COUNT_RATIO = 0.5  # 観測点の数がその日の中央値の半分未満の時刻は「欠け」とみなす

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "out"
MANIFEST = REPO_ROOT / "data" / "manifest_traffic.csv"
PENDING = OUT_DIR / "pending_manifest.csv"

MANIFEST_FIELDS = [
    "date",
    "layer",
    "road_type",
    "file",
    "rows",
    "codes_present",
    "codes_expected",
    "missing_codes",
    "status",
    "fetched_at_jst",
]
# この状態の日は、もう取りに行かない
DONE_STATUSES = {"complete", "complete_with_gaps", "partial_before_expiry", "empty_before_expiry"}


# ---------------------------------------------------------------------------
# API


@dataclass
class Fetcher:
    session: requests.Session
    pause_sec: float = 1.0
    retries: int = 3
    timeout_sec: int = 120
    requests_made: int = 0

    def get_features(self, layer: str, road_type: int, code_from: str, code_to: str) -> list[dict]:
        """時間コード code_from〜code_to の観測値を取る。打ち切られていたら5分ずつに分けて取り直す。"""
        body = self._request(layer, road_type, code_from, code_to)
        features = body.get("features", [])
        matched = body.get("numberMatched", body.get("totalFeatures"))
        returned = body.get("numberReturned", len(features))
        truncated = isinstance(matched, int) and matched > returned
        if truncated and code_from != code_to:
            features = []
            for code in _codes_between(code_from, code_to):
                features.extend(self.get_features(layer, road_type, code, code))
        elif truncated:
            raise RuntimeError(f"{layer} {code_from}: {returned}/{matched} 件しか返らない（1時刻でも打ち切り）")
        return features

    def _request(self, layer: str, road_type: int, code_from: str, code_to: str) -> dict:
        min_x, min_y, max_x, max_y = KINKI_BBOX
        cql = (
            f"道路種別={road_type} AND 時間コード>={code_from} AND 時間コード<={code_to} "
            f"AND BBOX(ジオメトリ,{min_x},{min_y},{max_x},{max_y},'EPSG:4326')"
        )
        params = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": layer,
            "srsName": "EPSG:4326",
            "outputFormat": "application/json",
            "exceptions": "application/json",
            "cql_filter": cql,
        }
        last_error: Exception | None = None
        for attempt in range(self.retries):
            if self.requests_made:
                time.sleep(self.pause_sec)
            self.requests_made += 1
            try:
                resp = self.session.get(API_URL, params=params, timeout=self.timeout_sec)
                resp.raise_for_status()
                body = resp.json()
                if "exceptions" in body or body.get("type") != "FeatureCollection":
                    raise RuntimeError(f"API がエラーを返した: {json.dumps(body, ensure_ascii=False)[:300]}")
                return body
            except Exception as exc:  # noqa: BLE001 - 通信まわりはまとめてやり直す
                last_error = exc
                time.sleep(self.pause_sec * (attempt + 2) * 5)
        raise RuntimeError(f"{layer} 道路種別{road_type} {code_from}〜{code_to} を取れなかった: {last_error}")


def _codes_between(code_from: str, code_to: str) -> list[str]:
    start = datetime.strptime(code_from, "%Y%m%d%H%M")
    end = datetime.strptime(code_to, "%Y%m%d%H%M")
    codes = []
    while start <= end:
        codes.append(start.strftime("%Y%m%d%H%M"))
        start += timedelta(minutes=5)
    return codes


# ---------------------------------------------------------------------------
# 1日分


@dataclass
class DayResult:
    day: date
    layer: str
    road_type: int
    rows: list[dict] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    codes_present: set[str] = field(default_factory=set)
    missing_codes: list[str] = field(default_factory=list)

    @property
    def missing_ratio(self) -> float:
        return len(self.missing_codes) / CODES_PER_DAY


def point_of(geometry: dict | None) -> tuple[float | None, float | None]:
    """Point（[経度, 緯度]）でも MultiPoint（[[経度, 緯度]]）でも、空でも読む。"""
    coords = (geometry or {}).get("coordinates")
    while isinstance(coords, list) and coords and isinstance(coords[0], list):
        coords = coords[0]
    if isinstance(coords, list) and len(coords) >= 2:
        return coords[0], coords[1]
    return None, None


def fetch_day(fetcher: Fetcher, day: date, layer_key: str, road_type: int) -> DayResult:
    layer = LAYERS[layer_key]
    result = DayResult(day=day, layer=layer_key, road_type=road_type)
    ymd = day.strftime("%Y%m%d")
    for hour in range(24):
        features = fetcher.get_features(layer, road_type, f"{ymd}{hour:02d}00", f"{ymd}{hour:02d}55")
        if features and not getattr(fetcher, "_shown_sample", set()) >= {layer_key}:
            # 提供元の形が変わっても気づけるよう、レイヤーごとに最初の1件の様子をログに出す
            sample = features[0]
            print(f"  [{layer_key}] 1件目の位置: {json.dumps(sample.get('geometry'), ensure_ascii=False)[:200]}")
            print(f"  [{layer_key}] 列: {list((sample.get('properties') or {}).keys())}")
            fetcher._shown_sample = getattr(fetcher, "_shown_sample", set()) | {layer_key}
        for feat in features:
            props = dict(feat.get("properties") or {})
            props["経度"], props["緯度"] = point_of(feat.get("geometry"))
            for key in props:
                if key not in result.columns:
                    result.columns.append(key)
            result.rows.append(props)

    # 時刻ごとの観測点の数。少なすぎる時刻は欠けとみなす（提供元の途中経過など）
    counts: dict[str, int] = {}
    for row in result.rows:
        code = str(row.get("時間コード", ""))
        counts[code] = counts.get(code, 0) + 1
    expected = _codes_between(f"{ymd}0000", f"{ymd}2355")
    if counts:
        median = statistics.median(counts.values())
        result.codes_present = {c for c, n in counts.items() if n >= median * LOW_COUNT_RATIO}
    result.missing_codes = [c for c in expected if c not in result.codes_present]
    # 経度・緯度を最後の2列にそろえる
    result.columns = [c for c in result.columns if c not in ("経度", "緯度")] + ["経度", "緯度"]
    return result


def decide_status(result: DayResult, age_days: int) -> str | None:
    """保存するなら状態の名前を返す。まだ待つなら None。"""
    if not result.rows:
        return "empty_before_expiry" if age_days >= ACCEPT_PARTIAL_AT_AGE else None
    if not result.missing_codes:
        return "complete"
    if age_days >= 2 and result.missing_ratio <= MAX_MISSING_RATIO:
        return "complete_with_gaps"
    if age_days >= ACCEPT_PARTIAL_AT_AGE:
        return "partial_before_expiry"
    return None


def file_name(result: DayResult) -> str:
    return f"kinki_{result.layer}_5m_road{result.road_type}_{result.day:%Y%m%d}.csv.gz"


def write_csv_gz(result: DayResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=result.columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    result.rows.sort(key=lambda r: (str(r.get("時間コード", "")), str(r.get("常時観測点コード", ""))))
    writer.writerows(result.rows)
    # mtime=0 で、同じ中身なら同じファイルになるようにする
    with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        gz.write(buf.getvalue().encode("utf-8"))


# ---------------------------------------------------------------------------
# どの日を取りに行くか


def read_manifest(path: Path | None = None) -> list[dict]:
    path = path or MANIFEST
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def done_keys(manifest_rows: list[dict]) -> set[tuple[str, str, str]]:
    return {(r["date"], r["layer"], r["road_type"]) for r in manifest_rows if r["status"] in DONE_STATUSES}


def days_to_fetch(today: date, done: set[tuple[str, str, str]], max_days: int) -> list[date]:
    """まだ全部そろっていない日を、消えそうな日から順に、次に新しい日から順に並べる。"""
    todo = []
    for age in range(1, RETENTION_DAYS + 1):
        day = today - timedelta(days=age)
        keys = {(day.isoformat(), layer, str(road)) for layer in LAYERS for road in ROAD_TYPES}
        if not keys <= done:
            todo.append(day)
    near_expiry = sorted([d for d in todo if (today - d).days >= ACCEPT_PARTIAL_AT_AGE])
    rest = sorted([d for d in todo if (today - d).days < ACCEPT_PARTIAL_AT_AGE], reverse=True)
    return (near_expiry + rest)[:max_days]


# ---------------------------------------------------------------------------
# コマンド


def cmd_fetch(args: argparse.Namespace, session: requests.Session | None = None) -> int:
    now = datetime.fromisoformat(args.now).astimezone(JST) if args.now else datetime.now(JST)
    today = now.date()
    done = done_keys(read_manifest())
    targets = days_to_fetch(today, done, args.max_days)
    print(f"今日（JST）: {today}。取りに行く日: {[d.isoformat() for d in targets] or 'なし'}")

    if session is None:
        session = requests.Session()
        repo = os.environ.get("GITHUB_REPOSITORY", "local")
        session.headers["User-Agent"] = f"kinki-opendata-collector (+https://github.com/{repo})"
    fetcher = Fetcher(session=session, pause_sec=args.pause)

    OUT_DIR.mkdir(exist_ok=True)
    pending: list[dict] = []
    deadline = time.monotonic() + args.time_budget_min * 60
    for day in targets:
        age = (today - day).days
        for layer in LAYERS:
            for road in ROAD_TYPES:
                if (day.isoformat(), layer, str(road)) in done:
                    continue
                if time.monotonic() > deadline:
                    print("時間の上限に達したので、続きは次の実行で取る")
                    return _finish(pending)
                result = fetch_day(fetcher, day, layer, road)
                status = decide_status(result, age)
                label = f"{day} {layer} 道路種別{road}: {len(result.rows)}行、欠け{len(result.missing_codes)}/{CODES_PER_DAY}"
                if status is None:
                    print(f"{label} → まだ待つ（次の実行で取り直す）")
                    continue
                name = ""
                if result.rows:
                    name = file_name(result)
                    write_csv_gz(result, OUT_DIR / name)
                print(f"{label} → {status}")
                pending.append(
                    {
                        "date": day.isoformat(),
                        "layer": layer,
                        "road_type": str(road),
                        "file": name,
                        "rows": str(len(result.rows)),
                        "codes_present": str(len(result.codes_present)),
                        "codes_expected": str(CODES_PER_DAY),
                        "missing_codes": ";".join(c[-4:] for c in result.missing_codes),
                        "status": status,
                        "fetched_at_jst": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z"),
                    }
                )
    return _finish(pending)


def _finish(pending: list[dict]) -> int:
    with open(PENDING, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(pending)
    print(f"保存する日：{len(pending)}件（{PENDING.relative_to(REPO_ROOT)}）")
    return 0


def cmd_record(_args: argparse.Namespace) -> int:
    """アップロードが済んだあとに呼ぶ。pending を manifest に足す。"""
    if not PENDING.exists():
        print("pending がないので何もしない")
        return 0
    new_rows = read_manifest(PENDING)
    rows = read_manifest()
    keys = {(r["date"], r["layer"], r["road_type"]) for r in new_rows}
    rows = [r for r in rows if (r["date"], r["layer"], r["road_type"]) not in keys] + new_rows
    rows.sort(key=lambda r: (r["date"], r["layer"], r["road_type"]))
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(f"manifest に {len(new_rows)} 件を記録")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_fetch = sub.add_parser("fetch", help="取得して out/ に書く")
    p_fetch.add_argument("--max-days", type=int, default=8, help="1回の実行で取りに行く日数の上限")
    p_fetch.add_argument("--pause", type=float, default=1.0, help="リクエストの間の待ち時間（秒）")
    p_fetch.add_argument("--time-budget-min", type=float, default=240, help="1回の実行の時間の上限（分）")
    p_fetch.add_argument("--now", help="今の時刻を差し替える（試験用、ISO形式）")
    sub.add_parser("record", help="アップロード後に manifest へ記録する")
    args = parser.parse_args(argv)
    return cmd_fetch(args) if args.cmd == "fetch" else cmd_record(args)


if __name__ == "__main__":
    sys.exit(main())
