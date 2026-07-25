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
from src import io_logger, qc_rules            # noqa: E402
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
    ap.add_argument("--env", nargs="+", required=True, help="환경 로거 파일 경로/글롭")
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
    paths = expand_paths(args.env)
    if not paths:
        print("환경 파일을 찾지 못했습니다.", file=sys.stderr)
        return 2

    grid, context, extra = load_standardized(paths, cfg, replicate=args.replicate)
    now = pd.Timestamp(args.now) if args.now else None

    alerts = qc_rules.run_all(grid, cfg, lookback_days=args.lookback, now=now,
                              map_report=extra.get("mapping"))
    health = qc_rules.health_score(grid, cfg, days=args.lookback)
    result = alert_mod.dispatch(alerts, cfg, context=context, health=health, dry_run=args.dry_run)

    summary = {
        **qc_rules.summarize(alerts),
        "interval_minutes": context["interval_minutes"],
        "variables": sorted(qc_rules.value_columns(grid)),
        "sent": result["n_sent"],
        "report": result.get("report_path"),
        "period": f"{context['start']} ~ {context['end']}",
        "missing_timestamps": context["n_missing_ts"],
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))

    if summary.get("CRITICAL", 0) > 0:
        return 2
    if summary.get("WARN", 0) > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
