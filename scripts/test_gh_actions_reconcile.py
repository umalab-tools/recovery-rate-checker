import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import gh_actions_reconcile as r  # noqa: E402


def mk_pick(track="船橋", date="20261108", race_no=1, combo="3-7", judgement="見送り"):
    return {"track": track, "race_date": date, "race_no": race_no, "combo": combo,
            "judgement": judgement, "published_at": "2026-11-08 07:00:00"}


def mk_known(track="船橋", date="20261107", race_no=1, combo="3-7", judgement="見送り",
             status="的中", payout_per_100=800.0):
    return {"track": track, "race_date": date, "race_no": race_no, "combo": combo,
            "judgement": judgement, "status": status, "payout_per_100": payout_per_100,
            "reconciled_at": "2026-11-08T00:05:00"}


def test_lookup_wide_payout_found():
    payback = [{"競馬場": "船橋", "競走年月日": "20261108", "レース番号": "1",
                "ワイド組番1馬番1": "3", "ワイド組番1馬番2": "7", "ワイド払戻金1（円）": "800"}]
    assert r.lookup_wide_payout(payback, "船橋", "20261108", 1, "3-7") == 800.0


def test_lookup_wide_payout_miss_returns_zero():
    payback = [{"競馬場": "船橋", "競走年月日": "20261108", "レース番号": "1",
                "ワイド組番1馬番1": "1", "ワイド組番1馬番2": "2", "ワイド払戻金1（円）": "500"}]
    assert r.lookup_wide_payout(payback, "船橋", "20261108", 1, "3-7") == 0.0


def test_lookup_wide_payout_race_not_found_returns_none():
    assert r.lookup_wide_payout([], "船橋", "20261108", 1, "3-7") is None


def test_merge_preserves_known_results_without_recompute():
    """既知の確定済み結果は、今日の公式データに同じレースが無くても消えない。"""
    known = [mk_known()]
    picks = [mk_pick(date="20261107")]  # 既知のレースと同じキー
    merged = r.merge_new_confirmations(picks, known, payback_rows=[], today="20261108")
    assert len(merged) == 1
    assert merged[0]["status"] == "的中"  # 既知の値のまま(上書きしない)


def test_merge_adds_newly_confirmed_today_only():
    known = []
    picks = [mk_pick(date="20261108"), mk_pick(date="20261107", race_no=2)]
    payback = [{"競馬場": "船橋", "競走年月日": "20261108", "レース番号": "1",
                "ワイド組番1馬番1": "3", "ワイド組番1馬番2": "7", "ワイド払戻金1（円）": "800"}]
    merged = r.merge_new_confirmations(picks, known, payback, today="20261108")
    # 今日分だけ新規確定、昨日分(当日ファイルに無い)は未確定のまま追加されない
    assert len(merged) == 1
    assert merged[0]["race_date"] == "20261108"
    assert merged[0]["status"] == "的中"


def test_merge_does_not_touch_other_dates_when_unconfirmable():
    known = [mk_known(date="20261105")]
    picks = [mk_pick(date="20261105"), mk_pick(date="20261108", race_no=9)]
    merged = r.merge_new_confirmations(picks, known, payback_rows=[], today="20261108")
    # 当日ファイルにデータが無いので新規確定は0件、既知の1件のみ残る
    assert len(merged) == 1


def test_compute_period_sample_size_warning():
    rows = [{"judgement": "買う", "status": "的中", "payout_per_100": 800.0}]
    stat = r.compute_period(rows, all_picks_in_period=rows, stake_per_bet=1000.0)
    assert stat["sample_size_warning"] is True
    assert stat["purchase_total"] == 1000.0
    assert stat["pnl"] == 7000.0


def test_compute_period_zero_purchase_roi_none():
    rows = []
    picks = [mk_pick()]
    stat = r.compute_period(rows, all_picks_in_period=picks, stake_per_bet=1000.0)
    assert stat["purchase_total"] == 0
    assert stat["roi"] is None


def test_strip_timestamps_ignores_generated_and_reconciled_at():
    a = {"generated_at": "t1", "x": [{"reconciled_at": "t1", "y": 1}]}
    b = {"generated_at": "t2", "x": [{"reconciled_at": "t2", "y": 1}]}
    assert r.strip_timestamps(a) == r.strip_timestamps(b)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
