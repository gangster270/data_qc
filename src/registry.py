"""로거 일련번호 ↔ 구역 이름 등록부.

같은 센서 로거는 내려받을 때마다 파일명이 달라져도(`260703_22094002.csv`,
`260710_22094002.csv` …) **일련번호는 그대로**다. 그 번호에 구역 이름을 한 번
지정해 두면 이후로는 자동으로 같은 구역으로 묶인다.

등록부 파일(config/logger_registry.yaml)
---------------------------------------
loggers:
  "22094002":
    zone: "3구역"          # ← 사람이 지정. 비어 있으면 일련번호를 그대로 구역명으로 사용
    model: "HOBO"
    note: "PAR 좌/우"
    first_seen: 2026-07-03
    last_seen: 2026-07-27
    files: 3

- 새 일련번호는 파일을 넣는 순간 자동 등록되고(zone 은 빈 값), 이름만 채우면 된다.
- 한 구역에 로거가 여러 대여도 된다(같은 zone 을 여러 일련번호에 지정).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

from .config import PROJECT_ROOT

DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "config" / "logger_registry.yaml"

HEADER = """# =====================================================================
# 로거 일련번호 ↔ 구역 이름 등록부
#
#   zone 에 구역 이름을 적으면, 그 번호의 파일은 이후 자동으로 같은 구역으로
#   묶인다(파일명이 매번 달라져도 무방). 비워 두면 일련번호가 그대로 쓰인다.
#   같은 구역에 로거가 여러 대면 zone 에 같은 이름을 적으면 된다.
#
#   지정:  python scripts/build_archive.py --zone "22094002=3구역" --list-zones
# =====================================================================
"""


def load_registry(path: str | Path | None = None) -> dict:
    """등록부를 읽는다(없으면 빈 등록부)."""
    path = Path(path) if path else DEFAULT_REGISTRY_PATH
    if yaml is None or not path.exists():
        return {"loggers": {}}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data.setdefault("loggers", {})
    return data


def save_registry(reg: dict, path: str | Path | None = None) -> Path:
    """등록부를 저장한다(설명 주석 유지)."""
    path = Path(path) if path else DEFAULT_REGISTRY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.dump(reg, allow_unicode=True, sort_keys=False, default_flow_style=False) \
        if yaml is not None else ""
    path.write_text(HEADER + body, encoding="utf-8")
    return path


def zone_of(reg: dict, serial: str) -> str:
    """일련번호의 구역 이름. 지정돼 있지 않으면 일련번호를 그대로 쓴다."""
    entry = (reg.get("loggers") or {}).get(str(serial)) or {}
    zone = str(entry.get("zone") or "").strip()
    return zone or str(serial)


def set_zone(reg: dict, serial: str, zone: str) -> dict:
    """구역 이름을 지정(또는 변경)한다."""
    loggers = reg.setdefault("loggers", {})
    entry = loggers.setdefault(str(serial), {})
    entry["zone"] = str(zone).strip()
    return reg


def touch(reg: dict, serial: str, *, model: str = "", filename: str = "",
          first: str = "", last: str = "") -> dict:
    """파일을 볼 때마다 등록부를 갱신한다(신규 번호는 자동 등록)."""
    loggers = reg.setdefault("loggers", {})
    entry = loggers.setdefault(str(serial), {})
    entry.setdefault("zone", "")
    if model and not entry.get("model"):
        entry["model"] = model
    entry.setdefault("first_seen", first or str(date.today()))
    if first and str(first) < str(entry.get("first_seen", first)):
        entry["first_seen"] = first
    if last:
        entry["last_seen"] = last
    entry["files"] = int(entry.get("files", 0)) + (1 if filename else 0)
    if filename:
        recent = list(entry.get("recent_files", []))
        recent = [f for f in recent if f != filename][-4:] + [filename]
        entry["recent_files"] = recent
    return reg


def guess_model(columns) -> str:
    """열 구성으로 로거 기종을 대략 추정한다(등록부 참고용)."""
    text = " ".join(str(c).lower() for c in columns)
    if "lgr s/n" in text or "sen s/n" in text:
        return "HOBO"
    if "port" in text and ("teros" in text or "water content" in text or "ppfd" in text):
        return "METER ZL6"
    return ""


def as_table(reg: dict):
    """등록부를 표로(대시보드·CLI 출력용)."""
    import pandas as pd
    rows = []
    for serial, e in (reg.get("loggers") or {}).items():
        rows.append({
            "일련번호": serial,
            "구역": (e or {}).get("zone", "") or "(미지정)",
            "기종": (e or {}).get("model", ""),
            "최초": (e or {}).get("first_seen", ""),
            "최근": (e or {}).get("last_seen", ""),
            "파일수": (e or {}).get("files", 0),
            "비고": (e or {}).get("note", ""),
        })
    return pd.DataFrame(rows).sort_values(["구역", "일련번호"]).reset_index(drop=True)
