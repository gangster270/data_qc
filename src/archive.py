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

from . import io_logger, preprocess, qc_rules, registry, sensor_map

MASTER_NAME = "env_master.csv"
CLEAN_NAME = "env_master_clean.csv"
SUMMARY_NAME = "archive_summary.csv"

ID_COLS = ["logger", "timestamp"]        # logger = 구역 이름(미지정 시 일련번호)


def read_one(source, filename: str, cfg: dict, replicate: str = "first",
             reg: dict | None = None) -> tuple[pd.DataFrame, dict]:
    """파일 1개를 읽어 (구역, timestamp, 표준 변수) 형태로 만든다.

    구역은 **파일 안에서 찾은 로거 일련번호**로 정한다(파일명이 매번 달라져도
    같은 로거는 같은 구역으로 묶인다). 등록부에 구역 이름이 지정돼 있으면
    그 이름을, 없으면 일련번호를 그대로 쓴다.
    """
    raw = io_logger.read_env_file(source, filename)
    serial = str(raw["_serial"].iloc[0]) if "_serial" in raw.columns else \
        (io_logger.serial_from_filename(filename) or filename)
    model = registry.guess_model(raw.columns)

    ts_df, ts_report = io_logger.prepare_timestamp(raw)
    std, map_report = io_logger.standardize(ts_df, replicate=replicate)

    # 로거마다 기록 시각이 몇 초씩 다르다(HOBO 20:00:01 vs ZL6 20:00:00).
    # 같은 구역의 다른 로거와 한 행으로 묶이려면 기록 간격 격자에 맞춰야 한다.
    interval = qc_rules.resolve_interval(cfg, ts_df)
    snapped = std["timestamp"].dt.round(f"{int(round(interval * 60))}s")
    n_snap = int((snapped != std["timestamp"]).sum())
    std["timestamp"] = snapped

    zone = registry.zone_of(reg or {"loggers": {}}, serial)
    std.insert(0, "logger", zone)
    std.insert(1, "serial", serial)
    info = {
        "파일": filename,
        "구역": zone,
        "일련번호": serial,
        "기종": model,
        "행수": int(len(std)),
        "시작": ts_report["start"],
        "종료": ts_report["end"],
        "기록간격(분)": interval,
        "시각정렬": n_snap,
        "시간열": ts_report["timestamp_column"],
        "중복ts": int(ts_report["duplicate_rows"]),
        "시트": ", ".join(sorted(set(raw["_sheet"].dropna().astype(str)))) if "_sheet" in raw else "",
        "변수": ", ".join(c for c in std.columns if c not in ID_COLS + ["serial"]),
        "mapping": map_report,
    }
    return std, info


def resolve_zone_collisions(frames: list[pd.DataFrame]) -> tuple[list[pd.DataFrame], pd.DataFrame]:
    """한 구역에 로거가 여러 대일 때, 같은 이름의 변수 열을 분리한다.

    예) 3구역에 ZL6(ppfd)와 HOBO(ppfd)가 함께 있으면 뒤에 오는 쪽을
    `ppfd__rep2` 로 밀어 둘 다 살린다. 한 대뿐인 구역은 이름이 그대로 유지된다.
    """
    owner_of: dict[tuple[str, str], str] = {}   # (구역, 열) → 소유 일련번호
    out, notes = [], []
    for df in frames:
        if "logger" not in df.columns or df.empty:
            out.append(df)
            continue
        zone = str(df["logger"].iloc[0])
        serial = str(df["serial"].iloc[0]) if "serial" in df.columns else ""
        value_cols = [c for c in df.columns if c not in ID_COLS + ["serial", "qc_status"]]

        rename: dict[str, str] = {}
        # 이 프레임이 최종적으로 갖게 될 이름들(자기 자신과도 겹치면 안 된다)
        taken_here = set(value_cols)
        for col in value_cols:
            owner = owner_of.get((zone, col))
            if owner is None or owner == serial:
                continue                              # 비어 있거나 내 것 → 그대로
            base = col.split("__rep")[0]
            n = 2
            while (zone, f"{base}__rep{n}") in owner_of or f"{base}__rep{n}" in taken_here:
                n += 1
            new = f"{base}__rep{n}"
            rename[col] = new
            taken_here.discard(col)
            taken_here.add(new)
            notes.append({"구역": zone, "일련번호": serial, "원래 열": col, "바뀐 열": new})

        for name in taken_here:                       # 최종 이름을 구역 소유로 등록
            owner_of.setdefault((zone, name), serial)
        out.append(df.rename(columns=rename) if rename else df)
    return out, pd.DataFrame(notes)


def merge_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """여러 프레임을 열의 합집합으로 합치고, 구역·시각 기준으로 정리한다.

    같은 (구역, 시각) 행이 여러 개면 **값이 있는 쪽을 살려** 한 행으로 합친다
    (재내려받기 겹침·분할 파일 경계·같은 구역의 다른 로거에서 흔함).
    """
    if not frames:
        return pd.DataFrame(columns=ID_COLS)
    merged = pd.concat(frames, ignore_index=True, sort=False)
    merged["timestamp"] = pd.to_datetime(merged["timestamp"], errors="coerce")
    merged = merged.dropna(subset=["timestamp"])
    merged = merged.sort_values(ID_COLS, kind="stable")
    # groupby.first() 는 NaN 을 건너뛰므로 결측이 있는 쪽이 다른 쪽 값으로 채워진다
    serials = None
    if "serial" in merged.columns:
        serials = (merged.groupby(ID_COLS)["serial"]
                   .agg(lambda s: "+".join(sorted(set(map(str, s.dropna())))))
                   .rename("serial"))
        merged = merged.drop(columns=["serial"])

    merged = merged.groupby(ID_COLS, as_index=False, sort=True).first()
    if serials is not None:
        merged = merged.merge(serials.reset_index(), on=ID_COLS, how="left")
    value_cols = [c for c in merged.columns if c not in ID_COLS + ["serial"]]
    head = ID_COLS + (["serial"] if "serial" in merged.columns else [])
    return merged[head + sorted(value_cols, key=_col_order)]


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
        df = sub.drop(columns=[c for c in ("logger", "serial") if c in sub.columns]).reset_index(drop=True)
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
        serials = sorted({s for cell in sub.get("serial", pd.Series(dtype=str)).dropna().astype(str)
                          for s in cell.split("+")})
        interval = qc_rules.resolve_interval(cfg, sub)
        cl = clean[clean["logger"] == logger_id] if not clean.empty else pd.DataFrame()
        n_missing = int((cl["qc_status"] == "missing_timestamp_inserted").sum()) if len(cl) else 0
        vars_here = [c for c in qc_rules.value_columns(sub) if sub[c].notna().any()]
        span_days = (sub["timestamp"].max() - sub["timestamp"].min()).days + 1
        rows.append({
            "구역": logger_id,
            "로거번호": ", ".join(serials),
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
                  replicate: str = "first", update: bool = True,
                  registry_path: str | Path | None = None) -> dict:
    """파일 목록을 읽어 통합 아카이브를 만들고 저장한다.

    sources: 경로 문자열 리스트 또는 (buffer, filename) 튜플 리스트
    update : True 면 out_dir 에 있는 기존 마스터에 이어붙인다(기본).

    반환 dict: master / clean / summary / files / range_report / log
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    reg = registry.load_registry(registry_path)

    frames, infos, log = [], [], []
    for s in sources:
        try:
            if isinstance(s, tuple):
                df, info = read_one(s[0], s[1], cfg, replicate, reg=reg)
            else:
                df, info = read_one(s, Path(s).name, cfg, replicate, reg=reg)
            frames.append(df)
            infos.append({k: v for k, v in info.items() if k != "mapping"})
            # 등록부 갱신: 새 일련번호는 자동 등록되고, 구역 이름은 사람이 채운다
            registry.touch(reg, info["일련번호"], model=info.get("기종", ""),
                           filename=info["파일"], first=f"{info['시작']:%Y-%m-%d}",
                           last=f"{info['종료']:%Y-%m-%d}")
            sheets = f", 시트 {info['시트']}" if info.get("시트") else ""
            if info.get("시각정렬"):
                sheets += f", 시각정렬 {info['시각정렬']:,}건"
            log.append(f"읽음: {info['파일']} → [{info['구역']}] 번호 {info['일련번호']} "
                       f"({info['행수']:,}행, {info['기록간격(분)']:g}분, "
                       f"{info['시작']:%Y-%m-%d}~{info['종료']:%Y-%m-%d}{sheets})")
        except Exception as e:                       # 한 파일 실패가 전체를 막지 않게
            name = s[1] if isinstance(s, tuple) else str(s)
            log.append(f"실패: {name} → {e}")

    frames, collision_report = resolve_zone_collisions(frames)
    if not collision_report.empty:
        for _, r in collision_report.iterrows():
            log.append(f"구역 내 열 충돌 정리: [{r['구역']}] {r['일련번호']} "
                       f"{r['원래 열']} → {r['바뀐 열']}")

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

    registry.save_registry(reg, registry_path)
    unnamed = [s for s, e in (reg.get("loggers") or {}).items() if not str((e or {}).get("zone", "")).strip()]
    if unnamed:
        log.append(f"구역 미지정 로거 {len(unnamed)}대: {', '.join(unnamed)} "
                   f"→ config/logger_registry.yaml 에서 zone 을 채우면 이후 자동으로 묶입니다.")

    master.to_csv(out_dir / MASTER_NAME, index=False, encoding="utf-8-sig")
    clean.to_csv(out_dir / CLEAN_NAME, index=False, encoding="utf-8-sig")
    summary.to_csv(out_dir / SUMMARY_NAME, index=False, encoding="utf-8-sig")

    log.append(f"통합 결과: {len(master):,}행 "
               f"(신규 {n_new_rows:,} + 기존 {prev_rows:,} → 중복 정리 "
               f"{n_new_rows + prev_rows - len(master):,}행 병합)")
    return {
        "master": master, "clean": clean, "summary": summary,
        "files": pd.DataFrame(infos), "range_report": range_report,
        "collisions": collision_report, "registry": reg,
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
        df = (sub.drop(columns=[c for c in ("logger", "serial") if c in sub.columns])
                 .dropna(axis=1, how="all").reset_index(drop=True))
        yield str(logger_id), df
