#!/usr/bin/env python3
"""여러 로거를 한 번에 전처리하는 배치 CLI.

로거별로 파일을 묶어(파일명 첫 밑줄 앞부분 = 로거 ID) 각각 전처리한 뒤,
**모든 로거·처리구를 한 파일로 합친 통합 산출물**까지 만든다.

사용 예
-------
# 5개 로거 전체 → 일별 요약(처리구별) + 통합 파일
python scripts/run_all_loggers.py --env "data/*.xlsx" --by-treatment --out outputs/all

# 생육자료까지 있으면 구간 매칭·병합까지 한 번에
python scripts/run_all_loggers.py --env "data/*.xlsx" --growth data/growth.csv \
       --by-treatment --first-start 2026-04-01 --out outputs/all

산출물
------
  <로거>/daily_env_summary.csv        로거별 일별 요약
  <로거>/env_interval_summary.csv     로거별 구간 요약(생육자료 제공 시)
  all_loggers_daily.csv               전체 통합 일별(로거·처리구 열 포함)
  all_loggers_interval.csv            전체 통합 구간(생육자료 제공 시)
  all_loggers_merged.csv              생육 + 환경 병합(생육자료 제공 시)
  all_loggers_report.xlsx             위 전부 + 로거별 요약 + 처리구 목록
"""

from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import io_logger, preprocess, qc_rules, sensor_map   # noqa: E402
from src.config import load_config                            # noqa: E402


def group_by_logger(paths: list[str]) -> dict[str, list[str]]:
    """파일명 첫 밑줄 앞부분을 로거 ID 로 보고 묶는다."""
    groups: dict[str, list[str]] = {}
    for p in paths:
        groups.setdefault(sensor_map.logger_id_from_filename(Path(p).name), []).append(p)
    return dict(sorted(groups.items()))


def process_logger(logger_id: str, files: list[str], cfg: dict, args, smap: dict):
    """로거 1대를 전처리해 (일별, 구간, 병합, 요약) 을 돌려준다."""
    pcfg = cfg["preprocess"]
    raw, _ = io_logger.load_env_files(files)
    ts_df, ts_report = io_logger.prepare_timestamp(raw)
    interval = qc_rules.resolve_interval(cfg, ts_df)
    std, map_report = io_logger.standardize(ts_df, replicate=args.replicate)
    grid, gap_report = io_logger.reindex_full_grid(std, interval_minutes=interval)

    clean = grid.drop(columns=["qc_status"])
    range_report = pd.DataFrame()
    if not args.keep_out_of_range:
        clean, range_report = preprocess.mask_out_of_range(clean, cfg["sensors"])

    daily_kwargs = dict(
        interval_minutes=interval,
        gdd_base=args.gdd_base if args.gdd_base is not None else float(pcfg.get("gdd_base", 10.0)),
        photoperiod_ppfd_threshold=float(pcfg.get("photoperiod_ppfd_threshold", 10)),
        daytime_hours=tuple(pcfg.get("daytime_hours", [9, 15])),
        min_completeness=float(pcfg.get("daily_min_completeness", 0.9)),
    )

    entry = sensor_map.resolve_logger(smap, logger_id) if args.by_treatment else None
    if entry and entry.get("treatments"):
        frames = sensor_map.split_by_treatment(clean, entry)
        daily = preprocess.to_daily_by_treatment(frames, **daily_kwargs)
        treatments = list(frames)
    else:
        daily = preprocess.to_daily(clean, **daily_kwargs)
        treatments = []

    summary = {
        "로거": logger_id,
        "파일수": len(files),
        "기록간격(분)": interval,
        "시작": f"{ts_report['start']:%Y-%m-%d}",
        "종료": f"{ts_report['end']:%Y-%m-%d}",
        "일수": int(daily["date"].nunique()) if "date" in daily else 0,
        "행수": int(ts_report["n_rows"]),
        "중복ts": int(ts_report["duplicate_rows"]),
        "결측ts": int((grid["qc_status"] == "missing_timestamp_inserted").sum()),
        "처리구수": len(treatments),
        "처리구": ", ".join(treatments) if treatments else "(구분없음)",
        "변수": ", ".join(qc_rules.value_columns(clean)),
        "범위이탈처리": int(range_report["결측처리건수"].sum()) if not range_report.empty else 0,
        "불완전일": int((~daily["is_complete"]).sum()) if "is_complete" in daily else 0,
    }
    return daily, summary, gap_report, map_report


def main() -> int:
    ap = argparse.ArgumentParser(description="여러 로거 일괄 전처리")
    ap.add_argument("--env", nargs="+", default=["data/*.xlsx"], help="환경 파일 경로/글롭")
    ap.add_argument("--growth", help="생육 조사 파일(csv/xlsx)")
    ap.add_argument("--growth-date-col", default="date")
    ap.add_argument("--growth-trt-col", default="trt")
    ap.add_argument("--out", default="outputs/all", help="산출물 디렉터리")
    ap.add_argument("--config", default=None)
    ap.add_argument("--sensor-map", default=None)
    ap.add_argument("--by-treatment", action="store_true", help="센서↔처리구 매핑 적용")
    ap.add_argument("--replicate", choices=["first", "mean", "keep"], default="first")
    ap.add_argument("--lag-days", type=int, default=None)
    ap.add_argument("--window-days", type=int, default=None)
    ap.add_argument("--gdd-base", type=float, default=None)
    ap.add_argument("--first-start", default=None)
    ap.add_argument("--keep-out-of-range", action="store_true")
    ap.add_argument("--drop-incomplete-days", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    pcfg = cfg["preprocess"]
    smap = sensor_map.load_sensor_map(args.sensor_map)

    paths: list[str] = []
    for pattern in args.env:
        hits = sorted(glob.glob(pattern))
        paths.extend(hits if hits else [pattern])
    groups = group_by_logger(paths)
    if not groups:
        print("환경 파일을 찾지 못했습니다.", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"로거 {len(groups)}대 / 파일 {len(paths)}개 처리\n")

    daily_all, summaries, gaps_all = [], [], []
    for logger_id, files in groups.items():
        print(f"[{logger_id}] {len(files)}개 파일...")
        daily, summary, gap_report, _ = process_logger(logger_id, files, cfg, args, smap)
        summaries.append(summary)

        d = daily.copy()
        d.insert(0, "logger", logger_id)
        if "trt" not in d.columns:
            d.insert(1, "trt", "(구분없음)")
        daily_all.append(d)

        logger_dir = out_dir / logger_id
        logger_dir.mkdir(parents=True, exist_ok=True)
        daily.to_csv(logger_dir / "daily_env_summary.csv", index=False, encoding="utf-8-sig")
        if not gap_report.empty:
            gap_report.insert(0, "logger", logger_id)
            gaps_all.append(gap_report)

        print(f"    간격 {summary['기록간격(분)']:g}분 | {summary['시작']}~{summary['종료']} "
              f"({summary['일수']}일) | 처리구 {summary['처리구수']}개 | "
              f"결측ts {summary['결측ts']}건 → {logger_dir}/daily_env_summary.csv")

    combined_daily = pd.concat(daily_all, ignore_index=True)
    combined_daily.to_csv(out_dir / "all_loggers_daily.csv", index=False, encoding="utf-8-sig")
    summary_df = pd.DataFrame(summaries)

    combined_interval = combined_merged = intervals = None
    if args.growth:
        suffix = Path(args.growth).suffix.lower()
        growth = pd.read_excel(args.growth) if suffix in (".xlsx", ".xls") else pd.read_csv(args.growth)
        growth[args.growth_date_col] = pd.to_datetime(growth[args.growth_date_col])
        cadence = preprocess.detect_cadence(growth[args.growth_date_col])
        lag = args.lag_days if args.lag_days is not None else int(pcfg.get("lag_days", 0))
        window = args.window_days if args.window_days is not None else pcfg.get("window_days")
        intervals = preprocess.build_intervals(growth[args.growth_date_col],
                                               first_start=args.first_start,
                                               lag_days=lag, window_days=window)
        print(f"\n생육 조사 {growth[args.growth_date_col].nunique()}회 · 추정 간격 {cadence}일 · "
              f"시차 {lag}일 → 구간 {len(intervals)}개")

        drop_incomplete = args.drop_incomplete_days or bool(pcfg.get("drop_incomplete_days"))
        parts = []
        for logger_id, sub in combined_daily.groupby("logger", sort=True):
            d = sub.drop(columns=["logger"])
            if (d["trt"] != "(구분없음)").any():
                iv = preprocess.aggregate_intervals_by_treatment(d, intervals, drop_incomplete)
            else:
                iv = preprocess.aggregate_intervals(d.drop(columns=["trt"]), intervals, drop_incomplete)
                iv.insert(0, "trt", "(구분없음)")
            iv.insert(0, "logger", logger_id)
            parts.append(iv)
        combined_interval = pd.concat(parts, ignore_index=True)
        combined_interval.to_csv(out_dir / "all_loggers_interval.csv", index=False, encoding="utf-8-sig")

        trt_col = args.growth_trt_col if args.growth_trt_col in growth.columns else None
        if trt_col:
            missing = set(growth[trt_col].astype(str)) - set(combined_interval["trt"].astype(str))
            if missing:
                print(f"⚠ 매핑에 없는 생육 처리구: {', '.join(sorted(missing))} "
                      f"→ config/sensor_map.yaml 의 처리구명을 생육자료와 맞추세요.")
        combined_merged = preprocess.match_growth(growth, combined_interval,
                                                  date_col=args.growth_date_col, trt_col=trt_col)
        combined_merged.to_csv(out_dir / "all_loggers_merged.csv", index=False, encoding="utf-8-sig")

    # --- Excel 통합 리포트 ------------------------------------------------
    xlsx = out_dir / "all_loggers_report.xlsx"
    with pd.ExcelWriter(xlsx, engine="openpyxl") as w:
        summary_df.to_excel(w, sheet_name="logger_summary", index=False)
        combined_daily.to_excel(w, sheet_name="all_daily", index=False)
        if combined_interval is not None:
            intervals.to_excel(w, sheet_name="interval_definition", index=False)
            combined_interval.to_excel(w, sheet_name="all_interval", index=False)
            combined_merged.to_excel(w, sheet_name="merged_env_growth", index=False)
        (pd.concat(gaps_all, ignore_index=True) if gaps_all
         else pd.DataFrame({"note": ["결측 timestamp 없음"]})).to_excel(
            w, sheet_name="missing_timestamp", index=False)

    print("\n" + summary_df[["로거", "기록간격(분)", "시작", "종료", "일수", "처리구수",
                             "결측ts", "불완전일"]].to_string(index=False))
    print(f"\n통합 산출물 → {out_dir}/")
    print(f"  all_loggers_daily.csv     ({len(combined_daily):,}행, "
          f"처리구 {combined_daily['trt'].nunique()}개)")
    if combined_interval is not None:
        print(f"  all_loggers_interval.csv  ({len(combined_interval):,}행)")
        print(f"  all_loggers_merged.csv    ({len(combined_merged):,}행)")
    print(f"  all_loggers_report.xlsx")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
