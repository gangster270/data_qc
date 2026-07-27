"""센서 정기 검증(교정·상호비교) 루틴 지원 모듈.

docs/sensor_verification_routine.md 의 절차를 코드로 뒷받침한다.

제공 기능
---------
1) 검증 이력 관리      : 검증 로그 CSV 읽기/추가, 합격 판정
2) 검증 기한 관리      : 점검 주기 대비 경과일 → 도래/지연 목록
3) 현장 상호비교 분석  : 두 센서를 24시간 나란히 두고 편차·상관·회귀 산출
4) 드리프트 추세       : 검증 이력의 편차 변화 기울기(연간 드리프트 추정)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .config import resolve_path

# 검증 로그 표준 스키마
LOG_COLUMNS = [
    "date",            # 검증일 (YYYY-MM-DD)
    "logger_id",       # 로거 ID (예: z6-21068)
    "sensor_id",       # 센서 식별자 (제조번호 또는 포트)
    "sensor_type",     # SQ-521 / ATMOS 14 / TEROS 12 / PYR ...
    "variable",        # temp / rh / soil_temp / vwc / ppfd / solar / ec
    "check_type",      # visual_check / cross_check / reference_check / factory_calibration
    "reference_value", # 기준기(또는 기준센서) 값
    "sensor_value",    # 대상 센서 값
    "deviation",       # sensor - reference (자동 계산 가능)
    "rel_deviation",   # 상대편차 (광센서용)
    "result",          # pass / fail
    "operator",        # 점검자
    "action",          # 조치 내용(청소/재설치/교정/교체)
    "note",            # 비고
]

CHECK_LABELS = {
    "visual_check": "육안점검·청소",
    "cross_check": "센서 상호비교",
    "reference_check": "기준기 대조",
    "factory_calibration": "제조사 재교정",
}


# ---------------------------------------------------------------------
# 1. 검증 로그
# ---------------------------------------------------------------------
def empty_log() -> pd.DataFrame:
    return pd.DataFrame(columns=LOG_COLUMNS)


def load_log(cfg: dict) -> pd.DataFrame:
    """검증 로그 CSV 를 읽는다. 없으면 빈 로그를 만든다."""
    path = resolve_path(cfg, cfg["verification"].get("log_file", "outputs/sensor_verification_log.csv"))
    if not path.exists():
        return empty_log()
    log = pd.read_csv(path, dtype=str)
    for c in LOG_COLUMNS:
        if c not in log.columns:
            log[c] = np.nan
    log["date"] = pd.to_datetime(log["date"], errors="coerce")
    for c in ("reference_value", "sensor_value", "deviation", "rel_deviation"):
        log[c] = pd.to_numeric(log[c], errors="coerce")
    return log[LOG_COLUMNS]


def judge(variable: str, deviation: float, reference: float | None, cfg: dict) -> tuple[str, float | None]:
    """허용오차 대비 합격/불합격 판정. 광센서는 상대편차 기준."""
    tol = cfg["verification"].get("tolerance", {})
    rel = None
    if variable in ("ppfd", "solar"):
        limit = tol.get(f"{variable}_rel", 0.05)
        if reference and reference != 0:
            rel = abs(deviation) / abs(reference)
            return ("pass" if rel <= limit else "fail"), rel
        return "unknown", None
    limit = tol.get(variable)
    if limit is None:
        return "unknown", None
    return ("pass" if abs(deviation) <= limit else "fail"), None


def append_log(cfg: dict, record: dict) -> pd.DataFrame:
    """검증 결과 1건을 로그에 추가하고 저장한다(편차·판정 자동 계산)."""
    log = load_log(cfg)
    rec = {c: record.get(c) for c in LOG_COLUMNS}
    rec["date"] = pd.to_datetime(rec.get("date") or datetime.now().date())

    ref, val = rec.get("reference_value"), rec.get("sensor_value")
    if rec.get("deviation") in (None, "", np.nan) and ref is not None and val is not None:
        try:
            rec["deviation"] = float(val) - float(ref)
        except (TypeError, ValueError):
            rec["deviation"] = np.nan
    if rec.get("deviation") is not None and not pd.isna(rec.get("deviation")):
        result, rel = judge(str(rec.get("variable")), float(rec["deviation"]),
                            float(ref) if ref not in (None, "") else None, cfg)
        rec["result"] = rec.get("result") or result
        rec["rel_deviation"] = rec.get("rel_deviation") if rec.get("rel_deviation") else rel

    log = pd.concat([log, pd.DataFrame([rec])], ignore_index=True)
    path = resolve_path(cfg, cfg["verification"].get("log_file", "outputs/sensor_verification_log.csv"))
    path.parent.mkdir(parents=True, exist_ok=True)
    log.to_csv(path, index=False, encoding="utf-8-sig")
    return log


# ---------------------------------------------------------------------
# 2. 검증 기한 관리
# ---------------------------------------------------------------------
def due_status(cfg: dict, sensors: list[dict] | None = None, today=None) -> pd.DataFrame:
    """센서 × 점검종류별 '마지막 점검일 / 다음 예정일 / 지연일수'를 정리한다.

    sensors: [{"logger_id":..., "sensor_id":..., "sensor_type":..., "variable":...}, ...]
             생략하면 검증 로그에 등장한 센서 목록을 사용한다.
    """
    log = load_log(cfg)
    today = pd.Timestamp(today or datetime.now().date())
    schedule = cfg["verification"].get("schedule_days", {})

    if sensors is None:
        if log.empty:
            return pd.DataFrame(columns=["logger_id", "sensor_id", "sensor_type", "variable",
                                         "check_type", "점검종류", "마지막점검", "다음예정",
                                         "지연일수", "상태"])
        sensors = (log[["logger_id", "sensor_id", "sensor_type", "variable"]]
                   .drop_duplicates().to_dict("records"))

    rows = []
    for s in sensors:
        for check, period in schedule.items():
            sub = log[(log["sensor_id"].astype(str) == str(s.get("sensor_id"))) &
                      (log["check_type"] == check)]
            last = sub["date"].max() if not sub.empty else pd.NaT
            if pd.isna(last):
                next_due, overdue, status = pd.NaT, np.nan, "미실시"
            else:
                next_due = last + pd.Timedelta(days=int(period))
                overdue = int((today - next_due).days)
                status = "지연" if overdue > 0 else ("임박" if overdue > -3 else "정상")
            rows.append({
                **{k: s.get(k) for k in ("logger_id", "sensor_id", "sensor_type", "variable")},
                "check_type": check,
                "점검종류": CHECK_LABELS.get(check, check),
                "주기(일)": int(period),
                "마지막점검": last,
                "다음예정": next_due,
                "지연일수": overdue,
                "상태": status,
            })
    out = pd.DataFrame(rows)
    if not out.empty:
        order = {"지연": 0, "미실시": 1, "임박": 2, "정상": 3}
        out = out.sort_values(by="상태", key=lambda c: c.map(order)).reset_index(drop=True)
    return out


# ---------------------------------------------------------------------
# 3. 현장 상호비교 (두 센서를 나란히 두고 24시간 비교)
# ---------------------------------------------------------------------
def cross_check(df10: pd.DataFrame, col_ref: str, col_test: str, variable: str,
                cfg: dict, start=None, end=None, daytime_only: bool = False) -> dict:
    """두 센서 열을 비교해 bias·MAE·RMSE·상관·회귀기울기와 합격여부를 산출한다.

    col_ref  : 기준 센서 열 (신규·교정필 센서)
    col_test : 검증 대상 열
    daytime_only : 광센서는 야간 0 구간이 상관을 부풀리므로 주간만 비교 권장
    """
    df = df10.copy()
    if start is not None:
        df = df[df["timestamp"] >= pd.Timestamp(start)]
    if end is not None:
        df = df[df["timestamp"] <= pd.Timestamp(end)]
    if daytime_only:
        h = pd.to_datetime(df["timestamp"]).dt.hour
        df = df[(h >= 9) & (h < 16)]

    pair = df[[col_ref, col_test]].dropna()
    if len(pair) < 6:
        return {"n": len(pair), "error": "비교 가능한 관측이 부족합니다(6개 미만)."}

    ref, test = pair[col_ref].to_numpy(float), pair[col_test].to_numpy(float)
    diff = test - ref
    bias = float(np.mean(diff))
    mae = float(np.mean(np.abs(diff)))
    rmse = float(np.sqrt(np.mean(diff ** 2)))
    r = float(np.corrcoef(ref, test)[0, 1]) if np.std(ref) > 0 and np.std(test) > 0 else np.nan
    slope, intercept = (np.polyfit(ref, test, 1) if np.std(ref) > 0 else (np.nan, np.nan))

    tol = cfg["verification"].get("tolerance", {})
    if variable in ("ppfd", "solar"):
        denom = np.mean(np.abs(ref))
        rel_bias = float(bias / denom) if denom else np.nan
        limit = tol.get(f"{variable}_rel", 0.05)
        result = "pass" if abs(rel_bias) <= limit else "fail"
        criterion = f"상대편차 |{rel_bias:.1%}| ≤ {limit:.0%}"
    else:
        rel_bias = np.nan
        limit = tol.get(variable, np.nan)
        result = "pass" if (not np.isnan(limit) and abs(bias) <= limit) else \
                 ("fail" if not np.isnan(limit) else "unknown")
        criterion = f"편차 |{bias:.3g}| ≤ {limit}"

    return {
        "n": int(len(pair)), "variable": variable,
        "ref_col": col_ref, "test_col": col_test,
        "ref_mean": float(np.mean(ref)), "test_mean": float(np.mean(test)),
        "bias": round(bias, 4), "rel_bias": None if np.isnan(rel_bias) else round(rel_bias, 4),
        "MAE": round(mae, 4), "RMSE": round(rmse, 4),
        "r": None if np.isnan(r) else round(r, 4),
        "slope": None if np.isnan(slope) else round(float(slope), 4),
        "intercept": None if np.isnan(intercept) else round(float(intercept), 4),
        "criterion": criterion, "result": result,
        "period": (str(pair.index.min()), str(pair.index.max())),
    }


def daily_pair_table(df10: pd.DataFrame, col_ref: str, col_test: str) -> pd.DataFrame:
    """상호비교 기간의 일별 편차 표(보고서·그래프용)."""
    df = df10[["timestamp", col_ref, col_test]].dropna().copy()
    if df.empty:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["timestamp"]).dt.date
    df["diff"] = df[col_test] - df[col_ref]
    return (df.groupby("date")
              .agg(n=("diff", "size"), ref_mean=(col_ref, "mean"), test_mean=(col_test, "mean"),
                   bias=("diff", "mean"), mae=("diff", lambda x: x.abs().mean()),
                   max_abs_diff=("diff", lambda x: x.abs().max()))
              .round(3).reset_index())


# ---------------------------------------------------------------------
# 4. 드리프트 추세
# ---------------------------------------------------------------------
def drift_trend(cfg: dict, sensor_id: str, variable: str | None = None) -> dict:
    """검증 이력의 편차 변화로 연간 드리프트를 추정한다(단순 선형회귀)."""
    log = load_log(cfg)
    sub = log[(log["sensor_id"].astype(str) == str(sensor_id)) & log["deviation"].notna()]
    if variable:
        sub = sub[sub["variable"] == variable]
    sub = sub.dropna(subset=["date"]).sort_values("date")
    if len(sub) < 3:
        return {"n": len(sub), "note": "추세 추정에는 최소 3회 검증 이력이 필요합니다."}

    x = (sub["date"] - sub["date"].min()).dt.days.to_numpy(float)
    y = sub["deviation"].to_numpy(float)
    slope, intercept = np.polyfit(x, y, 1)
    return {
        "n": int(len(sub)),
        "period": f"{sub['date'].min():%Y-%m-%d} ~ {sub['date'].max():%Y-%m-%d}",
        "slope_per_day": round(float(slope), 6),
        "drift_per_year": round(float(slope) * 365, 4),
        "last_deviation": round(float(y[-1]), 4),
        "history": sub[["date", "check_type", "deviation", "result"]].reset_index(drop=True),
    }


def export_template(path: str | Path) -> Path:
    """빈 검증 로그 템플릿 CSV 를 만든다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    empty_log().to_csv(path, index=False, encoding="utf-8-sig")
    return path
