"""보유한 모든 환경데이터를 하나로 모으는 통합 아카이브.

여러 로거·여러 기종·여러 번 내려받은 파일을 한 폴더에 넣고 실행하면
  1) 형식(엑셀/CSV·구분자·인코딩·헤더 위치)을 자동 판별해 읽고
  2) 시간 열(또는 날짜+시간)을 찾아 **날짜순으로 정렬**하고
  3) 변수명을 표준 키(temp/rh/soil_temp/vwc/ppfd/solar/ec …)로 통일하고
     — 표준에 없는 변수(수온·pH·풍속 등)도 이름만 정리해 그대로 보존 —
  4) 로거별로 **중복 시각을 합치고**(재내려받기 겹침 정리)
  5) 원자료 마스터와 QC 적용 마스터를 각각 남긴다.

새 파일을 받았을 때 `update=True` 로 다시 돌리면 기존 마스터에 **이어붙인다**
(같은 로거·같은 시각은 값이 있는 쪽을 살려 한 행으로 합침).

산출 파일
  env_master.csv        원자료 통합 (정렬·중복정리·변수 표준화)  ← 원본 보존
  env_master_clean.csv  범위 이탈값 결측 처리 + 기록간격 격자 정합 ← 분석·모니터링용
  archive_summary.csv   로거별 기간·행수·간격·변수·결측 요약
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from . import io_logger, preprocess, qc_rules, sensor_map

MASTER_NAME = "env_master.csv"
CLEAN_NAME = "env_master_clean.csv"
SUMMARY_NAME = "archive_summary.csv"

ID_COLS = ["logger", "timestamp"]


def _logger_id(name: str) -> str:
    return sensor_map.logger_id_from_filename(name)


def read_one(source, filename: str, cfg: dict, replicate: str = "first") -> tuple[pd.DataFrame, dict]:
    """파일 1개를 읽어 (로거ID, timestamp, 표준 변수) 형태로 만든다."""
    raw = io_logger.read_env_file(source, filename)
    ts_df, ts_report = io_logger.prepare_timestamp(raw)
    std, map_report = io_logger.standardize(ts_df, replicate=replicate)
    std.insert(0, "logger", _logger_id(filename))
    info = {
        "파일": filename,
        "로거": _logger_id(filename),
        "행수": int(len(std)),
        "시작": ts_report["start"],
        "종료": ts_report["end"],
        "기록간격(분)": qc_rules.resolve_interval(cfg, ts_df),
        "시간열": ts_report["timestamp_column"],
        "중복ts": int(ts_report["duplicate_rows"]),
        "변수": ", ".join(c for c in std.columns if c not in ID_COLS),
        "mapping": map_report,
    }
    return std, info


def merge_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """여러 프레임을 열의 합집합으로 합치고, 로거·시각 기준으로 정리한다.

    같은 (로거, 시각) 행이 여러 개면 **값이 있는 쪽을 살려** 한 행으로 합친다
    (재내려받기 겹침·분할 파일 경계에서 흔함).
    """
    if not frames:
        return pd.DataFrame(columns=ID_COLS)
    merged = pd.concat(frames, ignore_index=True, sort=False)
    merged["timestamp"] = pd.to_datetime(merged["timestamp"], errors="coerce")
    merged = merged.dropna(subset=["timestamp"])
    merged = merged.sort_values(ID_COLS, kind="stable")
    # groupby.first() 는 NaN 을 건너뛰므로 결측이 있는 쪽이 다른 쪽 값으로 채워진다
    merged = merged.groupby(ID_COLS, as_index=False, sort=True).first()
    value_cols = [c for c in merged.columns if c not in ID_COLS]
    return merged[ID_COLS + sorted(value_cols, key=_col_order)]


def _col_order(col: str) -> tuple:
    """표준 변수를 앞쪽에, 기타 변수를 뒤쪽에 두는 정렬 키."""
    base = str(col).split("__rep")[0]
    if base in io_logger.STANDARD_ORDER:
        return (0, io_logger.STANDARD_ORDER.index(base), str(col))
    return (1, 0, str(col))


def clean_master(master: pd.DataFrame, cfg: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """로거별로 범위 이탈값을 결측 처리하고 기록간격 격자에 맞춘다.

    반환: (정리된 마스터(qc_status 포함), 범위 이탈 처리 리포트)
    """
    parts, reports = [], []
    for logger_id, sub in master.groupby("logger", sort=True):
        df = sub.drop(columns=["logger"]).reset_index(drop=True)
        df = df.dropna(axis=1, how="all")            # 이 로거에 없는 변수 열 제거
        cleaned, rep = preprocess.mask_out_of_range(df, cfg["sensors"])
        if not rep.empty:
            rep.insert(0, "logger", logger_id)
            reports.append(rep)
        interval = qc_rules.resolve_interval(cfg, cleaned)
        grid, _ = io_logger.reindex_full_grid(cleaned, interval_minutes=interval)
        grid.insert(0, "logger", logger_id)
        parts.append(grid)
    if not parts:
        return pd.DataFrame(), pd.DataFrame()
    clean = pd.concat(parts, ignore_index=True, sort=False).sort_values(ID_COLS)
    return clean.reset_index(drop=True), (pd.concat(reports, ignore_index=True) if reports else pd.DataFrame())


def summarize(master: pd.DataFrame, clean: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """로거별 아카이브 현황표."""
    rows = []
    for logger_id, sub in master.groupby("logger", sort=True):
        interval = qc_rules.resolve_interval(cfg, sub)
        cl = clean[clean["logger"] == logger_id] if not clean.empty else pd.DataFrame()
        n_missing = int((cl["qc_status"] == "missing_timestamp_inserted").sum()) if len(cl) else 0
        vars_here = [c for c in qc_rules.value_columns(sub) if sub[c].notna().any()]
        span_days = (sub["timestamp"].max() - sub["timestamp"].min()).days + 1
        rows.append({
            "로거": logger_id,
            "시작": f"{sub['timestamp'].min():%Y-%m-%d}",
            "종료": f"{sub['timestamp'].max():%Y-%m-%d}",
            "기간(일)": span_days,
            "관측행": int(len(sub)),
            "기록간격(분)": interval,
            "격자행": int(len(cl)) if len(cl) else 0,
            "결측ts": n_missing,
            "결측률": round(n_missing / len(cl), 4) if len(cl) else 0.0,
            "변수수": len(vars_here),
            "변수": ", ".join(vars_here),
        })
    return pd.DataFrame(rows)


def build_archive(sources: list, cfg: dict, out_dir: str | Path,
                  replicate: str = "first", update: bool = True) -> dict:
    """파일 목록을 읽어 통합 아카이브를 만들고 저장한다.

    sources: 경로 문자열 리스트 또는 (buffer, filename) 튜플 리스트
    update : True 면 out_dir 에 있는 기존 마스터에 이어붙인다(기본).

    반환 dict: master / clean / summary / files / range_report / log
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    frames, infos, log = [], [], []
    for s in sources:
        try:
            if isinstance(s, tuple):
                df, info = read_one(s[0], s[1], cfg, replicate)
            else:
                df, info = read_one(s, Path(s).name, cfg, replicate)
            frames.append(df)
            infos.append({k: v for k, v in info.items() if k != "mapping"})
            log.append(f"읽음: {info['파일']} → {info['로거']} "
                       f"({info['행수']:,}행, {info['기록간격(분)']:g}분, "
                       f"{info['시작']:%Y-%m-%d}~{info['종료']:%Y-%m-%d})")
        except Exception as e:                       # 한 파일 실패가 전체를 막지 않게
            name = s[1] if isinstance(s, tuple) else str(s)
            log.append(f"실패: {name} → {e}")

    n_new_rows = sum(len(f) for f in frames)

    prev_path = out_dir / MASTER_NAME
    prev_rows = 0
    if update and prev_path.exists():
        prev = pd.read_csv(prev_path, parse_dates=["timestamp"])
        prev_rows = len(prev)
        frames.append(prev)
        log.append(f"기존 마스터 이어받기: {prev_rows:,}행")

    master = merge_frames(frames)
    clean, range_report = clean_master(master, cfg)
    summary = summarize(master, clean, cfg)

    master.to_csv(out_dir / MASTER_NAME, index=False, encoding="utf-8-sig")
    clean.to_csv(out_dir / CLEAN_NAME, index=False, encoding="utf-8-sig")
    summary.to_csv(out_dir / SUMMARY_NAME, index=False, encoding="utf-8-sig")

    log.append(f"통합 결과: {len(master):,}행 "
               f"(신규 {n_new_rows:,} + 기존 {prev_rows:,} → 중복 정리 "
               f"{n_new_rows + prev_rows - len(master):,}행 병합)")
    return {
        "master": master, "clean": clean, "summary": summary,
        "files": pd.DataFrame(infos), "range_report": range_report,
        "log": log, "out_dir": out_dir,
    }


def load_master(path: str | Path, clean: bool = False) -> pd.DataFrame:
    """저장된 마스터를 읽는다(clean=True 면 QC 적용본)."""
    path = Path(path)
    if path.is_dir():
        path = path / (CLEAN_NAME if clean else MASTER_NAME)
    df = pd.read_csv(path, parse_dates=["timestamp"])
    return df.sort_values([c for c in ID_COLS if c in df.columns]).reset_index(drop=True)


def iter_loggers(master: pd.DataFrame):
    """로거별 프레임을 (로거ID, DataFrame) 으로 순회한다(logger 열 제거)."""
    if "logger" not in master.columns:
        yield "(전체)", master
        return
    for logger_id, sub in master.groupby("logger", sort=True):
        df = sub.drop(columns=["logger"]).dropna(axis=1, how="all").reset_index(drop=True)
        yield str(logger_id), df
