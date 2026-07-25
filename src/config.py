"""설정 로딩 유틸.

config/qc_config.yaml 을 읽어 dict 로 돌려준다. 파일이 없거나 일부 키가 빠져도
DEFAULT_CONFIG 로 채워 넣어 항상 동작하도록 한다(현장 운영 중 설정 실수 방지).
"""

from __future__ import annotations

import copy
import os
from pathlib import Path

try:
    import yaml
except Exception:  # pragma: no cover - PyYAML 미설치 시에도 기본값으로 동작
    yaml = None

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "qc_config.yaml"

# yaml 이 없거나 키가 누락됐을 때 사용하는 기본값 (qc_config.yaml 과 동일 구조)
DEFAULT_CONFIG = {
    "site": {"name": "site", "timezone": "Asia/Seoul", "interval_minutes": "auto"},
    "preprocess": {
        "gdd_base": 10.0,
        "daily_min_completeness": 0.90,
        "drop_incomplete_days": False,
        "photoperiod_ppfd_threshold": 10,
        "daytime_hours": [9, 15],
        "lag_days": 0,
        "window_days": None,
    },
    "sensors": {
        "temp": {"label": "온도", "unit": "℃", "min": -40, "max": 80, "flat_minutes": 360, "spike": 5.0},
        "rh": {"label": "습도", "unit": "%", "min": 0, "max": 100, "flat_minutes": 360, "spike": 30.0},
        "soil_temp": {"label": "배지온도", "unit": "℃", "min": -40, "max": 60, "flat_minutes": 720, "spike": 4.0},
        # METER TEROS 는 '% Water Content'(체적수분 ×100) — m³/m³ 가 아니라 % 단위
        "vwc": {"label": "배지습도", "unit": "%", "min": 0.0, "max": 75.0, "flat_minutes": 720, "spike": 15.0},
        "ppfd": {"label": "PPFD", "unit": "µmol m-2 s-1", "min": 0, "max": 2500, "flat_minutes": 180, "spike": 1500},
        "solar": {"label": "일사량", "unit": "W/m²", "min": 0, "max": 1400, "flat_minutes": 180, "spike": 800},
        "ec": {"label": "EC", "unit": "mS/cm", "min": 0.0, "max": 20.0, "flat_minutes": 720, "spike": 3.0},
        "co2": {"label": "CO2", "unit": "ppm", "min": 250, "max": 3000, "flat_minutes": 360, "spike": 500},
    },
    "qc": {
        "gap_warn_minutes": 60,
        "gap_critical_minutes": 360,
        "missing_warn_ratio": 0.10,
        "missing_critical_ratio": 0.50,
        "flatline_enabled": True,
        "flatline_ignore_zero": ["ppfd", "solar", "ec"],
        "flatline_light_floor": 10,
        "spike_enabled": True,
        "spike_warn_count": 3,
        "daytime_dark_ppfd_max": 20,
        "heat_event_temp": 45,
        "heat_event_soil_temp": 40,
        "night_light_ppfd": 5,
        "night_hours": [23, 3],
        "night_light_enabled": False,
        "rh_saturated_hours": 12,
        "offline_warn_minutes": 120,
        "offline_critical_minutes": 720,
        "pair_divergence_enabled": False,
        "pair_divergence": {"temp": 1.0, "soil_temp": 1.0, "rh": 5.0, "vwc": 5.0, "ppfd": 0.15},
        "transmittance": {
            "enabled": True,
            "solar_to_ppfd_factor": 2.057,
            "drop_ratio": 0.70,
            "baseline_days": 30,
            "recent_days": 3,
        },
    },
    "alerts": {
        "min_level": "WARN",
        "cooldown_hours": 12,
        "state_file": "outputs/alert_state.json",
        "report_dir": "outputs/reports",
        "channels": {"console": True, "file": True, "slack": False, "email": False},
        "email": {"sender": "noreply@example.org", "recipients": [], "subject_prefix": "[환경데이터 QC]"},
    },
    "verification": {
        "log_file": "outputs/sensor_verification_log.csv",
        "schedule_days": {
            "visual_check": 7,
            "cross_check": 30,
            "reference_check": 90,
            "factory_calibration": 730,
        },
        "tolerance": {
            "temp": 0.5, "rh": 3.0, "soil_temp": 0.5, "vwc": 3.0,   # vwc 는 % 단위
            "ec": 0.3, "ppfd_rel": 0.05, "solar_rel": 0.05,
        },
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """override 값으로 base 를 재귀 병합(누락 키는 base 유지)."""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | os.PathLike | None = None) -> dict:
    """설정 파일을 읽어 기본값과 병합한 dict 반환."""
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    user_cfg = {}
    if yaml is not None and path.exists():
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f) or {}
    cfg = _deep_merge(DEFAULT_CONFIG, user_cfg)
    cfg["_path"] = str(path)
    return cfg


def resolve_path(cfg: dict, value: str) -> Path:
    """설정 내 상대경로를 프로젝트 루트 기준 절대경로로 변환."""
    p = Path(value)
    return p if p.is_absolute() else PROJECT_ROOT / p
