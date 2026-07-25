"""결측 · 센서 오류 자동 감지 규칙 엔진.

표준화된 10분 자료(timestamp + temp/rh/soil_temp/vwc/ppfd/solar/ec/co2)를 받아
규칙별로 이상을 찾고 알림 레코드(DataFrame)를 만든다.

규칙 목록
---------
R01 timestamp_gap        기록 누락(연속 결측 구간)
R02 missing_ratio        변수별 일 결측률 초과
R03 out_of_range         물리적으로 불가능한 값
R04 flatline             동일값 연속(센서 고착·통신 정지)
R05 spike                10분 간 비현실적 급변
R06 daytime_dark         주간에 광센서가 어두움(탈락·차폐·오염)
R07 night_light          야간 광 검출(광 누출·오프셋) ※ 야간보광 시험은 기본 비활성
R08 rh_saturated         습도 99% 이상 장시간 지속(결로·고장)
R09 logger_offline       최신 관측이 오래됨(통신 두절·전원)
R10 pair_divergence      중복 센서 간 편차 초과(드리프트)
R11 transmittance_drop   내부PPFD/외부일사 비율 급락(오염·차광)

각 레코드: level(INFO/WARN/CRITICAL), rule, variable, 기간, 값, 메시지, dedup key
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from .io_logger import LABELS

LEVEL_ORDER = {"INFO": 0, "WARN": 1, "CRITICAL": 2}
STD_VARS = ["temp", "rh", "soil_temp", "vwc", "ppfd", "solar", "ec", "co2"]


def _alert(rule, level, variable, message, *, start=None, end=None, value=None, detail=None) -> dict:
    """알림 레코드 1건 생성. key 는 중복 발송 억제(cooldown)용 식별자."""
    scope = pd.Timestamp(start).strftime("%Y-%m-%d %H:%M") if start is not None else "-"
    return {
        "rule": rule,
        "level": level,
        "variable": variable,
        "label": LABELS.get(variable, variable),
        "start": pd.Timestamp(start) if start is not None else pd.NaT,
        "end": pd.Timestamp(end) if end is not None else pd.NaT,
        "value": value,
        "message": message,
        "detail": detail or "",
        "key": f"{rule}|{variable}|{scope}",
        "detected_at": pd.Timestamp(datetime.now()).floor("s"),
    }


def _run_lengths(mask: pd.Series) -> list[tuple[int, int]]:
    """True 가 연속되는 구간의 (시작 위치, 길이) 목록."""
    runs, start = [], None
    values = mask.to_numpy()
    for i, v in enumerate(values):
        if v and start is None:
            start = i
        elif not v and start is not None:
            runs.append((start, i - start))
            start = None
    if start is not None:
        runs.append((start, len(values) - start))
    return runs


def sensor_columns(df: pd.DataFrame, var: str) -> list[str]:
    """변수의 실제 센서 열 목록.

    센서가 여러 개면 개별 열(var__rep1..N)을, 하나면 대표 열(var)을 돌려준다.
    대표 열은 개별 열 중 하나의 복사본이므로 함께 세면 중복 집계가 된다.
    """
    reps = sorted(c for c in df.columns if c.startswith(f"{var}__rep"))
    if reps:
        return reps
    return [var] if var in df.columns else []


def _window(df: pd.DataFrame, lookback_days: int | None, now=None) -> pd.DataFrame:
    """최근 N일만 잘라낸다(과거 이슈 반복 알림 방지)."""
    if not lookback_days or df.empty:
        return df
    end = pd.Timestamp(now) if now is not None else pd.Timestamp(df["timestamp"].max())
    return df[df["timestamp"] >= end - pd.Timedelta(days=lookback_days)]


# ---------------------------------------------------------------------
# R01 기록 누락
# ---------------------------------------------------------------------
def check_timestamp_gaps(df10: pd.DataFrame, cfg: dict, lookback_days=None, now=None) -> list[dict]:
    """기록 누락 구간 탐지.

    10분 격자로 이미 정합된 자료(qc_status 열 존재)면 삽입된 빈 행을 기준으로,
    그렇지 않으면 timestamp 간격으로 판정한다.
    """
    interval = int(cfg["site"]["interval_minutes"])
    qc = cfg["qc"]
    df = _window(df10, lookback_days, now).sort_values("timestamp").reset_index(drop=True)
    if len(df) < 2:
        return []

    def _level(minutes: float) -> str:
        if minutes >= qc["gap_critical_minutes"]:
            return "CRITICAL"
        return "WARN" if minutes >= qc["gap_warn_minutes"] else "INFO"

    out = []
    if "qc_status" in df.columns:
        inserted = df["qc_status"].eq("missing_timestamp_inserted")
        for pos, length in _run_lengths(inserted):
            minutes = length * interval
            start = df["timestamp"].iloc[pos]
            end = df["timestamp"].iloc[pos + length - 1]
            out.append(_alert(
                "R01_timestamp_gap", _level(minutes), "timestamp",
                f"기록 누락 {length}건({minutes}분): {start:%m-%d %H:%M} ~ {end:%m-%d %H:%M}",
                start=start, end=end, value=length,
            ))
        return out

    ts = pd.to_datetime(df["timestamp"])
    gaps = ts.diff().dt.total_seconds().div(60).fillna(interval)
    for i, gap_min in enumerate(gaps):
        if gap_min <= interval:
            continue
        missing = int(gap_min // interval) - 1
        if missing <= 0:
            continue
        minutes = missing * interval
        start, end = ts.iloc[i - 1], ts.iloc[i]
        out.append(_alert(
            "R01_timestamp_gap", _level(minutes), "timestamp",
            f"기록 누락 {missing}건({minutes}분): {start:%m-%d %H:%M} ~ {end:%m-%d %H:%M}",
            start=start, end=end, value=missing,
        ))
    return out


# ---------------------------------------------------------------------
# R02 변수별 일 결측률
# ---------------------------------------------------------------------
def check_missing_ratio(df10: pd.DataFrame, cfg: dict, lookback_days=None, now=None) -> list[dict]:
    interval = int(cfg["site"]["interval_minutes"])
    qc = cfg["qc"]
    expected = int(24 * 60 / interval)
    df = _window(df10, lookback_days, now).copy()
    if df.empty:
        return []
    df["date"] = pd.to_datetime(df["timestamp"]).dt.date

    # 점검 창의 첫날·마지막날은 창이 하루를 다 덮지 않아 결측률이 과대 계산된다.
    # 하루가 온전히 창 안에 들어온 날짜만 평가한다(경계 오탐 방지).
    counts = df.groupby("date")["timestamp"].size()
    full_days = set(counts[counts >= expected * 0.99].index)
    edge_days = {df["date"].min(), df["date"].max()}
    evaluable = {d for d in counts.index if d in full_days or d not in edge_days}

    out = []
    for var in [v for v in STD_VARS if v in df.columns]:
        # 관측기간 내내 비어 있는 변수(미설치)는 결측 알림 대상에서 제외
        if df[var].notna().sum() == 0:
            continue
        for date, g in df.groupby("date"):
            if date not in evaluable:
                continue
            ratio = 1 - g[var].notna().sum() / expected
            if ratio >= qc["missing_critical_ratio"]:
                level = "CRITICAL"
            elif ratio >= qc["missing_warn_ratio"]:
                level = "WARN"
            else:
                continue
            out.append(_alert(
                "R02_missing_ratio", level, var,
                f"{LABELS.get(var, var)} {date} 결측률 {ratio:.1%} "
                f"({int(g[var].notna().sum())}/{expected}건 수신)",
                start=pd.Timestamp(date), end=pd.Timestamp(date), value=round(float(ratio), 4),
            ))
    return out


# ---------------------------------------------------------------------
# R03 범위 이탈
# ---------------------------------------------------------------------
def check_out_of_range(df10: pd.DataFrame, cfg: dict, lookback_days=None, now=None) -> list[dict]:
    df = _window(df10, lookback_days, now).copy()
    if df.empty:
        return []
    df["date"] = pd.to_datetime(df["timestamp"]).dt.date
    out = []
    for var, spec in cfg["sensors"].items():
        if var not in df.columns:
            continue
        s = df[var]
        bad = s.notna() & ((s < spec["min"]) | (s > spec["max"]))
        if not bad.any():
            continue
        for date, g in df[bad].groupby("date"):
            n = len(g)
            level = "CRITICAL" if n >= 12 else "WARN"    # 2시간 이상이면 심각
            out.append(_alert(
                "R03_out_of_range", level, var,
                f"{spec.get('label', var)} 범위 이탈 {n}건 ({date}) "
                f"허용 {spec['min']}~{spec['max']}{spec.get('unit', '')}, "
                f"관측 {g[var].min():.1f}~{g[var].max():.1f}",
                start=pd.Timestamp(date), end=pd.Timestamp(date), value=n,
            ))
    return out


# ---------------------------------------------------------------------
# R04 고착(flatline)
# ---------------------------------------------------------------------
def check_flatline(df10: pd.DataFrame, cfg: dict, lookback_days=None, now=None) -> list[dict]:
    qc = cfg["qc"]
    if not qc.get("flatline_enabled", True):
        return []
    interval = int(cfg["site"]["interval_minutes"])
    df = _window(df10, lookback_days, now).sort_values("timestamp").reset_index(drop=True)
    ignore_zero = set(qc.get("flatline_ignore_zero", []))
    out = []
    for var, spec in cfg["sensors"].items():
        # 센서가 여러 개면 개별 열(var__rep1..N)을 점검한다. 대표 열(var)은 그중
        # 하나의 복사본이므로 중복 알림을 피하려 건너뛴다.
        # 미연결 포트는 '전 구간 0' 으로 나오므로 여기서 잡지 않으면 조용히 묻힌다.
        for col in sensor_columns(df, var):
            out.extend(_flatline_for_column(df, col, var, spec, qc, ignore_zero, interval))
    return out


def _flatline_for_column(df, col, var, spec, qc, ignore_zero, interval) -> list[dict]:
    """단일 열의 동일값 연속 구간을 찾아 알림 목록을 만든다."""
    out: list[dict] = []
    s = df[col]
    if s.notna().sum() < 2:
        return out
    label = spec.get("label", var) + ("" if col == var else f"[{col}]")
    same = s.eq(s.shift()) & s.notna()
    if var in ignore_zero:
        # 광센서: 야간·박명의 낮은 값 연속은 정상(분해능 한계)
        # 그 외(EC 등): 0 연속만 정상으로 본다
        #   TEROS 는 배지가 마르면(vwc<10%) EC 를 0 으로 출력한다 — 고장이 아니라 물리 현상
        floor = qc.get("flatline_light_floor", 10) if var in ("ppfd", "solar") else 0
        same &= s > floor
    # 관측 전 구간이 한 값이면 고착이 아니라 미연결·고장 포트다
    constant_all = s.dropna().nunique() <= 1
    for pos, length in _run_lengths(same):
        run_len = length + 1                       # shift 비교이므로 +1
        if run_len < spec.get("flat_n", 36):
            continue
        start = df["timestamp"].iloc[max(pos - 1, 0)]
        end = df["timestamp"].iloc[pos + length - 1]
        hours = run_len * interval / 60
        level = "CRITICAL" if hours >= 12 else "WARN"
        cause = ("미연결·고장 포트(전 구간 동일값) — 배선·포트 설정 확인" if constant_all
                 else "센서 고착·통신 정지 의심")
        out.append(_alert(
            "R04_flatline", level, var,
            f"{label} 동일값({s.iloc[pos]:.3g}) {run_len}회 연속({hours:.1f}시간) — {cause}",
            start=start, end=end, value=float(s.iloc[pos]), detail=col,
        ))
    return out


# ---------------------------------------------------------------------
# R05 급변(spike)
# ---------------------------------------------------------------------
def check_spike(df10: pd.DataFrame, cfg: dict, lookback_days=None, now=None) -> list[dict]:
    qc = cfg["qc"]
    if not qc.get("spike_enabled", True):
        return []
    df = _window(df10, lookback_days, now).sort_values("timestamp").copy()
    if df.empty:
        return []
    df["date"] = pd.to_datetime(df["timestamp"]).dt.date
    out = []
    for var, spec in cfg["sensors"].items():
        if var not in df.columns or var in ("ppfd", "solar"):
            continue                                   # 광은 구름에 의해 정상적으로 급변
        thr = spec.get("spike")
        if thr is None:
            continue
        d = df[var].diff().abs()
        hit = d > thr
        if not hit.any():
            continue
        for date, g in df[hit].groupby("date"):
            n = len(g)
            if n < qc.get("spike_warn_count", 3):
                continue
            out.append(_alert(
                "R05_spike", "WARN", var,
                f"{spec.get('label', var)} 10분간 {thr}{spec.get('unit','')} 초과 급변 {n}회 ({date}) "
                f"— 접촉불량·노이즈 의심",
                start=pd.Timestamp(date), end=pd.Timestamp(date), value=n,
            ))
    return out


# ---------------------------------------------------------------------
# R06 주간 암흑 / R07 야간 광
# ---------------------------------------------------------------------
def check_light_pattern(df10: pd.DataFrame, cfg: dict, lookback_days=None, now=None) -> list[dict]:
    qc = cfg["qc"]
    df = _window(df10, lookback_days, now).copy()
    if df.empty:
        return []
    df["date"] = pd.to_datetime(df["timestamp"]).dt.date
    df["hour"] = pd.to_datetime(df["timestamp"]).dt.hour
    out = []

    # R06 주간 암흑: 10~14시 최대 PPFD(또는 일사량)가 임계 미만
    for var, thr in (("ppfd", qc["daytime_dark_ppfd_max"]), ("solar", qc["daytime_dark_ppfd_max"] / 2)):
        if var not in df.columns or df[var].notna().sum() == 0:
            continue
        noon = df[(df["hour"] >= 10) & (df["hour"] < 15)]
        for date, g in noon.groupby("date"):
            vals = g[var].dropna()
            if vals.empty:
                continue
            if vals.max() < thr:
                out.append(_alert(
                    "R06_daytime_dark", "CRITICAL", var,
                    f"{LABELS.get(var, var)} 주간(10~15시) 최대값 {vals.max():.1f} < {thr} ({date}) "
                    f"— 센서 탈락·완전 차폐·오염 의심",
                    start=pd.Timestamp(date), end=pd.Timestamp(date), value=float(vals.max()),
                ))

    # R07 야간 광: 야간보광(NI/SL) 시험 중이면 비활성 유지
    if qc.get("night_light_enabled", False) and "ppfd" in df.columns:
        h0, h1 = qc.get("night_hours", [23, 3])
        night = df[(df["hour"] >= h0) | (df["hour"] < h1)] if h0 > h1 else \
                df[(df["hour"] >= h0) & (df["hour"] < h1)]
        thr = qc["night_light_ppfd"]
        for date, g in night.groupby("date"):
            vals = g["ppfd"].dropna()
            if vals.empty:
                continue
            if vals.max() > thr:
                out.append(_alert(
                    "R07_night_light", "WARN", "ppfd",
                    f"야간({h0}~{h1}시) PPFD 최대 {vals.max():.1f} > {thr} ({date}) "
                    f"— 광 누출 또는 센서 오프셋(야간보광 처리구면 무시)",
                    start=pd.Timestamp(date), end=pd.Timestamp(date), value=float(vals.max()),
                ))
    return out


# ---------------------------------------------------------------------
# R13 고온 사건 (센서 오류가 아니라 '실제로 위험한 값')
# ---------------------------------------------------------------------
def check_heat_event(df10: pd.DataFrame, cfg: dict, lookback_days=None, now=None) -> list[dict]:
    """작물 위험 수준의 고온을 경보한다.

    센서 물리범위(ATMOS 14: -40~80℃)는 통과하지만 작물에는 치명적인 값이 있다.
    실측 사례: 환기 실패로 기온 70℃·배지온도 50℃가 한나절 지속. 이런 값은
    지우면 안 되고(진짜 사건), 대신 즉시 알려야 한다.
    """
    qc = cfg["qc"]
    df = _window(df10, lookback_days, now).copy()
    if df.empty:
        return []
    df["date"] = pd.to_datetime(df["timestamp"]).dt.date
    interval = int(cfg["site"]["interval_minutes"])
    out = []
    for var, thr_key in (("temp", "heat_event_temp"), ("soil_temp", "heat_event_soil_temp")):
        thr = qc.get(thr_key)
        if var not in df.columns or thr is None:
            continue
        for date, g in df.groupby("date"):
            s = g[var].dropna()
            if s.empty or s.max() <= thr:
                continue
            n_over = int((s > thr).sum())
            hours = n_over * interval / 60
            level = "CRITICAL" if hours >= 1 else "WARN"
            out.append(_alert(
                "R13_heat_event", level, var,
                f"{LABELS.get(var, var)} {thr}℃ 초과 {hours:.1f}시간 (최고 {s.max():.1f}℃, {date}) "
                f"— 환기·차광 상태 확인, 해당 구간 생육자료 해석 주의",
                start=pd.Timestamp(date), end=pd.Timestamp(date), value=float(s.max()),
            ))
    return out


# ---------------------------------------------------------------------
# R08 습도 포화 지속
# ---------------------------------------------------------------------
def check_rh_saturated(df10: pd.DataFrame, cfg: dict, lookback_days=None, now=None) -> list[dict]:
    if "rh" not in df10.columns:
        return []
    interval = int(cfg["site"]["interval_minutes"])
    hours_thr = cfg["qc"].get("rh_saturated_hours", 12)
    need = int(hours_thr * 60 / interval)
    df = _window(df10, lookback_days, now).sort_values("timestamp").reset_index(drop=True)
    s = df["rh"]
    mask = s.notna() & (s >= 99)
    out = []
    for pos, length in _run_lengths(mask):
        if length < need:
            continue
        start, end = df["timestamp"].iloc[pos], df["timestamp"].iloc[pos + length - 1]
        out.append(_alert(
            "R08_rh_saturated", "WARN", "rh",
            f"습도 99% 이상 {length * interval / 60:.1f}시간 연속 "
            f"({start:%m-%d %H:%M}~{end:%m-%d %H:%M}) — 결로·필터 오염 점검 필요",
            start=start, end=end, value=length,
        ))
    return out


# ---------------------------------------------------------------------
# R12 진성 오류값(#VALUE!, ERROR, inf …) 검출
# ---------------------------------------------------------------------
def check_error_tokens(map_report: pd.DataFrame, cfg: dict, warn_ratio: float = 0.001) -> list[dict]:
    """열 매핑 리포트의 '오류값수'를 근거로 센서 출력 오류를 알린다.

    NaN(빈칸)은 결측이지 오류가 아니다. 여기서 잡는 것은 #VALUE!·ERROR·inf 처럼
    센서/로거가 실제로 비정상 출력을 낸 경우다.
    """
    if map_report is None or map_report.empty or "오류값수" not in map_report.columns:
        return []
    out = []
    for _, r in map_report.iterrows():
        n_err = int(r.get("오류값수", 0) or 0)
        if n_err <= 0:
            continue
        total = int(r.get("유효값수", 0) or 0) + int(r.get("결측수", 0) or 0)
        ratio = n_err / total if total else 0
        level = "CRITICAL" if ratio > 0.3 else ("WARN" if ratio > warn_ratio else "INFO")
        out.append(_alert(
            "R12_error_value", level, str(r.get("표준변수", "-")),
            f"{r.get('라벨', '')} 열 '{r.get('원본열')}' 에서 오류값 {n_err:,}건({ratio:.2%}) "
            f"— 센서 출력 오류·계산 오류 확인",
            value=n_err, detail=str(r.get("원본열")),
        ))
    return out


# ---------------------------------------------------------------------
# R09 로거 통신 두절
# ---------------------------------------------------------------------
def check_offline(df10: pd.DataFrame, cfg: dict, now=None) -> list[dict]:
    if df10.empty:
        return [_alert("R09_logger_offline", "CRITICAL", "logger", "수신 데이터 없음")]
    qc = cfg["qc"]
    last = pd.Timestamp(df10["timestamp"].max())
    ref = pd.Timestamp(now) if now is not None else pd.Timestamp(datetime.now())
    delay = (ref - last).total_seconds() / 60
    if delay >= qc["offline_critical_minutes"]:
        level = "CRITICAL"
    elif delay >= qc["offline_warn_minutes"]:
        level = "WARN"
    else:
        return []
    return [_alert(
        "R09_logger_offline", level, "logger",
        f"최근 관측이 {delay / 60:.1f}시간 전({last:%Y-%m-%d %H:%M}) — 로거 전원·통신 확인 필요",
        start=last, end=ref, value=round(delay, 1),
    )]


# ---------------------------------------------------------------------
# R10 중복 센서 편차
# ---------------------------------------------------------------------
def check_pair_divergence(df10: pd.DataFrame, cfg: dict, lookback_days=None, now=None) -> list[dict]:
    """같은 환경에 나란히 설치된 동일 종류 센서(var, var__rep2 ...) 간 편차 점검.

    반복 센서가 서로 다른 처리구를 재는 현장에서는 편차가 정상이므로
    `qc.pair_divergence_enabled: false` 로 꺼 둔다(기본값). 교정 목적으로
    두 센서를 나란히 놓고 비교할 때만 켠다.
    """
    if not cfg["qc"].get("pair_divergence_enabled", False):
        return []
    tol = cfg["qc"].get("pair_divergence", {})
    df = _window(df10, lookback_days, now)
    if df.empty:
        return []
    out = []
    for var, limit in tol.items():
        cols = sensor_columns(df, var)
        if len(cols) < 2:
            continue
        base = df[cols[0]]
        for other in cols[1:]:
            pair = df[[cols[0], other]].dropna()
            if len(pair) < 6:
                continue
            diff = (pair[other] - pair[cols[0]])
            if var in ("ppfd", "solar"):
                # 광은 절대편차 대신 상대편차로 비교
                denom = pair[cols[0]].where(pair[cols[0]] > 50)
                rel = (diff / denom).dropna()
                if rel.empty:
                    continue
                metric, unit = float(rel.abs().median()), ""
            else:
                metric, unit = float(diff.abs().median()), ""
            if metric > limit:
                out.append(_alert(
                    "R10_pair_divergence", "WARN", var,
                    f"{LABELS.get(var, var)} 중복센서 편차 중앙값 {metric:.3g}{unit} > 허용 {limit} "
                    f"({cols[0]} vs {other}) — 드리프트·설치위치 확인",
                    start=df["timestamp"].min(), end=df["timestamp"].max(),
                    value=round(metric, 4), detail=f"{cols[0]} vs {other}",
                ))
    return out


# ---------------------------------------------------------------------
# R11 투과율 급락
# ---------------------------------------------------------------------
def check_transmittance(df10: pd.DataFrame, cfg: dict, now=None) -> list[dict]:
    """내부 PPFD 대비 (일사량→PPFD 환산) 비율의 급락을 잡는다.

    같은 로거에 SQ-521(PPFD)과 PYR(일사량)이 함께 있을 때 유효하다.
    비율이 최근 며칠 갑자기 떨어지면 렌즈 오염·차광막·구조물 그늘을 의심한다.
    """
    tcfg = cfg["qc"].get("transmittance", {})
    if not tcfg.get("enabled", True):
        return []
    if "ppfd" not in df10.columns or "solar" not in df10.columns:
        return []

    df = df10.copy()
    df["date"] = pd.to_datetime(df["timestamp"]).dt.date
    df["hour"] = pd.to_datetime(df["timestamp"]).dt.hour
    day = df[(df["hour"] >= 10) & (df["hour"] < 15)].dropna(subset=["ppfd", "solar"])
    if day.empty:
        return []
    factor = tcfg.get("solar_to_ppfd_factor", 2.057)
    day = day[day["solar"] > 50]
    if day.empty:
        return []
    day["ratio"] = day["ppfd"] / (day["solar"] * factor)
    daily_ratio = day.groupby("date")["ratio"].median().sort_index()
    if len(daily_ratio) < 5:
        return []

    recent_n = int(tcfg.get("recent_days", 3))
    base_n = int(tcfg.get("baseline_days", 30))
    recent = daily_ratio.iloc[-recent_n:]
    baseline = daily_ratio.iloc[-(base_n + recent_n):-recent_n]
    if baseline.empty or recent.empty:
        return []
    r_med, b_med = float(recent.median()), float(baseline.median())
    if b_med <= 0:
        return []
    if r_med / b_med < tcfg.get("drop_ratio", 0.70):
        return [_alert(
            "R11_transmittance_drop", "WARN", "ppfd",
            f"투과율(내부PPFD/외부환산) 급락: 최근 {recent_n}일 중앙값 {r_med:.3f} vs "
            f"기준 {b_med:.3f} ({r_med / b_med:.0%}) — 렌즈 오염·차광막·그늘 확인",
            start=pd.Timestamp(recent.index[0]), end=pd.Timestamp(recent.index[-1]),
            value=round(r_med / b_med, 3),
        )]
    return []


# ---------------------------------------------------------------------
# 통합 실행
# ---------------------------------------------------------------------
RULES = [
    ("R01", check_timestamp_gaps),
    ("R02", check_missing_ratio),
    ("R03", check_out_of_range),
    ("R04", check_flatline),
    ("R05", check_spike),
    ("R06", check_light_pattern),
    ("R08", check_rh_saturated),
    ("R13", check_heat_event),
    ("R10", check_pair_divergence),
]


def run_all(df10: pd.DataFrame, cfg: dict, lookback_days: int | None = 7, now=None,
            map_report: pd.DataFrame | None = None) -> pd.DataFrame:
    """모든 규칙을 실행해 알림 DataFrame 을 반환(심각도·시간 순 정렬).

    map_report: io_logger.standardize() 가 돌려준 열 매핑 리포트.
                넘기면 진성 오류값(R12) 점검까지 수행한다.
    """
    alerts: list[dict] = []
    for _, fn in RULES:
        try:
            alerts.extend(fn(df10, cfg, lookback_days=lookback_days, now=now))
        except Exception as e:  # 한 규칙 실패가 전체 모니터링을 멈추지 않게
            alerts.append(_alert("R00_rule_error", "INFO", "-",
                                 f"규칙 실행 오류: {fn.__name__} → {e}"))
    try:
        alerts.extend(check_offline(df10, cfg, now=now))
        alerts.extend(check_transmittance(df10, cfg, now=now))
        if map_report is not None:
            alerts.extend(check_error_tokens(map_report, cfg))
    except Exception as e:
        alerts.append(_alert("R00_rule_error", "INFO", "-", f"규칙 실행 오류: {e}"))

    if not alerts:
        return pd.DataFrame(columns=["rule", "level", "variable", "label", "start", "end",
                                     "value", "message", "detail", "key", "detected_at"])
    out = pd.DataFrame(alerts)
    out["_lv"] = out["level"].map(LEVEL_ORDER).fillna(0)
    out = out.sort_values(["_lv", "start"], ascending=[False, True]).drop(columns=["_lv"])
    return out.reset_index(drop=True)


def summarize(alerts: pd.DataFrame) -> dict:
    """알림 요약(등급별 건수, 규칙별 건수, 영향 변수)."""
    if alerts.empty:
        return {"total": 0, "CRITICAL": 0, "WARN": 0, "INFO": 0, "by_rule": {}, "variables": []}
    return {
        "total": int(len(alerts)),
        "CRITICAL": int((alerts["level"] == "CRITICAL").sum()),
        "WARN": int((alerts["level"] == "WARN").sum()),
        "INFO": int((alerts["level"] == "INFO").sum()),
        "by_rule": alerts["rule"].value_counts().to_dict(),
        "variables": sorted(alerts["variable"].dropna().unique().tolist()),
    }


def health_score(df10: pd.DataFrame, cfg: dict, days: int = 7) -> pd.DataFrame:
    """변수별 최근 상태 점수표(대시보드 카드용).

    수신율 · 범위이탈률 · 최근값 · 마지막 유효 관측시각을 한 표로 정리한다.
    """
    interval = int(cfg["site"]["interval_minutes"])
    if df10.empty:
        return pd.DataFrame()
    end = pd.Timestamp(df10["timestamp"].max())
    # 구간 양끝을 모두 포함하면 1건이 더 잡혀 수신율이 100%를 넘으므로 시작점은 제외
    sub = df10[df10["timestamp"] > end - pd.Timedelta(days=days)]
    expected = int(days * 24 * 60 / interval)
    rows = []
    for var, spec in cfg["sensors"].items():
        if var not in sub.columns:
            continue
        s = sub[var]
        if s.notna().sum() == 0 and df10[var].notna().sum() == 0:
            continue                                    # 미설치 센서는 표에서 제외
        valid = int(s.notna().sum())
        oor = int((s.notna() & ((s < spec["min"]) | (s > spec["max"]))).sum())
        last_valid = sub.loc[s.notna(), "timestamp"].max() if valid else pd.NaT
        rows.append({
            "변수": spec.get("label", var),
            "키": var,
            "수신율": round(valid / expected, 3) if expected else np.nan,
            "결측률": round(1 - valid / expected, 3) if expected else np.nan,
            "범위이탈": oor,
            "최근값": float(s.dropna().iloc[-1]) if valid else np.nan,
            "평균": round(float(s.mean()), 2) if valid else np.nan,
            "최소": round(float(s.min()), 2) if valid else np.nan,
            "최대": round(float(s.max()), 2) if valid else np.nan,
            "마지막관측": last_valid,
            "상태": ("정상" if valid / expected >= 0.9 and oor == 0 else
                     ("주의" if valid / expected >= 0.5 else "위험")) if expected else "-",
        })
    return pd.DataFrame(rows)
