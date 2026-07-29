#!/usr/bin/env python3
"""10분 환경데이터 → 생육조사 구간(7·10일) 시차 매칭 전처리 CLI.

사용 예
-------
# 1) 환경자료만 → 일별 요약
python scripts/run_preprocess.py --env data/z6-21068_*.xlsx --out outputs/

# 2) 생육자료와 구간 매칭 (조사간격 자동 추정)
python scripts/run_preprocess.py --env "data/*.xlsx" --growth data/growth.csv --out outputs/

# 3) 시차 3일 적용 + 고정 10일창
python scripts/run_preprocess.py --env "data/*.xlsx" --growth data/growth.csv \
       --lag-days 3 --window-days 10 --out outputs/

산출물
------
  daily_env_summary.csv      일별 환경 요약(레코드 완전성 포함)
  env_interval_summary.csv   생육 구간별 환경 요약(시차 반영)
  merged_env_growth.csv      생육 + 구간환경 병합 (분석 투입용)
  preprocess_report.xlsx     위 3종 + 열 매핑 + 결측구간 리포트
"""

from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import io_logger, preprocess, sensor_map   # noqa: E402
from src.config import load_config                  # noqa: E402


def expand_paths(patterns: list[str]) -> list[str]:
    paths: list[str] = []
    for p in patterns:
        hits = sorted(glob.glob(p))
        paths.extend(hits if hits else [p])
    return paths


def read_growth(path: str, date_col: str) -> pd.DataFrame:
    suffix = Path(path).suffix.lower()
    df = pd.read_excel(path) if suffix in (".xlsx", ".xls") else pd.read_csv(path)
    if date_col not in df.columns:
        raise ValueError(f"생육 자료에 '{date_col}' 열이 없습니다. 실제 열: {list(df.columns)}")
    df[date_col] = pd.to_datetime(df[date_col])
    return df


def resolve_survey_dates(args, growth=None, date_col="date"):
    """조사일을 정한다: 사용자 지정 > 생육파일. 없으면 None."""
    if any([args.survey_dates, args.survey_start]):
        dates = preprocess.parse_survey_dates(
            dates=args.survey_dates, start=args.survey_start,
            interval=args.survey_interval, count=args.survey_count, end=args.survey_end)
        print(f"    조사일 {len(dates)}회 (사용자 지정): "
              f"{', '.join(d.strftime('%Y-%m-%d') for d in dates[:4])}"
              f"{' …' if len(dates) > 4 else ''}")
        return pd.Series(dates)
    if growth is not None and date_col in growth.columns:
        return growth[date_col]
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="환경(10분) → 생육구간 시차 매칭 전처리")
    ap.add_argument("--env", nargs="+", required=True, help="환경 로거 파일 경로/글롭")
    ap.add_argument("--growth", help="생육 조사 파일(csv/xlsx)")
    ap.add_argument("--growth-date-col", default="date", help="생육 자료의 조사일 열 이름(기본 date)")
    ap.add_argument("--out", default="outputs", help="산출물 디렉터리")
    ap.add_argument("--config", default=None, help="설정 파일 경로")
    ap.add_argument("--lag-days", type=int, default=None, help="시차(일). 환경 구간을 N일 앞당김")
    ap.add_argument("--window-days", type=int, default=None,
                    help="고정 창 길이(일). 지정하지 않으면 직전 조사일 다음날~당일 가변구간")
    ap.add_argument("--gdd-base", type=float, default=None, help="적산온도 기준온도(℃)")
    ap.add_argument("--first-start", default=None, help="첫 구간 시작일(정식일 등, YYYY-MM-DD)")
    ap.add_argument("--replicate", choices=["first", "mean", "keep"], default="first",
                    help="중복 센서 처리: first(기본)/mean(평균)/keep(모두 보존)")
    ap.add_argument("--drop-incomplete-days", action="store_true",
                    help="레코드 완전성 기준 미달일을 구간 집계에서 제외")
    ap.add_argument("--fill-missing", choices=["keep", "interpolate", "drop"], default=None,
                    help="집계 전 결측 처리: keep(그대로) / interpolate(짧은 결측 선형보간) / drop(결측행 삭제)")
    ap.add_argument("--fill-limit-minutes", type=float, default=None,
                    help="선형보간으로 채울 최대 연속 결측 시간(분). 기본 60 — 이보다 긴 구멍은 결측 유지")
    ap.add_argument("--keep-out-of-range", action="store_true",
                    help="센서 물리범위 이탈값을 결측 처리하지 않고 그대로 집계(기본은 결측 처리)")
    ap.add_argument("--by-treatment", action="store_true",
                    help="센서↔처리구 매핑을 적용해 처리구별로 집계·병합(한 로거의 센서가 서로 다른 처리구일 때)")
    ap.add_argument("--sensor-map", default=None, help="매핑 파일 경로(기본 config/sensor_map.yaml)")
    ap.add_argument("--growth-trt-col", default="trt", help="생육 자료의 처리구 열 이름(기본 trt)")
    # --- 조사일 기준: 생육파일 없이도 사용자가 직접 지정할 수 있다 -------
    ap.add_argument("--survey-dates", help='조사일 직접 지정 (예: "2026-04-01,2026-04-11,2026-04-21")')
    ap.add_argument("--survey-start", help="조사 시작일 (예: 2026-04-01)")
    ap.add_argument("--survey-interval", type=int, help="조사 간격(일). 예: 7 또는 10")
    ap.add_argument("--survey-count", type=int, help="조사 횟수")
    ap.add_argument("--survey-end", help="조사 종료일(횟수 대신 사용)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    pcfg = cfg["preprocess"]
    lag_days = args.lag_days if args.lag_days is not None else int(pcfg.get("lag_days", 0))
    window_days = args.window_days if args.window_days is not None else pcfg.get("window_days")
    gdd_base = args.gdd_base if args.gdd_base is not None else float(pcfg.get("gdd_base", 10.0))
    interval = None                     # 자료에서 자동 추정(설정이 숫자면 그 값)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- 1) 환경자료 읽기 · 표준화 ------------------------------------
    paths = expand_paths(args.env)
    print(f"[1/4] 환경 파일 {len(paths)}개 읽는 중...")
    raw, log = io_logger.load_env_files(paths)
    for line in log:
        print("   ", line)

    ts_df, ts_report = io_logger.prepare_timestamp(raw)
    from src import qc_rules as _qc
    interval = _qc.resolve_interval(cfg, ts_df)
    print(f"    시간 열: {ts_report['timestamp_column']} | 기록 간격 {interval:g}분(자동 인식) | "
          f"중복 제거 {ts_report['duplicate_rows']:,}건")
    print(f"    기간 {ts_report['start']} ~ {ts_report['end']}")

    std, map_report = io_logger.standardize(ts_df, replicate=args.replicate)
    print("    표준화된 변수:", ", ".join(c for c in std.columns if c != "timestamp"))

    grid, gap_report = io_logger.reindex_full_grid(std, interval_minutes=interval)
    n_missing = int((grid["qc_status"] == "missing_timestamp_inserted").sum())
    print(f"    {interval:g}분 격자 정합: 전체 {len(grid):,}행 중 결측 timestamp {n_missing:,}건 "
          f"({n_missing / max(len(grid), 1):.1%})")

    # --- 2) 범위 이탈값 결측 처리 + 일별 요약 ----------------------------
    print("[2/4] 일별 요약 생성 중...")
    clean = grid.drop(columns=["qc_status"])
    if not args.keep_out_of_range:
        clean, range_report = preprocess.mask_out_of_range(clean, cfg["sensors"])
        if not range_report.empty:
            print("    범위 이탈값 결측 처리:")
            for _, r in range_report.iterrows():
                print(f"      - {r['변수']}: {r['결측처리건수']:,}건 (허용 {r['허용범위']})")
    else:
        range_report = pd.DataFrame()

    # 결측 처리는 범위 이탈값을 지운 뒤에 한다(-99.9 를 먼저 결측으로 만든 다음 채워야 함)
    fill_method = args.fill_missing or str(pcfg.get("missing_method", "keep"))
    fill_limit = (args.fill_limit_minutes if args.fill_limit_minutes is not None
                  else float(pcfg.get("missing_limit_minutes", 60)))
    n_rows_before = len(clean)
    clean, missing_report = preprocess.fill_missing(
        clean, method=fill_method, limit_minutes=fill_limit, interval_minutes=interval)
    if fill_method == "interpolate" and not missing_report.empty:
        n_filled = int(missing_report["채운건수"].sum())
        n_left = int(missing_report["남은결측"].sum())
        print(f"    결측 선형보간: {n_filled:,}건 채움 (≤{fill_limit:g}분), "
              f"{n_left:,}건은 구멍이 길어 결측 유지")
    elif fill_method == "drop":
        print(f"    결측 행 삭제: {n_rows_before - len(clean):,}행 제거 "
              f"({(n_rows_before - len(clean)) / max(n_rows_before, 1):.1%})")

    daily_kwargs = dict(
        interval_minutes=interval,
        gdd_base=gdd_base,
        photoperiod_ppfd_threshold=float(pcfg.get("photoperiod_ppfd_threshold", 10)),
        daytime_hours=tuple(pcfg.get("daytime_hours", [9, 15])),
        min_completeness=float(pcfg.get("daily_min_completeness", 0.9)),
    )

    # 처리구별 집계: 한 로거의 센서들이 서로 다른 처리구를 잴 때
    by_trt, map_coverage = False, pd.DataFrame()
    if args.by_treatment:
        smap = sensor_map.load_sensor_map(args.sensor_map)
        entry = sensor_map.resolve_logger(smap, Path(paths[0]).name)
        if not entry or not entry.get("treatments"):
            print(f"    ⚠ 매핑 없음({Path(paths[0]).name}) — 처리구 구분 없이 진행합니다. "
                  f"config/sensor_map.yaml 을 확인하세요.")
        else:
            frames = sensor_map.split_by_treatment(clean, entry)
            map_coverage = sensor_map.coverage_report(clean, entry)
            daily = preprocess.to_daily_by_treatment(frames, **daily_kwargs)
            by_trt = True
            print(f"    처리구 {len(frames)}개로 분리: {', '.join(frames)} "
                  f"(공통변수: {', '.join(entry.get('shared', [])) or '없음'})")
            unmapped = map_coverage[map_coverage["매핑"] == "미매핑"]["열"].tolist()
            if unmapped:
                print(f"    ⚠ 매핑되지 않은 열: {', '.join(unmapped)}")

    if not by_trt:
        daily = preprocess.to_daily(clean, **daily_kwargs)

    daily.to_csv(out_dir / "daily_env_summary.csv", index=False, encoding="utf-8-sig")
    n_incomplete = int((~daily["is_complete"]).sum()) if "is_complete" in daily else 0
    n_days = daily["date"].nunique() if "date" in daily else 0
    print(f"    일별 {n_days}일{' × 처리구 ' + str(daily['trt'].nunique()) if by_trt else ''} "
          f"(불완전일 {n_incomplete}건) → daily_env_summary.csv")

    intervals = env_interval = merged = None

    # --- 3) 구간 매칭 (조사일 기준: 사용자 지정 또는 생육파일) --------------
    growth = read_growth(args.growth, args.growth_date_col) if args.growth else None
    survey_dates = resolve_survey_dates(args, growth, args.growth_date_col)

    if survey_dates is not None:
        print("[3/4] 조사일 구간 매칭 중...")
        cadence = preprocess.detect_cadence(survey_dates)
        print(f"    조사일 {pd.Series(survey_dates).nunique()}회, 추정 조사간격 {cadence}일")

        intervals = preprocess.build_intervals(
            survey_dates,
            first_start=args.first_start, lag_days=lag_days, window_days=window_days)
        drop_incomplete = args.drop_incomplete_days or bool(pcfg.get("drop_incomplete_days"))
        if growth is None:
            env_interval = (preprocess.aggregate_intervals_by_treatment(daily, intervals, drop_incomplete)
                            if by_trt else preprocess.aggregate_intervals(daily, intervals, drop_incomplete))
            merged = None
        elif by_trt:
            env_interval = preprocess.aggregate_intervals_by_treatment(daily, intervals, drop_incomplete)
            trt_col = args.growth_trt_col if args.growth_trt_col in growth.columns else None
            if trt_col is None:
                print(f"    ⚠ 생육 자료에 '{args.growth_trt_col}' 열이 없어 처리구 병합을 건너뜁니다. "
                      f"실제 열: {list(growth.columns)}")
            else:
                missing = set(growth[trt_col].astype(str)) - set(env_interval["trt"].astype(str))
                if missing:
                    print(f"    ⚠ 매핑에 없는 생육 처리구: {', '.join(sorted(missing))} "
                          f"→ config/sensor_map.yaml 의 처리구명을 생육자료와 일치시키세요.")
            merged = preprocess.match_growth(growth, env_interval,
                                             date_col=args.growth_date_col, trt_col=trt_col)
        else:
            env_interval = preprocess.aggregate_intervals(daily, intervals, drop_incomplete)
            merged = preprocess.match_growth(growth, env_interval, date_col=args.growth_date_col)

        env_interval.to_csv(out_dir / "env_interval_summary.csv", index=False, encoding="utf-8-sig")
        if merged is not None:
            merged.to_csv(out_dir / "merged_env_growth.csv", index=False, encoding="utf-8-sig")
        made = "env_interval_summary.csv" + (", merged_env_growth.csv" if merged is not None else "")
        print(f"    구간 {len(env_interval)}개 (시차 {lag_days}일"
              f"{', 고정창 ' + str(window_days) + '일' if window_days else ''})"
              f" → {made}")
        bad = env_interval[env_interval["quality_flag"] != "정상"] if "quality_flag" in env_interval else pd.DataFrame()
        if not bad.empty:
            print(f"    ⚠ 품질 주의 구간 {len(bad)}개:")
            for _, r in bad.iterrows():
                print(f"      - 구간{r['interval_id']} ({r['env_start']:%m-%d}~{r['env_end']:%m-%d}): {r['quality_flag']}")
    else:
        print("[3/4] 조사일 미지정 — 일별 요약까지만 생성 "
              "(--survey-start/--survey-interval/--survey-count 또는 --growth 로 지정)")

    # --- 4) Excel 리포트 -------------------------------------------------
    print("[4/4] Excel 리포트 작성 중...")
    xlsx_path = out_dir / "preprocess_report.xlsx"
    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        daily.to_excel(writer, sheet_name="daily_env_summary", index=False)
        if env_interval is not None:
            env_interval.to_excel(writer, sheet_name="env_interval_summary", index=False)
            intervals.to_excel(writer, sheet_name="interval_definition", index=False)
            if merged is not None:
                merged.to_excel(writer, sheet_name="merged_env_growth", index=False)
        map_report.to_excel(writer, sheet_name="column_mapping", index=False)
        if not map_coverage.empty:
            map_coverage.to_excel(writer, sheet_name="treatment_mapping", index=False)
        (range_report if not range_report.empty else pd.DataFrame({"note": ["범위 이탈값 없음"]})) \
            .to_excel(writer, sheet_name="out_of_range", index=False)
        (gap_report if not gap_report.empty else pd.DataFrame({"note": ["결측 timestamp 없음"]})) \
            .to_excel(writer, sheet_name="missing_timestamp", index=False)
        (missing_report if not missing_report.empty else pd.DataFrame({"note": ["결측 처리 없음"]})) \
            .to_excel(writer, sheet_name="missing_handling", index=False)
        pd.DataFrame([{
            "항목": "파일 수", "값": len(paths)}, {
            "항목": "원자료 행", "값": len(raw)}, {
            "항목": "중복 timestamp", "값": ts_report["duplicate_rows"]}, {
            "항목": "기간 시작", "값": str(ts_report["start"])}, {
            "항목": "기간 종료", "값": str(ts_report["end"])}, {
            "항목": "결측 timestamp", "값": n_missing}, {
            "항목": "일수", "값": len(daily)}, {
            "항목": "불완전일", "값": n_incomplete}, {
            "항목": "시차(일)", "값": lag_days}, {
            "항목": "고정창(일)", "값": window_days or "가변(직전 조사일~당일)"}, {
            "항목": "GDD 기준온도", "값": gdd_base}, {
            "항목": "결측 처리", "값": preprocess.MISSING_METHOD_LABELS.get(fill_method, fill_method)}, {
            "항목": "보간 한계(분)", "값": fill_limit if fill_method == "interpolate" else "-"},
        ]).to_excel(writer, sheet_name="run_summary", index=False)
    print(f"    → {xlsx_path}")
    print("완료.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
