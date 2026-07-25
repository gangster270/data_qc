#!/usr/bin/env python3
"""환경 로거 결측·센서오류 자동 모니터링 CLI (스케줄 실행용).

사용 예
-------
python scripts/run_monitor.py --env "data/*.xlsx"                 # 최근 7일 점검 + 알림
python scripts/run_monitor.py --env "data/*.xlsx" --lookback 3    # 최근 3일만
python scripts/run_monitor.py --env "data/*.xlsx" --dry-run       # 발송 없이 결과만 확인

cron 예 (매일 08:00):
  0 8 * * * cd /path/to/data_qc && python scripts/run_monitor.py --env "data/*.xlsx" >> outputs/monitor.log 2>&1

Cowork Routine 으로 돌리는 방법은 cowork/COWORK_ROUTINE.md 참조.
종료코드: 0=정상 / 1=WARN 있음 / 2=CRITICAL 있음  (CI·스케줄러에서 활용)
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import alerts as alert_mod            # noqa: E402
from src import archive, io_logger, qc_rules   # noqa: E402
from src.config import load_config             # noqa: E402


def expand_paths(patterns: list[str]) -> list[str]:
    paths: list[str] = []
    for p in patterns:
        hits = sorted(glob.glob(p))
        paths.extend(hits if hits else [p])
    return paths


def load_standardized(paths: list[str], cfg: dict, replicate: str = "first"):
    """파일들을 읽어 표준화 + 10분 격자 정합까지 마친 DataFrame 반환."""
    raw, log = io_logger.load_env_files(paths)
    ts_df, ts_report = io_logger.prepare_timestamp(raw)
    std, map_report = io_logger.standardize(ts_df, replicate=replicate)
    interval = qc_rules.resolve_interval(cfg, ts_df)      # 설정이 auto 면 자료에서 추정
    grid, gap_report = io_logger.reindex_full_grid(std, interval_minutes=interval)
    context = {
        "start": str(ts_report["start"]),
        "end": str(ts_report["end"]),
        "n_rows": int(ts_report["n_rows"]),
        "n_missing_ts": int((grid["qc_status"] == "missing_timestamp_inserted").sum()),
        "interval_minutes": interval,
        "n_files": len(paths),
        "duplicates": int(ts_report["duplicate_rows"]),
    }
    return grid, context, {"load_log": log, "mapping": map_report, "gaps": gap_report}


def main() -> int:
    ap = argparse.ArgumentParser(description="환경 로거 결측·센서오류 자동 모니터링")
    ap.add_argument("--env", nargs="+", help="환경 로거 파일 경로/글롭")
    ap.add_argument("--archive", help="통합 아카이브 디렉터리 또는 env_master.csv 경로")
    ap.add_argument("--by-logger", action="store_true",
                    help="로거별로 나눠 점검(아카이브에 여러 로거가 있을 때 권장)")
    ap.add_argument("--config", default=None, help="설정 파일 경로")
    ap.add_argument("--lookback", type=int, default=7, help="점검 대상 기간(일, 기본 7)")
    ap.add_argument("--replicate", choices=["first", "mean", "keep"], default="first")
    ap.add_argument("--dry-run", action="store_true", help="발송하지 않고 결과만 출력")
    ap.add_argument("--now", default=None, help="기준시각(테스트용, YYYY-MM-DD HH:MM)")
    ap.add_argument("--json", action="store_true", help="요약을 JSON 으로 출력")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.json:
        # stdout 을 JSON 전용으로 비운다(콘솔 알림문과 섞이면 파이프 파싱이 깨진다)
        cfg["alerts"]["channels"]["console"] = False
    now = pd.Timestamp(args.now) if args.now else None

    if args.archive:
        # 통합 아카이브(여러 로거) 점검 — 로거별로 나눠 규칙을 적용한다
        master = archive.load_master(args.archive, clean=False)
        frames = list(archive.iter_loggers(master)) if args.by_logger else [("(전체)", master)]
        alerts_all, health_all, contexts = [], [], []
        for logger_id, df in frames:
            if "logger" in df.columns:
                df = df.drop(columns=["logger"])
            interval = qc_rules.resolve_interval(cfg, df)
            grid_l, _ = io_logger.reindex_full_grid(df, interval_minutes=interval)
            a = qc_rules.run_all(grid_l, cfg, lookback_days=args.lookback, now=now)
            if not a.empty:
                a.insert(0, "logger", logger_id)
                a["key"] = logger_id + "|" + a["key"]      # 로거별로 쿨다운 분리
                alerts_all.append(a)
            h = qc_rules.health_score(grid_l, cfg, days=args.lookback)
            if not h.empty:
                h.insert(0, "로거", logger_id)
                health_all.append(h)
            contexts.append({
                "logger": logger_id, "n_rows": len(df),
                "start": str(df["timestamp"].min()), "end": str(df["timestamp"].max()),
                "n_missing_ts": int((grid_l["qc_status"] == "missing_timestamp_inserted").sum()),
                "interval_minutes": interval,
            })
        alerts = (pd.concat(alerts_all, ignore_index=True) if alerts_all
                  else qc_rules.run_all(pd.DataFrame(columns=["timestamp"]), cfg))
        health = pd.concat(health_all, ignore_index=True) if health_all else pd.DataFrame()
        context = {
            "start": min(c["start"] for c in contexts), "end": max(c["end"] for c in contexts),
            "n_rows": int(sum(c["n_rows"] for c in contexts)),
            "n_missing_ts": int(sum(c["n_missing_ts"] for c in contexts)),
            "n_files": len(contexts),
            "interval_minutes": contexts[0]["interval_minutes"] if contexts else 10,
        }
        variables = sorted(qc_rules.value_columns(master))
    else:
        paths = expand_paths(args.env or [])
        if not paths:
            print("환경 파일을 찾지 못했습니다. --env 또는 --archive 를 지정하세요.", file=sys.stderr)
            return 2
        grid, context, extra = load_standardized(paths, cfg, replicate=args.replicate)
        alerts = qc_rules.run_all(grid, cfg, lookback_days=args.lookback, now=now,
                                  map_report=extra.get("mapping"))
        health = qc_rules.health_score(grid, cfg, days=args.lookback)
        variables = sorted(qc_rules.value_columns(grid))

    result = alert_mod.dispatch(alerts, cfg, context=context, health=health, dry_run=args.dry_run)

    summary = {
        **qc_rules.summarize(alerts),
        "interval_minutes": context["interval_minutes"],
        "variables": variables,
        "sent": result["n_sent"],
        "report": result.get("report_path"),
        "period": f"{context['start']} ~ {context['end']}",
        "missing_timestamps": context["n_missing_ts"],
    }
    if args.archive and not alerts.empty and "logger" in alerts.columns:
        summary["by_logger"] = alerts.groupby("logger").size().to_dict()
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))

    if summary.get("CRITICAL", 0) > 0:
        return 2
    if summary.get("WARN", 0) > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
