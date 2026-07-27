"""10분 환경데이터 → 생육조사 구간(7일·10일) 시차 매칭 전처리.

수작업으로 하던 "10분 자료를 생육조사 간격에 맞춰 평균내는" 작업을 코드화한 모듈.

설계 원칙 (agri-env-growth-match 표준)
-------------------------------------
1) 생육은 **구간 누적 반응**이다. 측정일 하루의 환경이 아니라
   '직전 측정일 다음날 ~ 당일' 구간 전체를 집계해 매칭한다.
2) 2단계 집계: 10분 → 일별(daily) → 생육 구간(interval).
   중간 일별 파일을 반드시 남겨야 부분일·결측 영향을 추적할 수 있다.
3) 변수마다 물리적으로 올바른 집계가 다르다.
   기온=평균/최저/최고, PPFD=일적산(DLI), 습도·VPD·CO2=평균, GDD=누적.
4) 측정 간격(7/10일)은 하드코딩하지 않고 실제 조사일에서 자동 추정하며,
   불규칙 간격(7↔10일 혼재)도 그대로 처리한다.
5) 시차(lag)는 옵션으로 지정한다. lag_days=3 이면 각 구간을 3일 앞당겨
   "조사일 3일 전까지의 환경"과 매칭한다(환경 효과의 지연 반응 검토용).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# 일별 집계에서 '평균 성격' 변수와 '적산 성격' 변수를 구분한다.
MEAN_VARS = ["temp", "rh", "soil_temp", "vwc", "ec", "co2", "vpd"]
SUM_VARS = ["dli", "gdd", "solar_MJ"]      # 구간에서는 합계(누적)가 의미를 가짐

LABELS = {
    "temp": "온도", "rh": "습도", "soil_temp": "배지온도", "vwc": "배지습도",
    "ppfd": "PPFD", "solar": "일사량", "ec": "EC", "co2": "CO2", "vpd": "VPD",
    "dli": "DLI", "gdd": "적산온도",
}


# ---------------------------------------------------------------------
# 파생 변수
# ---------------------------------------------------------------------
def compute_vpd(temp_c: pd.Series, rh_pct: pd.Series) -> pd.Series:
    """VPD(kPa) = es(T) × (1 − RH/100), es 는 Tetens 식.

    반드시 10분 원자료에서 계산한 뒤 평균낸다. 일평균 온도·습도로 뒤늦게
    계산하면 Jensen 부등식 때문에 실제보다 낮게 나온다.
    """
    es = 0.6108 * np.exp(17.27 * temp_c / (temp_c + 237.3))
    return es * (1.0 - rh_pct / 100.0)


def add_derived(df10: pd.DataFrame) -> pd.DataFrame:
    """10분 자료에 VPD 등 파생변수를 추가한다."""
    out = df10.copy()
    if "temp" in out.columns and "rh" in out.columns:
        out["vpd"] = compute_vpd(out["temp"], out["rh"])
    return out


def mask_out_of_range(df10: pd.DataFrame, sensors: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """물리적으로 불가능한 값(-99.9 등)을 결측 처리한다.

    집계 전에 반드시 거쳐야 하는 단계. 오류값 하나가 일최저·일평균을 통째로
    망가뜨리므로, 값을 지우고 '몇 건을 지웠는지'를 리포트로 남긴다.
    """
    out = df10.copy()
    rows = []
    for var, spec in (sensors or {}).items():
        if var not in out.columns:
            continue
        s = pd.to_numeric(out[var], errors="coerce")
        bad = s.notna() & ((s < spec.get("min", -np.inf)) | (s > spec.get("max", np.inf)))
        n_bad = int(bad.sum())
        if n_bad:
            out.loc[bad, var] = np.nan
            rows.append({
                "변수": spec.get("label", var), "키": var,
                "허용범위": f"{spec.get('min')} ~ {spec.get('max')}{spec.get('unit', '')}",
                "결측처리건수": n_bad,
                "비율": round(n_bad / len(out), 5),
            })
    return out, pd.DataFrame(rows)


# ---------------------------------------------------------------------
# Step 1. 10분 → 일별 요약
# ---------------------------------------------------------------------
def to_daily(
    df10: pd.DataFrame,
    interval_minutes: float | None = None,
    gdd_base: float = 10.0,
    photoperiod_ppfd_threshold: float = 10.0,
    daytime_hours: tuple[int, int] = (9, 15),
    min_completeness: float = 0.90,
    include_replicates: bool = False,
) -> pd.DataFrame:
    """환경자료(임의 기록간격)를 일별로 요약한다.

    interval_minutes 를 주지 않으면 자료에서 자동 추정한다(1·5·10·15·30·60분 등).
    표준 변수가 아닌 열(CO2·풍속·수온 등)도 평균/최저/최고로 함께 요약한다.

    산출 열
      date, n_records, expected_records, completeness, is_complete
      temp_mean/min/max/amp, rh_mean/min, vpd_mean, vpd_day (주간평균)
      soil_temp_mean/min/max, vwc_mean/min/max, ec_mean, co2_mean
      ppfd_day_mean(광주기 중 평균), photoperiod_h, dli(mol m-2 d-1)
      solar_MJ(MJ m-2 d-1), gdd(일 적산온도)
      <var>_missing_ratio (변수별 결측률)
    """
    if df10.empty:
        return pd.DataFrame()

    if interval_minutes is None:
        from .io_logger import detect_interval_minutes
        interval_minutes = detect_interval_minutes(df10["timestamp"])

    df = add_derived(df10)
    df = df.copy()
    df["date"] = pd.to_datetime(df["timestamp"]).dt.date
    df["hour"] = pd.to_datetime(df["timestamp"]).dt.hour
    expected = max(int(round(24 * 60 / interval_minutes)), 1)   # 10분 → 144, 1시간 → 24
    seconds = interval_minutes * 60

    # 표준 변수가 아닌 수치 열(CO2·풍속·수온·pH 등)도 요약 대상에 넣는다
    known = set(MEAN_VARS) | {"ppfd", "solar", "timestamp", "date", "hour", "qc_status", "_source_file"}
    extra_vars = [c for c in df.columns
                  if c not in known and pd.api.types.is_numeric_dtype(df[c])
                  and (include_replicates or "__rep" not in c)]

    recs = []
    for date, g in df.groupby("date", sort=True):
        row: dict = {"date": pd.Timestamp(date)}

        # --- 관측 완전성 -------------------------------------------------
        # 로거가 기록한 행 중 '실제 값이 있는' 행 기준. 어느 변수도 없는 행은 제외.
        value_cols = [c for c in df.columns
                      if c not in ("timestamp", "date", "hour", "qc_status", "_source_file")]
        has_any = g[value_cols].notna().any(axis=1) if value_cols else pd.Series(False, index=g.index)
        row["n_records"] = int(has_any.sum())
        row["expected_records"] = expected
        row["completeness"] = round(row["n_records"] / expected, 4)
        row["is_complete"] = bool(row["completeness"] >= min_completeness)

        # --- 온도: 평균 / 최저 / 최고 / 일교차 ---------------------------
        if "temp" in g:
            t = g["temp"].dropna()
            if len(t):
                row["temp_mean"] = t.mean()
                row["temp_min"] = t.min()
                row["temp_max"] = t.max()
                row["temp_amp"] = t.max() - t.min()          # 일교차
                # 적산온도(GDD): 일평균 기준. 음수는 0으로 절단.
                row["gdd"] = max(t.mean() - gdd_base, 0.0)

        # --- 습도 / VPD ---------------------------------------------------
        if "rh" in g:
            r = g["rh"].dropna()
            if len(r):
                row["rh_mean"] = r.mean()
                row["rh_min"] = r.min()
                row["rh_max"] = r.max()
        if "vpd" in g:
            v = g["vpd"].dropna()
            if len(v):
                row["vpd_mean"] = v.mean()
                day = g.loc[(g["hour"] >= daytime_hours[0]) & (g["hour"] < daytime_hours[1]), "vpd"].dropna()
                if len(day):
                    row["vpd_day"] = day.mean()              # 증산이 실제 일어나는 주간 VPD

        # --- 배지온도 / 배지습도 / EC / CO2 -------------------------------
        for var, aggs in (("soil_temp", ("mean", "min", "max")),
                          ("vwc", ("mean", "min", "max")),
                          ("ec", ("mean",)),
                          ("co2", ("mean",))):
            if var in g:
                s = g[var].dropna()
                if len(s):
                    for a in aggs:
                        row[f"{var}_{a}"] = getattr(s, a)()

        # --- 광: DLI(일적산) / 광주기 / 주간 평균 PPFD --------------------
        if "ppfd" in g:
            p = g["ppfd"].dropna()
            if len(p):
                row["dli"] = float(p.sum() * seconds / 1e6)   # mol m-2 d-1
                lit = p[p > photoperiod_ppfd_threshold]
                row["photoperiod_h"] = round(len(lit) * interval_minutes / 60, 2)
                row["ppfd_day_mean"] = float(lit.mean()) if len(lit) else 0.0
                row["ppfd_max"] = float(p.max())

        # --- 일사량: 일적산 (MJ m-2 d-1) ----------------------------------
        if "solar" in g:
            s = g["solar"].dropna()
            if len(s):
                row["solar_MJ"] = float(s.sum() * seconds / 1e6)
                row["solar_max"] = float(s.max())

        # --- 표준 변수가 아닌 열: 평균/최저/최고 ---------------------------
        for var in extra_vars:
            s = g[var].dropna()
            if len(s):
                row[f"{var}_mean"] = float(s.mean())
                row[f"{var}_min"] = float(s.min())
                row[f"{var}_max"] = float(s.max())

        # --- 변수별 결측률 -------------------------------------------------
        for var in list(("temp", "rh", "soil_temp", "vwc", "ppfd", "solar", "ec", "co2")) + extra_vars:
            if var in g:
                row[f"{var}_missing_ratio"] = round(1 - g[var].notna().sum() / expected, 4)

        recs.append(row)

    daily = pd.DataFrame(recs).sort_values("date").reset_index(drop=True)
    return daily


# ---------------------------------------------------------------------
# Step 2. 생육 조사일 → 구간 정의 (시차 매칭의 핵심)
# ---------------------------------------------------------------------
def parse_survey_dates(dates=None, start=None, end=None, interval=None, count=None) -> list[pd.Timestamp]:
    """생육 조사일 목록을 만든다 — **사용자가 기준을 직접 정할 때 쓰는 진입점**.

    세 가지 방식 중 하나를 쓴다.
      (a) 조사일 직접 나열 : dates="2026-04-01, 2026-04-11, 2026-04-21" (리스트도 가능)
      (b) 시작일 + 간격 + 횟수 : start="2026-04-01", interval=10, count=6
      (c) 시작일 + 간격 + 종료일 : start="2026-04-01", interval=7, end="2026-06-08"

    (a) 는 실제 조사일이 불규칙할 때(현장 사정으로 하루씩 밀린 경우) 그대로 반영된다.
    반환: 오름차순 정렬된 중복 없는 Timestamp 목록.
    """
    out: list[pd.Timestamp] = []

    if dates is not None and (not isinstance(dates, str) or dates.strip()):
        items = dates.replace("\n", ",").split(",") if isinstance(dates, str) else list(dates)
        for d in items:
            d = str(d).strip()
            if not d:
                continue
            ts = pd.to_datetime(d, errors="coerce")
            if pd.isna(ts):
                raise ValueError(f"조사일을 해석할 수 없습니다: '{d}' (예: 2026-04-01)")
            out.append(pd.Timestamp(ts).normalize())

    elif start is not None and interval:
        s = pd.to_datetime(start, errors="coerce")
        if pd.isna(s):
            raise ValueError(f"시작일을 해석할 수 없습니다: '{start}'")
        step = int(interval)
        if step <= 0:
            raise ValueError("조사 간격은 1일 이상이어야 합니다.")
        if count:
            out = [pd.Timestamp(s).normalize() + pd.Timedelta(days=step * i) for i in range(int(count))]
        elif end is not None:
            e = pd.to_datetime(end, errors="coerce")
            if pd.isna(e):
                raise ValueError(f"종료일을 해석할 수 없습니다: '{end}'")
            cur, e = pd.Timestamp(s).normalize(), pd.Timestamp(e).normalize()
            while cur <= e:
                out.append(cur)
                cur = cur + pd.Timedelta(days=step)
        else:
            raise ValueError("시작일·간격만으로는 부족합니다. 횟수(count) 또는 종료일(end)을 지정하세요.")
    else:
        raise ValueError("조사일을 정하려면 '조사일 목록' 또는 '시작일+간격+(횟수|종료일)'이 필요합니다.")

    return sorted(set(out))


def detect_cadence(dates: pd.Series) -> int:
    """조사일 간격(7 또는 10일 등)을 데이터에서 자동 추정한다(최빈 간격)."""
    d = pd.to_datetime(pd.Series(sorted(pd.unique(dates))))
    if len(d) < 2:
        return 0
    diffs = d.diff().dropna().dt.days.astype(int)
    return int(diffs.mode().iloc[0])


def build_intervals(
    growth_dates,
    first_start=None,
    lag_days: int = 0,
    window_days: int | None = None,
) -> pd.DataFrame:
    """각 생육 조사일에 매칭할 환경 구간(start~end)을 만든다.

    기본(window_days=None): 구간 = [직전 조사일 + 1일, 당일]  ← 가변 구간
    고정창(window_days=N):  구간 = [당일 − N + 1, 당일]        ← 항상 N일

    lag_days: 시차. 구간 전체를 N일 앞당긴다.
        예) 조사일 6/20, 직전 조사일 6/10, lag_days=3
            → 구간 6/8 ~ 6/17 (조사 3일 전까지의 환경이 생육에 반영됐다고 가정)

    first_start: 첫 조사일 구간의 시작일(정식일 등). 없으면 첫 조사일에서
        추정 간격만큼 거슬러 올라간다.
    """
    dates = pd.to_datetime(pd.Series(sorted(pd.unique(pd.to_datetime(growth_dates)))))
    if dates.empty:
        return pd.DataFrame(columns=["interval_id", "start", "end", "days_expected", "lag_days"])

    cadence = detect_cadence(dates) or (window_days or 7)
    rows = []
    for i, end in enumerate(dates):
        if window_days:
            start = end - pd.Timedelta(days=window_days - 1)
        elif i == 0:
            if first_start is not None:
                start = pd.Timestamp(first_start)
            else:
                # 첫 구간은 직전 조사일이 없으므로 추정 간격만큼 소급
                start = end - pd.Timedelta(days=cadence - 1)
        else:
            start = dates.iloc[i - 1] + pd.Timedelta(days=1)

        # 시차 적용: 구간 전체를 lag_days 만큼 과거로 이동
        start_l = start - pd.Timedelta(days=lag_days)
        end_l = end - pd.Timedelta(days=lag_days)
        rows.append({
            "interval_id": i + 1,
            "growth_date": end,               # 생육 조사일(매칭 키)
            "start": start_l,                 # 환경 구간 시작
            "end": end_l,                     # 환경 구간 종료
            "days_expected": int((end_l - start_l).days) + 1,
            "lag_days": lag_days,
        })
    return pd.DataFrame(rows)


def aggregate_intervals(daily: pd.DataFrame, intervals: pd.DataFrame,
                        drop_incomplete_days: bool = False) -> pd.DataFrame:
    """일별 요약을 생육 구간 단위로 다시 집계한다.

    평균 성격 변수 → 구간 평균 (+ 최저의 최저, 최고의 최고)
    적산 성격 변수 → 구간 합계 + 일평균 (DLI, GDD, 일사량)
    누적 변수      → 시험 시작부터의 누적 (cum_dli, cum_gdd)
    """
    if daily.empty or intervals.empty:
        return pd.DataFrame()

    d = daily.copy()
    d["date"] = pd.to_datetime(d["date"])
    if drop_incomplete_days and "is_complete" in d.columns:
        d = d[d["is_complete"]]

    recs = []
    for _, iv in intervals.iterrows():
        sub = d[(d["date"] >= iv["start"]) & (d["date"] <= iv["end"])]
        row = {
            "interval_id": iv["interval_id"],
            "growth_date": iv["growth_date"],
            "env_start": iv["start"],
            "env_end": iv["end"],
            "lag_days": iv["lag_days"],
            "days_expected": iv["days_expected"],
            "days_used": len(sub),
        }
        row["day_coverage"] = round(len(sub) / iv["days_expected"], 3) if iv["days_expected"] else np.nan
        if "completeness" in sub.columns and len(sub):
            row["record_completeness"] = round(float(sub["completeness"].mean()), 3)
            row["n_incomplete_days"] = int((~sub["is_complete"]).sum()) if "is_complete" in sub else 0

        if len(sub) == 0:
            recs.append(row)
            continue

        skip = {"date", "n_records", "expected_records", "completeness", "is_complete"}
        for col in sub.columns:
            if col in skip or not pd.api.types.is_numeric_dtype(sub[col]):
                continue

            # --- 적산 성격: 합계 + 일평균 (DLI·GDD·일사 적산) -------------
            if col in SUM_VARS:
                row[f"{col}_sum"] = float(sub[col].sum())
                row[f"{col}_mean"] = float(sub[col].mean())
            # --- 결측률: 구간 평균 ---------------------------------------
            elif col.endswith("_missing_ratio"):
                row[col] = round(float(sub[col].mean()), 4)
            # --- 일최저: 구간 최저 + 일최저의 평균 ------------------------
            elif col.endswith("_min"):
                row[col] = float(sub[col].min())
                row[f"{col}_mean"] = float(sub[col].mean())
            # --- 일최고: 구간 최고 + 일최고의 평균 ------------------------
            elif col.endswith("_max"):
                row[col] = float(sub[col].max())
                row[f"{col}_mean"] = float(sub[col].mean())
            # --- 그 밖(평균 성격): 구간 평균 -------------------------------
            else:
                name = col if col.endswith("_mean") or col in ("photoperiod_h", "vpd_day") \
                    else f"{col}_mean"
                row[name] = float(sub[col].mean())

        recs.append(row)

    out = pd.DataFrame(recs).sort_values("interval_id").reset_index(drop=True)

    if "temp_amp_mean" not in out.columns and "temp_amp" in out.columns:
        out = out.rename(columns={"temp_amp": "temp_amp_mean"})      # 평균 일교차

    # --- 시험 시작부터의 누적값 ------------------------------------------
    for col, cum in (("dli_sum", "cum_dli"), ("gdd_sum", "cum_gdd"), ("solar_MJ_sum", "cum_solar_MJ")):
        if col in out.columns:
            out[cum] = out[col].fillna(0).cumsum()

    # --- 품질 플래그 -------------------------------------------------------
    def _flag(r):
        notes = []
        if r.get("days_used", 0) == 0:
            notes.append("환경자료 없음")
        elif r.get("day_coverage", 1) < 0.9:
            notes.append(f"일수부족({r['days_used']}/{r['days_expected']}일)")
        if r.get("record_completeness", 1) < 0.9:
            notes.append(f"레코드결측(평균완전성 {r.get('record_completeness')})")
        return "; ".join(notes) if notes else "정상"
    out["quality_flag"] = out.apply(_flag, axis=1)
    return out


# ---------------------------------------------------------------------
# Step 3. 생육 데이터와 병합
# ---------------------------------------------------------------------
def match_growth(growth: pd.DataFrame, env_interval: pd.DataFrame,
                 date_col: str = "date", trt_col: str | None = None) -> pd.DataFrame:
    """생육 각 측정행에 해당 구간의 환경 요약을 붙인다.

    trt_col 을 주면 (조사일 × 처리구)로 병합한다. 한 로거의 센서들이 서로 다른
    처리구를 재는 경우, 처리구를 무시하고 붙이면 다른 처리구의 배지환경이
    섞여 들어가므로 반드시 처리구까지 맞춰야 한다.
    """
    g = growth.copy()
    g[date_col] = pd.to_datetime(g[date_col])
    if trt_col and "trt" in env_interval.columns:
        merged = g.merge(env_interval, how="left",
                         left_on=[date_col, trt_col], right_on=["growth_date", "trt"])
        drop = [c for c in ("growth_date",) if c in merged.columns]
        if trt_col != "trt" and "trt" in merged.columns:
            drop.append("trt")
        return merged.drop(columns=drop)
    merged = g.merge(env_interval, how="left", left_on=date_col, right_on="growth_date")
    if "growth_date" in merged.columns and date_col != "growth_date":
        merged = merged.drop(columns=["growth_date"])
    return merged


# ---------------------------------------------------------------------
# 처리구별 집계 (한 로거의 센서들이 서로 다른 처리구를 잴 때)
# ---------------------------------------------------------------------
def to_daily_by_treatment(frames: dict[str, pd.DataFrame], **kwargs) -> pd.DataFrame:
    """처리구별 10분 자료를 각각 일별 요약한 뒤 하나로 합친다(trt 열 추가)."""
    parts = []
    for trt, df in (frames or {}).items():
        daily = to_daily(df, **kwargs)
        if daily.empty:
            continue
        daily.insert(0, "trt", trt)
        parts.append(daily)
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True).sort_values(["trt", "date"]).reset_index(drop=True)


def aggregate_intervals_by_treatment(daily: pd.DataFrame, intervals: pd.DataFrame,
                                     drop_incomplete_days: bool = False) -> pd.DataFrame:
    """처리구별 일별 요약을 같은 구간 정의로 각각 집계한다(trt 열 유지)."""
    if daily.empty or "trt" not in daily.columns:
        return aggregate_intervals(daily, intervals, drop_incomplete_days)
    parts = []
    for trt, sub in daily.groupby("trt", sort=True):
        agg = aggregate_intervals(sub.drop(columns=["trt"]), intervals, drop_incomplete_days)
        if agg.empty:
            continue
        agg.insert(0, "trt", trt)
        parts.append(agg)
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True).sort_values(["trt", "interval_id"]).reset_index(drop=True)


def add_growth_rate(merged: pd.DataFrame, value_cols: list[str],
                    group_cols: list[str], date_col: str = "date") -> pd.DataFrame:
    """처리구·반복별로 구간 생육량(증가분)과 일당 생육속도를 계산한다."""
    out = merged.sort_values(group_cols + [date_col]).copy()
    for col in value_cols:
        if col not in out.columns:
            continue
        delta = out.groupby(group_cols, dropna=False)[col].diff()
        days = out["days_expected"] if "days_expected" in out.columns else np.nan
        out[f"{col}_delta"] = delta
        out[f"{col}_rate_per_day"] = delta / days
    return out


# ---------------------------------------------------------------------
# 파이프라인 진입점
# ---------------------------------------------------------------------
def run_pipeline(
    env10: pd.DataFrame,
    growth: pd.DataFrame | None = None,
    *,
    interval_minutes: int = 10,
    gdd_base: float = 10.0,
    min_completeness: float = 0.90,
    drop_incomplete_days: bool = False,
    photoperiod_ppfd_threshold: float = 10.0,
    daytime_hours: tuple[int, int] = (9, 15),
    lag_days: int = 0,
    window_days: int | None = None,
    first_start=None,
    growth_date_col: str = "date",
) -> dict:
    """10분 자료(+생육 자료)를 받아 일별·구간별·병합 결과를 한 번에 만든다.

    반환 dict:
      daily          : 일별 요약
      intervals      : 구간 정의(시차 반영)
      env_interval   : 구간별 환경 요약
      merged         : 생육 + 구간환경 병합(생육 자료가 있을 때)
      cadence        : 추정 조사 간격(일)
    """
    daily = to_daily(
        env10,
        interval_minutes=interval_minutes,
        gdd_base=gdd_base,
        photoperiod_ppfd_threshold=photoperiod_ppfd_threshold,
        daytime_hours=tuple(daytime_hours),
        min_completeness=min_completeness,
    )
    result = {"daily": daily, "cadence": 0,
              "intervals": pd.DataFrame(), "env_interval": pd.DataFrame(), "merged": None}
    if growth is None or growth.empty:
        return result

    gdates = pd.to_datetime(growth[growth_date_col])
    result["cadence"] = detect_cadence(gdates)
    intervals = build_intervals(gdates, first_start=first_start,
                                lag_days=lag_days, window_days=window_days)
    env_interval = aggregate_intervals(daily, intervals, drop_incomplete_days=drop_incomplete_days)
    merged = match_growth(growth, env_interval, date_col=growth_date_col)

    result.update({"intervals": intervals, "env_interval": env_interval, "merged": merged})
    return result
