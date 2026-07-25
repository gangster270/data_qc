"""센서 ↔ 처리구 매핑.

한 로거에 달린 여러 TEROS/SQ-521 이 **서로 다른 처리구**를 재고 있을 때 쓴다.
이 경우 반복 센서를 평균내거나 '중복 센서 편차'로 보면 안 되고, 각 센서를
독립된 처리구 환경으로 분리해 생육자료의 처리구와 맞춰야 한다.

매핑 파일(config/sensor_map.yaml) 구조
--------------------------------------
loggers:
  z6-20917:
    zone: "3온실"
    shared: [ppfd, solar]          # 처리구 공통 변수(로거 1개 → 모든 처리구에 동일 적용)
    treatments:
      NI:      {vwc: vwc,        soil_temp: soil_temp,        ec: ec}
      NI+SL:   {vwc: vwc__rep2,  soil_temp: soil_temp__rep2,  ec: ec__rep2}

  - 왼쪽(키)은 생육자료의 처리구 이름과 **정확히 같아야** 병합된다.
  - 오른쪽 값은 io_logger.standardize() 가 만든 열 이름(vwc, vwc__rep2 …).
  - shared 에 적은 변수는 모든 처리구 프레임에 복사된다(기온·PPFD 등 구역 공통).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

from .config import PROJECT_ROOT

DEFAULT_MAP_PATH = PROJECT_ROOT / "config" / "sensor_map.yaml"


def load_sensor_map(path: str | Path | None = None) -> dict:
    """매핑 파일을 읽는다. 없으면 빈 매핑(처리구 분리 없음)."""
    path = Path(path) if path else DEFAULT_MAP_PATH
    if yaml is None or not path.exists():
        return {"loggers": {}}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {"loggers": {}}


def logger_id_from_filename(filename: str) -> str:
    """파일명에서 로거 ID 를 뽑는다(첫 밑줄 앞부분).

    'z6-20917_086260934.xlsx' → 'z6-20917'
    '7구역-z6-21068_08626.xlsx' → '7구역-z6-21068'
    """
    return Path(str(filename)).stem.split("_")[0]


def resolve_logger(smap: dict, key: str) -> dict | None:
    """로거 ID(또는 파일명)로 매핑 항목을 찾는다. 부분일치도 허용."""
    loggers = (smap or {}).get("loggers", {}) or {}
    lid = logger_id_from_filename(key)
    if lid in loggers:
        return loggers[lid]
    for name, entry in loggers.items():           # '7구역-z6-21068' ↔ 'z6-21068'
        if name in lid or lid in name:
            return entry
    return None


def split_by_treatment(std: pd.DataFrame, entry: dict) -> dict[str, pd.DataFrame]:
    """표준화 DataFrame 을 처리구별 프레임으로 나눈다.

    반환: {처리구명: DataFrame(timestamp + 표준변수명 열)}
    매핑에 적힌 열이 없으면 그 변수는 건너뛴다(로거 구성이 바뀐 경우 대비).
    """
    treatments = (entry or {}).get("treatments") or {}
    shared = [c for c in (entry or {}).get("shared", []) if c in std.columns]
    out: dict[str, pd.DataFrame] = {}

    for trt, channels in treatments.items():
        frame = pd.DataFrame({"timestamp": std["timestamp"].to_numpy()})
        for var, src in (channels or {}).items():
            if src in std.columns:
                frame[var] = std[src].to_numpy()
        for var in shared:                          # 구역 공통 변수 복사
            if var not in frame.columns:
                frame[var] = std[var].to_numpy()
        if frame.shape[1] > 1:
            out[str(trt)] = frame
    return out


def coverage_report(std: pd.DataFrame, entry: dict) -> pd.DataFrame:
    """매핑이 어느 열을 쓰고 어느 열을 빠뜨렸는지 점검표."""
    treatments = (entry or {}).get("treatments") or {}
    shared = list((entry or {}).get("shared", []))
    used = {src for ch in treatments.values() for src in (ch or {}).values()} | set(shared)
    data_cols = [c for c in std.columns if c not in ("timestamp", "qc_status")]
    # 대표 열(var)은 개별 열(var__repN)의 복사본이므로, 개별 열이 있으면 점검 대상에서 뺀다
    has_reps = {c.split("__rep")[0] for c in data_cols if "__rep" in c}
    rows = [{"열": c, "매핑": "사용" if c in used else "미매핑"}
            for c in data_cols if not (c in has_reps and "__rep" not in c)]
    rows += [{"열": src, "매핑": "매핑에 있으나 자료에 없음"}
             for src in sorted(used) if src not in data_cols]
    return pd.DataFrame(rows)


def make_template(std: pd.DataFrame, logger_id: str, zone: str = "") -> dict:
    """실제 자료의 열 구성을 보고 매핑 템플릿(초안)을 만든다.

    반복 센서(var, var__rep2 …)를 처리구 후보로 나열하고, 반복이 없는 변수는
    shared(구역 공통)로 넣는다. 처리구 이름은 사용자가 채워야 한다.
    """
    data_cols = [c for c in std.columns if c not in ("timestamp", "qc_status")]
    base_vars: dict[str, list[str]] = {}
    for c in data_cols:
        base = c.split("__rep")[0]
        base_vars.setdefault(base, []).append(c)

    # 센서가 여러 개인 변수는 개별 열(var__rep1..N)만 처리구 후보로 쓴다.
    # 대표 열(var)은 그중 하나의 복사본이므로 슬롯으로 세면 안 된다.
    rep_vars, shared = {}, []
    for base, cols in base_vars.items():
        reps = sorted(c for c in cols if "__rep" in c)
        if reps:
            rep_vars[base] = reps
        else:
            shared.append(base)
    n_trt = max((len(v) for v in rep_vars.values()), default=0)

    treatments = {}
    for i in range(n_trt):
        channels = {}
        for base, cols in rep_vars.items():
            if i < len(cols):
                channels[base] = cols[i]
        treatments[f"TRT{i + 1}"] = channels        # ← 실제 처리구명으로 교체 필요

    return {logger_id: {"zone": zone, "shared": shared, "treatments": treatments}}
