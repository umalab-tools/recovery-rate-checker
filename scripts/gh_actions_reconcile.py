#!/usr/bin/env python3
"""GitHub Actions上で独立稼働する結果反映スクリプト(2026-11-08新設)。

Mac側(~/claude-work/keiba-analysis)が起動していない・スリープ中でも、このリポジトリ
だけで公式結果との照合を完結させる。Pythonの標準ライブラリのみを使用し、
pip installを待たずに毎回高速に実行できるようにしている。

【重要な設計方針(安全第一)】
このスクリプトは「ゼロから全履歴を再計算」はしない。公式の当日ファイルは
"今日"のレースしか含まないため、過去の確定済み履歴を再現する手段を持たない。
そのため、Mac側が既に書き出した既知の確定済み結果(published_picks_results.json)
を土台として読み込み、そこに「今日まだ未確定だったものが、今日の公式データで
新たに確定できたか」だけを追記する。これにより、GitHub Actionsだけが先に実行
されても過去の履歴を消してしまうことがない。

入力:
  - published_picks.json          (全期間の買い目一覧。Mac側が書き出す)
  - published_picks_results.json (Mac側が既に確定済みの結果。無ければ空として続行)
取得: https://www.keiba.go.jp/KeibaWeb/DataDownload/ の公式當日ファイル(payback.csv、
      "今日"のレースのみ取得可能)
出力: simulation_pnl.json(実質的な差分があるときのみ上書き)

実際の馬券購入・自動投票は一切行わない。公式データに結果がまだ無いレースは
「未確定」のまま変更しない(推測で的中/不的中を決めない)。
"""
from __future__ import annotations

import csv
import datetime
import io
import json
import os
import sys
import urllib.request
import urllib.error
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PICKS_PATH = os.path.join(BASE, "published_picks.json")
KNOWN_RESULTS_PATH = os.path.join(BASE, "published_picks_results.json")
SIM_PNL_PATH = os.path.join(BASE, "simulation_pnl.json")
BASE_URL = "https://www.keiba.go.jp/KeibaWeb/DataDownload/RaceDataDownload"
HITRATE_ROI_STATUSES = {"的中", "不的中"}
VOID_STATUSES = {"返還", "取消"}
MIN_SAMPLE_SIZE = 30


def fetch_today_payback(today: str) -> list[dict]:
    """公式サイトから当日のpayback.csvを取得する。取得できない場合は空リスト
    (=今回は新規確定を見つけられない。既存の既知結果はそのまま維持する)。"""
    url = f"{BASE_URL}?type=daily"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"[警告] 公式データ取得に失敗(今回は新規確定なし、既知の結果のみで継続): {e}")
        return []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            name = f"{today}_payback.csv"
            if name not in zf.namelist():
                print(f"[情報] {name} はZIP内に見つかりません(本日分はまだ生成されていない可能性)")
                return []
            with zf.open(name) as f:
                text = io.TextIOWrapper(f, encoding="utf-8-sig")
                return list(csv.DictReader(text))
    except zipfile.BadZipFile as e:
        print(f"[警告] ZIP展開に失敗(今回は新規確定なし、既知の結果のみで継続): {e}")
        return []


def lookup_wide_payout(payback_rows: list[dict], track: str, race_date: str, race_no: int,
                        combo: str) -> float | None:
    """該当レースが公式データに無ければNone(未確定)。あれば組番を正規化して
    ワイド払戻金を照合する(0円なら不的中)。"""
    nums = sorted(int(x) for x in combo.split("-"))
    matches = [r for r in payback_rows if r.get("競馬場") == track
               and r.get("競走年月日") == race_date and str(r.get("レース番号")) == str(race_no)]
    if not matches:
        return None
    row = matches[0]
    for i in (1, 2, 3):
        c1, c2 = row.get(f"ワイド組番{i}馬番1"), row.get(f"ワイド組番{i}馬番2")
        if not c1:
            continue
        if {int(c1), int(c2)} == set(nums):
            payout = row.get(f"ワイド払戻金{i}（円）")
            return float(payout) if payout else 0.0
    return 0.0


def row_key(r: dict) -> tuple:
    return (r["track"], r["race_date"], r["race_no"], r["combo"])


def merge_new_confirmations(picks: list[dict], known_results: list[dict],
                             payback_rows: list[dict], today: str) -> list[dict]:
    """既知の確定結果(known_results)をベースに、今日の公式データで新たに
    確定できたレースだけを追記する。既にある結果は上書きしない(ゼロから
    再計算しない)。"""
    known_keys = {row_key(r) for r in known_results}
    merged = list(known_results)
    for p in picks:
        key = row_key(p)
        if key in known_keys:
            continue  # 既に確定済み、変更しない
        if p["race_date"] != today:
            continue  # 当日ファイルは今日のレースしか含まないため対象外
        payout = lookup_wide_payout(payback_rows, p["track"], p["race_date"], p["race_no"], p["combo"])
        if payout is None:
            continue  # まだ未確定、推測しない
        merged.append({**p, "status": "的中" if payout > 0 else "不的中", "payout_per_100": payout,
                        "reconciled_at": datetime.datetime.now().isoformat(timespec="seconds")})
    return merged


def row_stake(judgement: str, stake: float) -> float:
    return stake if judgement == "買う" else 0.0


def row_payout(status: str, stake: float, payout_per_100: float | None) -> float | None:
    if status in VOID_STATUSES:
        return stake
    if status == "的中":
        return round((payout_per_100 or 0) / 100 * stake, 2)
    if status == "不的中":
        return 0.0
    return None


def filter_by_period(rows: list[dict], period: str, today: str) -> list[dict]:
    if period == "all":
        return rows
    today_dt = datetime.datetime.strptime(today, "%Y%m%d")
    out = []
    for r in rows:
        d = datetime.datetime.strptime(r["race_date"], "%Y%m%d")
        if period == "daily" and d.date() == today_dt.date():
            out.append(r)
        elif period == "weekly" and 0 <= (today_dt - d).days < 7:
            out.append(r)
        elif period == "monthly" and d.year == today_dt.year and d.month == today_dt.month:
            out.append(r)
    return out


def compute_period(rows: list[dict], all_picks_in_period: list[dict], stake_per_bet: float) -> dict:
    confirmed = [r for r in rows if row_stake(r["judgement"], stake_per_bet) > 0]
    purchase_total = sum(stake_per_bet for _ in confirmed)
    payout_total = sum(row_payout(r["status"], stake_per_bet, r.get("payout_per_100")) or 0 for r in confirmed)
    pnl = payout_total - purchase_total if purchase_total > 0 else None
    roi = round(payout_total / purchase_total * 100, 2) if purchase_total > 0 else None
    hr_rows = [r for r in confirmed if r["status"] in HITRATE_ROI_STATUSES]
    n_hit = sum(1 for r in hr_rows if r["status"] == "的中")
    n_miss = sum(1 for r in hr_rows if r["status"] == "不的中")
    n_denom = n_hit + n_miss
    return {
        "purchase_total": purchase_total,
        "payout_total": payout_total if purchase_total > 0 else None,
        "pnl": pnl, "roi": roi,
        "n_hit": n_hit, "n_miss": n_miss,
        "hit_rate": round(n_hit / n_denom * 100, 1) if n_denom > 0 else None,
        "n_races_bought": len(confirmed),
        "n_races_skipped": sum(1 for p in all_picks_in_period if p["judgement"] != "買う"),
        "pending_amount": 0, "n_pending": 0,
        "sample_size_warning": len(confirmed) < MIN_SAMPLE_SIZE,
        "note": "GitHub Actions(独立稼働)側で計算。Mac側のsimulation_pnl.pyと同一の計算式を使用。",
    }


def strip_timestamps(obj):
    if isinstance(obj, dict):
        return {k: strip_timestamps(v) for k, v in obj.items() if k not in ("generated_at", "reconciled_at")}
    if isinstance(obj, list):
        return [strip_timestamps(v) for v in obj]
    return obj


def main() -> int:
    if not os.path.exists(PICKS_PATH):
        print("[情報] published_picks.jsonが無いため何もしない")
        return 0
    with open(PICKS_PATH, encoding="utf-8") as f:
        picks_data = json.load(f)
    picks = picks_data.get("picks", [])
    stake_per_bet = picks_data.get("stake_per_bet", 1000.0)

    known_results = []
    if os.path.exists(KNOWN_RESULTS_PATH):
        with open(KNOWN_RESULTS_PATH, encoding="utf-8") as f:
            known_results = json.load(f).get("results", [])

    today = datetime.datetime.now().strftime("%Y%m%d")
    payback_rows = fetch_today_payback(today)
    merged = merge_new_confirmations(picks, known_results, payback_rows, today)

    picks_by_key = {row_key(p): p for p in picks}

    by_track: dict[str, list[dict]] = {}
    for r in merged:
        by_track.setdefault(r["track"], []).append(r)
    picks_by_track: dict[str, list[dict]] = {}
    for p in picks:
        picks_by_track.setdefault(p["track"], []).append(p)

    report = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "date": today,
        "stake_per_bet": stake_per_bet,
        "source": "github_actions",
        "note": "GitHub Actions上で独立稼働する結果反映(Macがスリープ中でも動作)。"
                 "対象は公式配信の買い目のみ。実際の馬券購入は行っていない。"
                 "公式の当日ファイルは今日のレースのみ確定可能なため、今日以前の未確定分は"
                 "Mac側の月次アーカイブ照合を待つ必要がある(既知の確定済み履歴は保持する)。",
        "periods": {p: compute_period(filter_by_period(merged, p, today),
                                        filter_by_period(picks, p, today), stake_per_bet)
                    for p in ("daily", "weekly", "monthly", "all")},
        "by_track": {t: compute_period(rs, picks_by_track.get(t, []), stake_per_bet)
                     for t, rs in by_track.items()},
        "max_drawdown": None,
        "n_total_rows": len(merged),
        "recent_results": sorted(merged, key=lambda r: r.get("reconciled_at") or "", reverse=True)[:10],
    }

    if os.path.exists(SIM_PNL_PATH):
        with open(SIM_PNL_PATH, encoding="utf-8") as f:
            old = json.load(f)
        if strip_timestamps(old) == strip_timestamps(report):
            print("[情報] 実質的な差分なし。ファイルを更新しない。")
            return 0

    with open(SIM_PNL_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    n_new = len(merged) - len(known_results)
    print(f"[完了] simulation_pnl.jsonを更新しました(既知{len(known_results)}件+新規確定{n_new}件)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
