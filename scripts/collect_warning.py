"""近畿の府県の気象警報・注意報（気象庁のホームページのJSON）を、変わったときだけ保存する。

気象庁のJSONは「今の最新の発表」しか返さないので、10分ごとに取りに行き、
前回保存したものと中身が違えば追記する。

    python scripts/collect_warning.py

保存先：data/warning/{府県コード}/{YYYY-MM}.jsonl（1行が1回分の発表の状態）
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

JST = ZoneInfo("Asia/Tokyo")
URL = "https://www.jma.go.jp/bosai/warning/data/r8/{code}.json"

# 近畿の府県予報区（気象庁の地域コード）
AREAS = {
    "270000": "大阪府",
    "260000": "京都府",
    "280000": "兵庫県",
    "290000": "奈良県",
    "300000": "和歌山県",
    "250000": "滋賀県",
}

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "warning"


def canonical_hash(body) -> str:
    """キーの並び順などの違いに左右されない、中身の指紋。"""
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def latest_saved_hash(code: str) -> str | None:
    """その府県で最後に保存した中身の指紋（月をまたいでも探す）。"""
    folder = DATA_DIR / code
    if not folder.exists():
        return None
    for path in sorted(folder.glob("*.jsonl"), reverse=True):
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        if lines:
            return json.loads(lines[-1])["sha256"]
    return None


def check_body(body) -> None:
    """気象庁のJSONらしい形か確かめる（エラーのページなどを保存しないため）。"""
    if not isinstance(body, list) or not body:
        raise ValueError("警報のJSONが配列でない、または空")
    for item in body:
        if not isinstance(item, dict) or "reportDatetime" not in item or "warning" not in item:
            raise ValueError("reportDatetime か warning がない要素がある")


def fetch(session: requests.Session, code: str, retries: int = 3, pause_sec: float = 5.0):
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            resp = session.get(URL.format(code=code), timeout=30)
            resp.raise_for_status()
            body = resp.json()
            check_body(body)
            return body
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(pause_sec * (attempt + 1))
    raise RuntimeError(f"{code} を取れなかった: {last_error}")


def collect(session: requests.Session, now: datetime | None = None, pause_sec: float = 1.0) -> dict[str, str]:
    """各府県を取り、変わっていれば保存する。府県ごとの結果（saved / same / failed）を返す。"""
    now = now or datetime.now(JST)
    results: dict[str, str] = {}
    for i, (code, name) in enumerate(AREAS.items()):
        if i:
            time.sleep(pause_sec)
        try:
            body = fetch(session, code)
        except RuntimeError as exc:
            print(f"{name}（{code}）：失敗 {exc}")
            results[code] = "failed"
            continue
        digest = canonical_hash(body)
        newest = max(item["reportDatetime"] for item in body)
        if digest == latest_saved_hash(code):
            print(f"{name}（{code}）：変化なし（最新の発表 {newest}）")
            results[code] = "same"
            continue
        path = DATA_DIR / code / f"{now:%Y-%m}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "fetched_at_jst": now.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "area_code": code,
            "latest_report_datetime": newest,
            "sha256": digest,
            "body": body,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        print(f"{name}（{code}）：保存（最新の発表 {newest}）")
        results[code] = "saved"
    return results


def main() -> int:
    session = requests.Session()
    repo = os.environ.get("GITHUB_REPOSITORY", "local")
    session.headers["User-Agent"] = f"kinki-opendata-collector (+https://github.com/{repo})"
    results = collect(session)
    # 一部の失敗は次の回で取り直せるので成功扱い。全部失敗したときだけ失敗にして気づけるようにする
    return 1 if results and all(v == "failed" for v in results.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
