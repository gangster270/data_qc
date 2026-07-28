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


# ---------------------------------------------------------------------
# 범용 형식 (ZL6 가 아닌 로거)
# ---------------------------------------------------------------------
def test_interval_detection():
    """기록 간격을 자료에서 추정한다(1·5·15·30·60분)."""
    for minutes in (1, 5, 15, 30, 60):
        ts = pd.date_range("2026-05-01", periods=500, freq=f"{minutes}min")
        assert io_logger.detect_interval_minutes(ts) == minutes
    print("✓ 기록 간격 자동 추정")


def test_split_date_time_columns(tmp_dir=None):
    """날짜 열 + 시간 열이 분리된 형식도 한 시각으로 합쳐 읽는다."""
    ts = pd.date_range("2026-06-01", periods=300, freq="1min")
    df = pd.DataFrame({
        "날짜": ts.strftime("%Y-%m-%d"), "시각": ts.strftime("%H:%M:%S"),
        "온도(℃)": np.linspace(20, 25, len(ts)),
        "상대습도(%)": np.linspace(60, 70, len(ts)),
    })
    out, rep = io_logger.prepare_timestamp(df)
    assert rep["duplicate_rows"] == 0            # 날짜만 쓰면 대량 중복이 생긴다
    assert rep["interval_minutes"] == 1
    assert len(out) == len(ts)
    std, _ = io_logger.standardize(out)
    assert {"temp", "rh"} <= set(std.columns)
    print("✓ 날짜/시간 분리 열 결합")


def test_column_alias_matching():
    """로거마다 다른 표기를 같은 변수로 인식한다(공백·대소문자·한글 무관)."""
    cases = {
        "SoilTemp": "soil_temp", "Soil Temperature": "soil_temp", "토양온도": "soil_temp",
        "AirTemp": "temp", "외부온도": "temp", "기온": "temp",
        "RH(%)": "rh", "상대습도": "rh",
        "PAR": "ppfd", "광량": "ppfd",
        "Water Content": "vwc", "토양수분": "vwc", "수분함량": "vwc",
        "일사량": "solar", "CO2": "co2", "탄산가스": "co2",
    }
    for name, expected in cases.items():
        assert io_logger._match_variable(name) == expected, f"{name} → {io_logger._match_variable(name)}"
    # 오인식하면 안 되는 것들
    for name in ("parameter", "Logger Temperature", "Battery Percent", "pH", "풍속"):
        assert io_logger._match_variable(name) is None, name
    print("✓ 변수명 별칭 인식")


def test_unknown_variables_preserved_and_checked():
    """표준 변수가 아닌 열(수온·pH·풍속)도 버리지 않고 집계·감시한다."""
    ts = pd.date_range("2026-05-01", periods=144 * 5, freq="10min")
    df = pd.DataFrame({
        "timestamp": ts,
        "수온": 18 + np.random.default_rng(0).normal(0, 0.3, len(ts)),
        "pH": 6.2 + np.random.default_rng(1).normal(0, 0.05, len(ts)),
    })
    std, rep = io_logger.standardize(df)
    assert {"수온", "ph"} <= set(std.columns)
    assert (rep["채택"].str.contains("기타 변수")).any()

    daily = preprocess.to_daily(std, interval_minutes=10)
    assert {"수온_mean", "수온_min", "수온_max", "ph_mean"} <= set(daily.columns)

    # 임계값이 없는 변수도 고착은 잡는다(12시간 동일값)
    stuck = std.copy()
    stuck.loc[stuck.index[:72 * 2], "수온"] = 18.0
    alerts = qc_rules.run_all(stuck, CFG, lookback_days=10, now=str(ts.max()))
    assert any(a.startswith("R04") for a in alerts["rule"]), alerts["rule"].tolist()
    print("✓ 미인식 변수 보존 · 집계 · 감시")


def test_hourly_logger_end_to_end():
    """1시간 간격 로거도 결측·고착 판정이 시간 기준으로 정확히 동작한다."""
    ts = pd.date_range("2026-05-01", periods=24 * 20, freq="1h")
    hour = ts.hour
    df = pd.DataFrame({
        "datetime": ts.strftime("%Y/%m/%d %H:%M"),
        "AirTemp": 20 + 8 * np.sin((hour - 9) / 24 * 2 * np.pi),
        "PAR": np.clip(np.sin((hour - 6) / 13 * np.pi), 0, None) * 1100,
    })
    # 3시간 기록 누락 + 온도 8시간 고착 주입
    df = df.drop(index=range(100, 103)).reset_index(drop=True)
    df.loc[200:207, "AirTemp"] = 21.5

    out, rep = io_logger.prepare_timestamp(df)
    assert rep["interval_minutes"] == 60
    std, _ = io_logger.standardize(out)
    grid, gaps = io_logger.reindex_full_grid(std)          # 간격 자동
    assert len(gaps) == 1 and int(gaps.iloc[0]["결측시간_분"]) == 180

    daily = preprocess.to_daily(grid.drop(columns=["qc_status"]))
    assert daily["expected_records"].iloc[0] == 24        # 1시간 → 하루 24 레코드
    assert daily["completeness"].max() <= 1.0

    alerts = qc_rules.run_all(grid, CFG, lookback_days=30, now=str(grid["timestamp"].max()))
    rules = set(alerts["rule"])
    assert "R01_timestamp_gap" in rules                    # 3시간 누락
    assert "R04_flatline" in rules                         # 8시간 고착(기준 6시간)
    assert "R00_rule_error" not in rules, alerts[alerts.rule == "R00_rule_error"]["message"].tolist()
    print("✓ 1시간 간격 로거 전 과정")


def test_flat_and_spike_scale_with_interval():
    """고착 기준은 '시간', 급변 기준은 '간격'에 맞춰 환산된다."""
    spec = {"flat_minutes": 360, "spike": 5.0}
    assert qc_rules.flat_count(spec, 10) == 36            # 6시간 = 10분 × 36
    assert qc_rules.flat_count(spec, 1) == 360            # 1분 로거는 360회
    assert qc_rules.flat_count(spec, 60) == 6             # 1시간 로거는 6회
    assert qc_rules.spike_threshold(spec, 10) == 5.0
    assert qc_rules.spike_threshold(spec, 1) == 0.5       # 1분당 허용 변화는 1/10
    assert qc_rules.spike_threshold({"spike": None}, 10) is None
    print("✓ 간격에 따른 고착·급변 기준 환산")


# ---------------------------------------------------------------------
# 조사일 기준 직접 지정 · 통합 아카이브
# ---------------------------------------------------------------------
def test_parse_survey_dates():
    """조사일을 사용자가 세 가지 방식으로 지정할 수 있다."""
    # (a) 직접 나열 — 불규칙해도 그대로
    d = preprocess.parse_survey_dates(dates="2026-04-01, 2026-04-11,2026-04-18")
    assert [x.strftime("%m-%d") for x in d] == ["04-01", "04-11", "04-18"]
    # 리스트·공백·중복·역순도 정리된다
    d2 = preprocess.parse_survey_dates(dates=["2026-04-11", "2026-04-01", "2026-04-11"])
    assert len(d2) == 2 and d2[0] < d2[1]

    # (b) 시작일 + 간격 + 횟수
    d3 = preprocess.parse_survey_dates(start="2026-04-01", interval=10, count=4)
    assert len(d3) == 4 and (d3[1] - d3[0]).days == 10
    # (c) 시작일 + 간격 + 종료일
    d4 = preprocess.parse_survey_dates(start="2026-04-01", interval=7, end="2026-04-29")
    assert [x.strftime("%m-%d") for x in d4] == ["04-01", "04-08", "04-15", "04-22", "04-29"]

    for bad in (dict(), dict(start="2026-04-01"), dict(dates="어제")):
        try:
            preprocess.parse_survey_dates(**bad)
            raise AssertionError(f"오류가 나야 함: {bad}")
        except ValueError:
            pass

    # 지정한 조사일이 그대로 구간이 된다
    iv = preprocess.build_intervals(pd.Series(d), first_start="2026-03-25")
    assert iv["days_expected"].tolist() == [8, 10, 7]
    print("✓ 조사일 사용자 지정(직접·시작일+간격·종료일)")


def _write_logger_file(path: Path, start: str, periods: int, freq: str, temp0=20.0):
    ts = pd.date_range(start, periods=periods, freq=freq)
    pd.DataFrame({
        "Timestamp": ts,
        " °C Air Temperature": np.linspace(temp0, temp0 + 5, periods).round(2),
        "% Relative Humidity": np.linspace(60, 70, periods).round(1),
    }).to_excel(path, index=False)


def test_archive_build_merge_and_update(tmp_path=None):
    """여러 로거·기간의 파일을 날짜순으로 모으고, 새 파일은 이어붙인다."""
    import tempfile
    from src import archive

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        _write_logger_file(tmp / "logA_1.xlsx", "2026-05-01", 144, "10min")
        _write_logger_file(tmp / "logB_1.xlsx", "2026-05-01", 24, "1h", temp0=15.0)
        out = tmp / "archive"

        res = archive.build_archive([str(tmp / "logA_1.xlsx"), str(tmp / "logB_1.xlsx")],
                                    CFG, out, update=False, registry_path=tmp / "registry.yaml")
        master = res["master"]
        assert set(master["logger"]) == {"logA", "logB"}
        assert len(master) == 144 + 24
        # 로거·시각 순 정렬
        assert master.sort_values(["logger", "timestamp"]).equals(master)
        assert {"temp", "rh"} <= set(master.columns)
        # 로거마다 기록 간격이 달라도 각각 맞게 격자 정합된다
        assert res["summary"].set_index("구역").loc["logB", "기록간격(분)"] == 60

        # 같은 파일을 다시 넣어도 행이 늘지 않는다(중복 병합)
        res2 = archive.build_archive([str(tmp / "logA_1.xlsx")], CFG, out, update=True,
                                     registry_path=tmp / "registry.yaml")
        assert len(res2["master"]) == len(master)

        # 이어지는 기간의 새 파일은 추가된다
        _write_logger_file(tmp / "logA_2.xlsx", "2026-05-02", 144, "10min")
        res3 = archive.build_archive([str(tmp / "logA_2.xlsx")], CFG, out, update=True,
                                     registry_path=tmp / "registry.yaml")
        assert len(res3["master"]) == len(master) + 144
        assert res3["master"]["timestamp"].max().strftime("%m-%d") == "05-02"

        # 저장·재로딩이 되고, 로거별 순회가 동작한다
        reloaded = archive.load_master(out)
        assert len(reloaded) == len(res3["master"])
        ids = [lid for lid, _ in archive.iter_loggers(reloaded)]
        assert ids == ["logA", "logB"]
    print("✓ 통합 아카이브 생성·중복병합·증분 업데이트")


def test_archive_mixed_intervals_daily():
    """아카이브에 간격이 다른 로거가 섞여 있어도 각각 올바르게 일별 집계된다."""
    import tempfile
    from src import archive

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        _write_logger_file(tmp / "tenmin_1.xlsx", "2026-05-01", 144 * 2, "10min")
        _write_logger_file(tmp / "hourly_1.xlsx", "2026-05-01", 24 * 2, "1h")
        res = archive.build_archive([str(tmp / "tenmin_1.xlsx"), str(tmp / "hourly_1.xlsx")],
                                    CFG, tmp / "arc", update=False, registry_path=tmp / "registry.yaml")
        expected = {}
        for logger_id, df in archive.iter_loggers(res["master"]):
            daily = preprocess.to_daily(df)                    # 간격 자동 추정
            expected[logger_id] = int(daily["expected_records"].iloc[0])
        assert expected == {"tenmin": 144, "hourly": 24}
    print("✓ 간격이 다른 로거 혼재 아카이브")


# ---------------------------------------------------------------------
# 로거 번호 인식 · 구역 묶기 · 다중 시트
# ---------------------------------------------------------------------
def test_serial_detection_from_filename():
    """파일명 앞에 내려받은 날짜가 붙어도 로거 번호를 골라낸다."""
    cases = {
        "260703_22094002.csv": "22094002",          # 날짜_번호
        "20260703_22094002.csv": "22094002",
        "a1b2c3d4-260703_22061061.csv": "22061061",  # 업로드 해시 접두
        "z6-03959_086260946.xlsx": "z6-03959",
    }
    for name, expected in cases.items():
        assert io_logger.serial_from_filename(name) == expected, name
    # 날짜 토큰만 있으면 파일명 전체를 식별자로
    assert io_logger.serial_from_filename("260703.csv") == "260703"     # 날짜뿐이면 파일명 그대로
    print("✓ 파일명에서 로거 번호 인식(날짜 접두 무시)")


def test_serial_detection_from_content():
    """파일 안의 표기(HOBO 차트 제목·LGR S/N, ZL6 헤더)에서 번호를 찾는다."""
    assert io_logger.detect_serial_from_text('"차트 제목: 22094002"') == "22094002"
    assert io_logger.detect_serial_from_text("Plot Title: 22094002") == "22094002"
    assert io_logger.detect_serial_from_text("PAR (LGR S/N: 22070169, SEN S/N: 1)") == "22070169"
    assert io_logger.detect_serial_from_text("z6-03959 Port 1 Records: 100") == "z6-03959"
    assert io_logger.detect_serial_from_text("수경재배 모니터") is None      # 임의 제목은 무시
    print("✓ 파일 내용에서 로거 번호 인식")


def test_korean_hobo_datetime():
    """HOBO 한국어 시각(`05. 19. 25 오후 05시 30분 01초`)을 파싱한다."""
    s = pd.Series(["05. 19. 25 오후 05시 30분 01초",
                   "05. 19. 25 오후 05시 40분 01초",
                   "05. 20. 25 오전 09시 00분 01초"])
    out = io_logger.parse_datetime_series(s)
    assert out.notna().all()
    assert out.iloc[0].strftime("%Y-%m-%d %H:%M") == "2025-05-19 17:30"
    assert out.iloc[2].strftime("%Y-%m-%d %H:%M") == "2025-05-20 09:00"
    print("✓ HOBO 한국어 시각 파싱")


def test_multi_sheet_excel(tmp_path=None):
    """센서 구성이 바뀌어 시트가 나뉜 엑셀도 전부 읽어 합친다."""
    import tempfile
    ts1 = pd.date_range("2026-05-01", periods=144, freq="10min")
    ts2 = pd.date_range("2026-05-02", periods=144, freq="10min")
    cfg1 = pd.DataFrame([["z6-99999", "Port 1", "Port 2"],
                         ["Records: 144", "SQ-521", "ATMOS 14"],
                         ["Timestamp", " µmol PPFD", " °C Air Temperature"]] +
                        [[t, 100.0, 20.0] for t in ts1])
    # Config 2 에서 포트 구성이 바뀜(PPFD 제거, TEROS 추가)
    cfg2 = pd.DataFrame([["z6-99999", "Port 2", "Port 3"],
                         ["Records: 144", "ATMOS 14", "TEROS 12"],
                         ["Timestamp", " °C Air Temperature", "% Water Content"]] +
                        [[t, 25.0, 30.0] for t in ts2])
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "z6-99999_260703.xlsx"
        with pd.ExcelWriter(path) as w:
            cfg1.to_excel(w, sheet_name="Processed Data Config 1", index=False, header=False)
            cfg2.to_excel(w, sheet_name="Processed Data Config 2", index=False, header=False)
            cfg1.to_excel(w, sheet_name="Raw Data Config 1", index=False, header=False)
            pd.DataFrame([["meta"]]).to_excel(w, sheet_name="Metadata", index=False, header=False)

        assert io_logger.pick_sheets(["Processed Data Config 1", "Processed Data Config 2",
                                      "Raw Data Config 1", "Metadata"]) == \
            ["Processed Data Config 1", "Processed Data Config 2"]

        df = io_logger.read_env_file(path)
        assert len(df) == 288                        # 두 Config 가 모두 들어옴(Raw 중복 없음)
        out, rep = io_logger.prepare_timestamp(df)
        std, _ = io_logger.standardize(out)
        # 같은 포트(Port 2 기온)는 한 열로 이어지고, 구성이 다른 변수는 각자 남는다
        assert std["temp"].notna().sum() == 288
        assert std["ppfd"].notna().sum() == 144
        assert std["vwc"].notna().sum() == 144
        assert rep["duplicate_rows"] == 0
    print("✓ 다중 시트(Config 1·2) 병합")


def test_zone_grouping_and_memory():
    """같은 번호는 계속 같은 구역으로, 한 구역의 로거 여러 대는 한 자료로 묶인다."""
    import tempfile
    from src import archive, registry

    def _hobo(path, serial, start, periods=144):
        ts = pd.date_range(start, periods=periods, freq="10min") + pd.Timedelta(seconds=1)
        pd.DataFrame({"Timestamp": ts,
                      f"PAR, µmol/m²/s (LGR S/N: {serial})": np.linspace(0, 900, periods)}
                     ).to_csv(path, index=False, encoding="utf-8-sig")

    def _zl6(path, serial, start, periods=144):
        ts = pd.date_range(start, periods=periods, freq="10min")
        pd.DataFrame({"Timestamp": ts,
                      " °C Air Temperature": np.linspace(18, 25, periods),
                      "% Relative Humidity": np.linspace(60, 70, periods)}
                     ).to_excel(path, index=False)
        return serial

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        reg_path = tmp / "registry.yaml"
        _hobo(tmp / "260703_22094002.csv", "22094002", "2026-05-01")
        _zl6(tmp / "z6-11111_260703.xlsx", "z6-11111", "2026-05-01")

        cfgd = {**CFG, "sensors": CFG["sensors"]}
        reg = registry.load_registry(reg_path)
        registry.set_zone(reg, "22094002", "3구역")
        registry.set_zone(reg, "z6-11111", "3구역")     # 같은 구역에 로거 2대
        registry.save_registry(reg, reg_path)

        res = archive.build_archive([str(tmp / "260703_22094002.csv"), str(tmp / "z6-11111_260703.xlsx")],
                                    cfgd, tmp / "arc", update=False, registry_path=reg_path)
        master = res["master"]
        assert set(master["logger"]) == {"3구역"}          # 두 로거가 한 구역으로
        assert len(master) == 144                          # 시각이 격자에 맞춰져 한 행으로 병합
        assert {"ppfd", "temp", "rh"} <= set(master.columns)
        assert master["serial"].str.contains(r"\+").all()  # 한 행에 두 로거가 기여

        # 다음 업로드(파일명이 달라도) 같은 번호 → 같은 구역, 기존 자료에 이어붙음
        _hobo(tmp / "260710_22094002.csv", "22094002", "2026-05-02")
        res2 = archive.build_archive([str(tmp / "260710_22094002.csv")], cfgd, tmp / "arc",
                                     update=True, registry_path=reg_path)
        assert set(res2["master"]["logger"]) == {"3구역"}
        assert len(res2["master"]) == 288
        print("✓ 구역 이름 기억 · 로거 여러 대 한 구역 병합")


def test_zone_column_collision():
    """한 구역의 두 로거가 같은 변수를 재면 둘 다 보존한다(__rep 로 분리)."""
    from src import archive
    ts = pd.date_range("2026-05-01", periods=10, freq="10min")
    a = pd.DataFrame({"logger": "3구역", "serial": "A", "timestamp": ts, "ppfd": 100.0})
    b = pd.DataFrame({"logger": "3구역", "serial": "B", "timestamp": ts,
                      "ppfd": 200.0, "ppfd__rep1": 210.0, "ppfd__rep2": 220.0})
    frames, notes = archive.resolve_zone_collisions([a, b])
    merged = archive.merge_frames(frames)
    assert len(merged) == 10                                  # 시각 기준 한 행
    assert merged["ppfd"].eq(100.0).all()                     # 먼저 온 로거가 기본 이름 유지
    assert (merged["ppfd__rep3"] == 200.0).all()              # 뒤 로거는 새 이름으로 보존
    assert not notes.empty
    print("✓ 구역 내 동일 변수 열 분리 보존")


def test_weekly_store_accumulates():
    """매주 올린 파일이 보관함에 쌓이고, 같은 파일은 다시 넣지 않는다."""
    import tempfile
    from src import archive, store

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        reg_path = tmp / "registry.yaml"
        week1 = tmp / "z6-55555_wk1.xlsx"
        _write_logger_file(week1, "2026-05-01", 144, "10min")
        st_dir = tmp / "store"

        r1 = store.add_files([str(week1)], CFG, st_dir, registry_path=reg_path)
        assert (r1["uploads"]["상태"] == "새로 보관").all()
        assert r1["before"] == 0 and r1["after"] == 144
        assert len(store.stored_files(st_dir)) == 1

        # 같은 파일을 다시 올려도 보관·행수 모두 그대로
        r2 = store.add_files([str(week1)], CFG, st_dir, registry_path=reg_path)
        assert (r2["uploads"]["상태"] == "이미 있음").all()
        assert r2["added"] == 0 and r2["after"] == 144
        assert len(store.stored_files(st_dir)) == 1

        # 숫자만으로 된 로거번호(HOBO)도 저장·재로딩 후 같은 구역으로 인식되어
        # 다시 올렸을 때 행이 늘지 않는다
        hobo = tmp / "260703_22094002.csv"
        ts = pd.date_range("2026-05-01", periods=144, freq="10min")
        pd.DataFrame({"Timestamp": ts,
                      "PAR, µmol/m²/s (LGR S/N: 22094002)": np.linspace(0, 900, 144)}
                     ).to_csv(hobo, index=False, encoding="utf-8-sig")
        h1 = store.add_files([str(hobo)], CFG, st_dir, registry_path=reg_path)
        rows_with_hobo = h1["after"]
        # 다음 주에 '처음부터 전부' 다시 내려받아 올린다 — 겹치는 144행은 늘면 안 되고
        # 새로 늘어난 144행만 추가돼야 한다
        hobo2 = tmp / "260710_22094002.csv"
        ts2 = pd.date_range("2026-05-01", periods=288, freq="10min")
        pd.DataFrame({"Timestamp": ts2,
                      "PAR, µmol/m²/s (LGR S/N: 22094002)": np.linspace(0, 900, 288)}
                     ).to_csv(hobo2, index=False, encoding="utf-8-sig")
        h2 = store.add_files([str(hobo2)], CFG, st_dir, registry_path=reg_path)
        assert h2["added"] == 144, h2["added"]
        assert h2["master"].duplicated(["logger", "timestamp"]).sum() == 0
        rows_with_hobo = h2["after"]

        # 다음 주 자료는 이어붙는다
        week2 = tmp / "z6-55555_wk2.xlsx"
        _write_logger_file(week2, "2026-05-02", 144, "10min")
        r3 = store.add_files([str(week2)], CFG, st_dir, registry_path=reg_path)
        assert r3["added"] == 144 and r3["after"] == rows_with_hobo + 144
        assert len(store.load_upload_log(st_dir)) == 4, len(store.load_upload_log(st_dir))

        # 원본이 남아 있으므로 전체 재통합도 같은 결과
        r4 = store.add_files([], CFG, st_dir, registry_path=reg_path, rebuild=True)
        assert r4["rebuilt"] and len(r4["master"]) == rows_with_hobo + 144

        # 보관함에 없는 로거가 마스터에 있으면 재통합을 거부한다(자료 소실 방지)
        outside = tmp / "z6-99999_direct.xlsx"
        _write_logger_file(outside, "2026-05-03", 144, "10min")
        archive.build_archive([str(outside)], CFG, st_dir, update=True, registry_path=reg_path)
        r5 = store.add_files([], CFG, st_dir, registry_path=reg_path, rebuild=True)
        assert not r5["rebuilt"] and len(r5["master"]) == rows_with_hobo + 288  # 그대로 보존
        assert "z6-99999" in r5["log"][0]

        # 회차별 결과 보관: 저장 → 목록 → 파일 확인
        iv = pd.DataFrame({"interval_id": [1, 2], "temp_mean": [20.0, 21.0]})
        res_dir = tmp / "results"
        f1 = store.save_result({"구간환경": iv}, res_dir, label="1주차")
        f2 = store.save_result({"구간환경": iv}, res_dir, label="2주차")
        runs = store.list_results(res_dir)
        assert len(runs) == 2 and runs.iloc[0]["이름"].endswith("2주차")   # 최근이 위
        assert (f1 / "구간환경.csv").exists() and (f2 / "전체결과.xlsx").exists()
        assert len(store.zip_result(f1)) > 0
    print("✓ 주간 보관함 누적 · 중복 방지 · 회차 결과 보관")


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
