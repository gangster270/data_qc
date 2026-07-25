#!/usr/bin/env python3
"""파이프라인 검증 테스트.

    python tests/make_sample_data.py     # 합성 자료 생성(선행)
    python tests/test_pipeline.py        # 전체 검증

pytest 로도 그대로 실행된다(test_* 함수 규약 준수).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import io_logger, preprocess, qc_rules, sensor_check, sensor_map   # noqa: E402
from src.config import load_config                              # noqa: E402

SAMPLE = ROOT / "tests" / "sample" / "z6-99999_260601-0900.xlsx"
GROWTH = ROOT / "tests" / "sample" / "growth.csv"
CFG = load_config()


def _load_grid():
    raw, _ = io_logger.load_env_files([str(SAMPLE)])
    ts_df, ts_report = io_logger.prepare_timestamp(raw)
    std, map_report = io_logger.standardize(ts_df)
    grid, gaps = io_logger.reindex_full_grid(std, interval_minutes=10)
    return grid, map_report, gaps, ts_report


# ---------------------------------------------------------------------
# 읽기 · 표준화
# ---------------------------------------------------------------------
def test_read_and_standardize():
    grid, map_report, gaps, ts_report = _load_grid()
    cols = set(grid.columns)
    assert {"timestamp", "temp", "rh", "soil_temp", "vwc", "ppfd", "solar", "ec"} <= cols
    # 로거 내부온도·배터리는 작물환경 변수로 잡히면 안 된다
    assert "Logger Temperature" not in " ".join(map_report["원본열"].tolist()) or \
           not (map_report["표준변수"] == "temp").sum() > 1
    # 기온과 지온이 서로 뒤바뀌지 않았는지(기온 일교차가 지온보다 큼)
    assert grid["temp"].std() > grid["soil_temp"].std()
    # 진성 오류값(ERROR 토큰)이 집계된다
    assert int(map_report.loc[map_report["표준변수"] == "ec", "오류값수"].iloc[0]) == 6
    print("✓ 읽기·표준화")


def test_missing_grid():
    grid, _, gaps, _ = _load_grid()
    n_missing = int((grid["qc_status"] == "missing_timestamp_inserted").sum())
    assert n_missing == 48, n_missing              # 주입한 8시간 결측 = 48건
    assert len(gaps) == 1 and int(gaps.iloc[0]["결측개수"]) == 48
    print("✓ 결측 timestamp 격자 정합")


# ---------------------------------------------------------------------
# 전처리
# ---------------------------------------------------------------------
def test_dli_math():
    """DLI = Σ(PPFD × 600) / 1e6 인지 직접 검산."""
    ts = pd.date_range("2026-05-01", periods=144, freq="10min")
    ppfd = np.zeros(144)
    ppfd[36:108] = 1000.0                              # 06:00~18:00 동안 1000 µmol
    df = pd.DataFrame({"timestamp": ts, "ppfd": ppfd})
    daily = preprocess.to_daily(df, interval_minutes=10)
    expected = 72 * 1000 * 600 / 1e6                    # = 43.2 mol m-2 d-1
    assert abs(daily["dli"].iloc[0] - expected) < 1e-9
    assert abs(daily["photoperiod_h"].iloc[0] - 12.0) < 1e-9
    print(f"✓ DLI 계산 ({expected} mol m-2 d-1)")


def test_vpd_from_10min():
    """VPD 는 10분 자료에서 계산해야 하며, 일평균 T·RH 로 계산한 값보다 크다."""
    ts = pd.date_range("2026-05-01", periods=144, freq="10min")
    temp = np.where(np.arange(144) < 72, 15.0, 30.0)
    rh = np.where(np.arange(144) < 72, 90.0, 50.0)
    df = pd.DataFrame({"timestamp": ts, "temp": temp, "rh": rh})
    daily = preprocess.to_daily(df, interval_minutes=10)
    naive = preprocess.compute_vpd(pd.Series([22.5]), pd.Series([70.0])).iloc[0]
    assert daily["vpd_mean"].iloc[0] > naive
    print(f"✓ VPD 10분 계산 ({daily['vpd_mean'].iloc[0]:.3f} > 일평균으로 계산 시 {naive:.3f})")


def test_range_mask():
    grid, _, _, _ = _load_grid()
    clean, report = preprocess.mask_out_of_range(grid.drop(columns=["qc_status"]), CFG["sensors"])
    assert int(report.loc[report["키"] == "temp", "결측처리건수"].iloc[0]) == 18
    assert clean["temp"].min() > -20
    print("✓ 범위 이탈값 결측 처리")


def test_cadence_and_intervals():
    growth = pd.read_csv(GROWTH)
    growth["date"] = pd.to_datetime(growth["date"])
    assert preprocess.detect_cadence(growth["date"]) == 10       # 10일 간격 자동 인식

    iv = preprocess.build_intervals(growth["date"], first_start="2026-04-01")
    # 두 번째 구간은 '직전 조사일 + 1일 ~ 당일'
    assert iv.iloc[1]["start"] == pd.Timestamp("2026-04-11")
    assert iv.iloc[1]["end"] == pd.Timestamp("2026-04-20")
    assert iv["days_expected"].iloc[1] == 10

    # 시차 3일 → 구간 전체가 3일 앞당겨짐
    iv3 = preprocess.build_intervals(growth["date"], first_start="2026-04-01", lag_days=3)
    assert iv3.iloc[1]["start"] == pd.Timestamp("2026-04-08")
    assert iv3.iloc[1]["end"] == pd.Timestamp("2026-04-17")

    # 고정창 7일
    iv7 = preprocess.build_intervals(growth["date"], window_days=7)
    assert (iv7["days_expected"] == 7).all()

    # 불규칙 간격(7↔10일 혼재)도 그대로 처리
    mixed = pd.Series(pd.to_datetime(["2026-04-10", "2026-04-17", "2026-04-27", "2026-05-04"]))
    ivm = preprocess.build_intervals(mixed, first_start="2026-04-04")
    assert ivm["days_expected"].tolist() == [7, 7, 10, 7]
    print("✓ 조사간격 자동 추정 · 시차 · 고정창 · 불규칙 간격")


def test_interval_aggregation():
    grid, _, _, _ = _load_grid()
    clean, _ = preprocess.mask_out_of_range(grid.drop(columns=["qc_status"]), CFG["sensors"])
    daily = preprocess.to_daily(clean, interval_minutes=10)
    growth = pd.read_csv(GROWTH)
    growth["date"] = pd.to_datetime(growth["date"])
    iv = preprocess.build_intervals(growth["date"], first_start="2026-04-01")
    env_iv = preprocess.aggregate_intervals(daily, iv)

    # 적산변수는 구간 합계, 평균변수는 구간 평균과 일치해야 한다
    sub = daily[(daily["date"] >= iv.iloc[1]["start"]) & (daily["date"] <= iv.iloc[1]["end"])]
    assert abs(env_iv.iloc[1]["dli_sum"] - sub["dli"].sum()) < 1e-6
    assert abs(env_iv.iloc[1]["temp_mean"] - sub["temp_mean"].mean()) < 1e-6
    # 누적 DLI 는 단조 증가
    assert env_iv["cum_dli"].is_monotonic_increasing
    # 결측이 많은 4/30 이 포함된 구간은 완전성 표시가 내려감
    assert env_iv.loc[env_iv["interval_id"] == 3, "record_completeness"].iloc[0] < 1.0

    merged = preprocess.match_growth(growth, env_iv)
    assert len(merged) == len(growth)                     # 생육 행 수가 보존
    assert merged["dli_sum"].notna().all()
    print("✓ 구간 집계 · 생육 병합")


# ---------------------------------------------------------------------
# QC 규칙
# ---------------------------------------------------------------------
def test_qc_rules_detect_injected_faults():
    grid, map_report, _, _ = _load_grid()
    alerts = qc_rules.run_all(grid, CFG, lookback_days=60,
                              now="2026-05-31 00:10", map_report=map_report)
    rules = set(alerts["rule"])
    for expected in ("R01_timestamp_gap", "R02_missing_ratio", "R03_out_of_range",
                     "R04_flatline", "R06_daytime_dark", "R12_error_value"):
        assert expected in rules, f"{expected} 미탐지 (탐지된 규칙: {sorted(rules)})"

    # 주입한 이상과 날짜가 맞는지
    gap = alerts[alerts["rule"] == "R01_timestamp_gap"].iloc[0]
    assert gap["start"].strftime("%m-%d") == "04-30" and gap["level"] == "CRITICAL"
    rh_miss = alerts[(alerts["rule"] == "R02_missing_ratio") & (alerts["variable"] == "rh")]
    assert (rh_miss["value"] == 1.0).any()               # 5/15 습도 100% 결측
    flat = alerts[alerts["rule"] == "R04_flatline"].iloc[0]
    assert flat["variable"] == "soil_temp"
    dark = alerts[alerts["rule"] == "R06_daytime_dark"].iloc[0]
    assert dark["start"].strftime("%m-%d") == "05-25"
    print(f"✓ QC 규칙 탐지 ({len(alerts)}건, 규칙 {len(rules)}종)")


def test_no_false_positive_on_clean_data():
    """정상 자료에는 결측·오류 알림이 뜨지 않아야 한다(오탐 확인)."""
    ts = pd.date_range("2026-05-01", periods=144 * 10, freq="10min")
    hour = ts.hour + ts.minute / 60
    df = pd.DataFrame({
        "timestamp": ts,
        "temp": 22 + 6 * np.sin((hour - 9) / 24 * 2 * np.pi) + np.random.default_rng(1).normal(0, .3, len(ts)),
        "rh": 70 + np.random.default_rng(2).normal(0, 3, len(ts)),
        "ppfd": np.clip(np.sin((hour - 6) / 13 * np.pi), 0, None) * 1000,
    })
    alerts = qc_rules.run_all(df, CFG, lookback_days=10, now=ts.max())
    bad = alerts[alerts["level"].isin(["WARN", "CRITICAL"])]
    assert bad.empty, bad[["rule", "message"]].to_string()
    print("✓ 정상 자료 오탐 없음")


def test_night_zero_not_flatline():
    """야간 PPFD 0 연속은 고착으로 오판하면 안 된다."""
    ts = pd.date_range("2026-05-01", periods=144 * 3, freq="10min")
    hour = ts.hour + ts.minute / 60
    ppfd = np.clip(np.sin((hour - 6) / 13 * np.pi), 0, None) * 900
    df = pd.DataFrame({"timestamp": ts, "ppfd": ppfd})
    alerts = qc_rules.check_flatline(df, CFG)
    assert not any(a["variable"] == "ppfd" for a in alerts)
    print("✓ 야간 PPFD 0 연속 → 고착 오탐 없음")


def test_health_score():
    grid, _, _, _ = _load_grid()
    h = qc_rules.health_score(grid, CFG, days=7)
    assert not h.empty
    assert (h["수신율"] <= 1.0001).all() and (h["결측률"] >= -0.0001).all()
    print("✓ 센서 상태표")


# ---------------------------------------------------------------------
# 센서 검증
# ---------------------------------------------------------------------
def test_cross_check():
    grid, _, _, _ = _load_grid()
    # 같은 온도열을 자기 자신과 비교하면 편차 0, r=1 → 합격
    res = sensor_check.cross_check(grid.assign(temp_b=grid["temp"]), "temp", "temp_b", "temp", CFG)
    assert res["result"] == "pass" and abs(res["bias"]) < 1e-9 and res["r"] > 0.999

    # 1.5℃ 치우친 센서는 허용오차(0.5℃)를 넘어 불합격
    res2 = sensor_check.cross_check(grid.assign(temp_b=grid["temp"] + 1.5), "temp", "temp_b", "temp", CFG)
    assert res2["result"] == "fail" and abs(res2["bias"] - 1.5) < 1e-6
    print("✓ 센서 상호비교 판정")


def test_stable_replicate_naming():
    """중복 센서 열 번호는 파일의 포트 순서를 그대로 따라야 한다.

    번호가 '살아 있는 센서 우선' 로 재정렬되면 처리구 매핑이 통째로 어긋난다.
    """
    ts = pd.date_range("2026-05-01", periods=200, freq="10min")
    df = pd.DataFrame({
        "timestamp": ts,
        "% Water Content": 0.0,                      # 미연결 포트(전 구간 0)
        "% Water Content.1": np.linspace(30, 33, 200),
        " °C Air Temperature": np.linspace(20, 25, 200),
    })
    std, rep = io_logger.standardize(df)
    assert (std["vwc__rep1"] == 0).all()             # 포트 순서 유지
    assert std["vwc__rep2"].iloc[0] == 30
    # 대표 열은 살아 있는 센서를 가리킨다
    assert std["vwc"].iloc[0] == 30
    assert "죽은 포트" in rep.loc[rep["원본열"] == "% Water Content", "채택"].iloc[0]
    # 센서 열 목록에는 대표 열의 복사본이 중복으로 들어가지 않는다
    assert qc_rules.sensor_columns(std, "vwc") == ["vwc__rep1", "vwc__rep2"]
    assert qc_rules.sensor_columns(std, "temp") == ["temp"]
    print("✓ 중복 센서 열 번호 안정성(포트 순서 유지)")


def test_treatment_split_and_merge():
    """한 로거의 센서가 서로 다른 처리구일 때, 처리구별로 분리·집계·병합된다."""
    ts = pd.date_range("2026-05-01", periods=144 * 14, freq="10min")
    hour = ts.hour + ts.minute / 60
    n = len(ts)
    std = pd.DataFrame({
        "timestamp": ts,
        "ppfd": np.clip(np.sin((hour - 6) / 13 * np.pi), 0, None) * 900,   # 구역 공통
        "vwc__rep1": np.full(n, 25.0), "vwc__rep2": np.full(n, 40.0),
        "soil_temp__rep1": np.full(n, 22.0), "soil_temp__rep2": np.full(n, 24.0),
    })
    entry = {
        "zone": "온실A", "shared": ["ppfd"],
        "treatments": {
            "NI": {"vwc": "vwc__rep1", "soil_temp": "soil_temp__rep1"},
            "NI+SL": {"vwc": "vwc__rep2", "soil_temp": "soil_temp__rep2"},
        },
    }
    frames = sensor_map.split_by_treatment(std, entry)
    assert set(frames) == {"NI", "NI+SL"}
    assert frames["NI"]["vwc"].iloc[0] == 25.0 and frames["NI+SL"]["vwc"].iloc[0] == 40.0
    assert "ppfd" in frames["NI"].columns                  # 공통 변수 복사

    daily = preprocess.to_daily_by_treatment(frames, interval_minutes=10)
    assert set(daily["trt"]) == {"NI", "NI+SL"}
    assert daily.groupby("trt")["vwc_mean"].first().to_dict() == {"NI": 25.0, "NI+SL": 40.0}

    growth = pd.DataFrame({
        "date": pd.to_datetime(["2026-05-07", "2026-05-07", "2026-05-14", "2026-05-14"]),
        "trt": ["NI", "NI+SL", "NI", "NI+SL"],
        "fresh_wt": [10.0, 12.0, 20.0, 24.0],
    })
    iv = preprocess.build_intervals(growth["date"], first_start="2026-05-01")
    env_iv = preprocess.aggregate_intervals_by_treatment(daily, iv)
    assert len(env_iv) == 4                                 # 구간 2 × 처리구 2
    merged = preprocess.match_growth(growth, env_iv, trt_col="trt")
    assert len(merged) == len(growth) and merged["vwc_mean"].notna().all()
    # 각 생육행에 '자기 처리구' 배지환경이 붙었는지
    assert merged.loc[merged["trt"] == "NI", "vwc_mean"].eq(25.0).all()
    assert merged.loc[merged["trt"] == "NI+SL", "vwc_mean"].eq(40.0).all()
    # 공통 광환경은 처리구와 무관하게 동일
    assert merged.groupby("date")["dli_sum"].nunique().eq(1).all()
    print("✓ 처리구별 분리·집계·병합")


def test_pair_divergence_disabled_by_default():
    """반복 센서가 서로 다른 처리구인 현장 설정에서는 편차 경보가 뜨지 않아야 한다."""
    ts = pd.date_range("2026-05-01", periods=300, freq="10min")
    df = pd.DataFrame({"timestamp": ts,
                       "vwc__rep1": np.full(300, 25.0), "vwc__rep2": np.full(300, 45.0)})
    assert qc_rules.check_pair_divergence(df, CFG) == []
    on = {**CFG, "qc": {**CFG["qc"], "pair_divergence_enabled": True}}
    assert len(qc_rules.check_pair_divergence(df, on)) == 1     # 켜면 정상 작동
    print("✓ 중복센서 편차 규칙 기본 비활성")


def test_judge_tolerance():
    assert sensor_check.judge("temp", 0.3, 20.0, CFG)[0] == "pass"
    assert sensor_check.judge("temp", 0.8, 20.0, CFG)[0] == "fail"
    assert sensor_check.judge("ppfd", 40.0, 1000.0, CFG)[0] == "pass"     # 4% → 합격
    assert sensor_check.judge("ppfd", 80.0, 1000.0, CFG)[0] == "fail"     # 8% → 불합격
    print("✓ 허용오차 판정")


def main() -> int:
    if not SAMPLE.exists():
        print("합성 자료가 없습니다. 먼저 실행: python tests/make_sample_data.py")
        return 1
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"✗ {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"✗ {t.__name__} (예외): {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 통과")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
