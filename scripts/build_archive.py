#!/usr/bin/env python3
"""보유한 모든 환경데이터를 하나로 모으는 통합 아카이브 CLI.

폴더에 파일을 넣고 실행하면 형식·기록간격·변수명을 자동 인식해
**날짜순 정렬 + 변수 표준화 + 중복 정리**된 마스터 파일을 만든다.
새 파일을 받으면 다시 실행만 하면 기존 마스터에 이어붙는다.

사용 예
-------
# 처음 만들기 (하위 폴더까지 훑기)
python scripts/build_archive.py --env "data/**/*.xlsx" "data/**/*.csv" --out outputs/archive

# 새로 받은 파일만 넣고 업데이트 (기존 마스터에 이어붙임)
python scripts/build_archive.py --env "data/신규/*.xlsx" --out outputs/archive

# 처음부터 다시 만들기
python scripts/build_archive.py --env "data/*.xlsx" --out outputs/archive --rebuild

산출물
------
  env_master.csv        원자료 통합(정렬·중복정리·변수 표준화) ← 원본 보존
  env_master_clean.csv  범위 이탈값 결측 처리 + 격자 정합    ← 분석·모니터링용
  archive_summary.csv   로거별 현황
  archive_report.xlsx   위 전부 + 파일 목록 + 범위이탈 리포트
"""

from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import archive                              # noqa: E402
from src.config import load_config                   # noqa: E402

DEFAULT_PATTERNS = ["data/**/*.xlsx", "data/**/*.xls", "data/**/*.csv", "data/**/*.txt"]


def expand(patterns: list[str]) -> list[str]:
    """글롭 확장(`**` 재귀 지원). 중복 경로는 한 번만."""
    out: list[str] = []
    for p in patterns:
        hits = sorted(glob.glob(p, recursive=True))
        out.extend(hits if hits else [])
    seen, uniq = set(), []
    for p in out:
        rp = str(Path(p).resolve())
        if rp not in seen and Path(p).is_file():
            seen.add(rp)
            uniq.append(p)
    return uniq


def main() -> int:
    ap = argparse.ArgumentParser(description="환경데이터 통합 아카이브 생성/갱신")
    ap.add_argument("--env", nargs="+", default=DEFAULT_PATTERNS,
                    help="환경 파일 경로/글롭(** 재귀 가능). 기본: data/ 아래 전부")
    ap.add_argument("--out", default="outputs/archive", help="아카이브 디렉터리")
    ap.add_argument("--config", default=None)
    ap.add_argument("--replicate", choices=["first", "mean", "keep"], default="first")
    ap.add_argument("--rebuild", action="store_true",
                    help="기존 마스터를 무시하고 처음부터 다시 만든다")
    args = ap.parse_args()

    cfg = load_config(args.config)
    paths = expand(args.env)
    if not paths:
        print(f"파일을 찾지 못했습니다: {' '.join(args.env)}", file=sys.stderr)
        return 2

    print(f"[1/3] 파일 {len(paths)}개 읽는 중...")
    res = archive.build_archive(paths, cfg, args.out,
                                replicate=args.replicate, update=not args.rebuild)
    for line in res["log"]:
        print("   ", line)

    master, clean, summary = res["master"], res["clean"], res["summary"]
    print(f"\n[2/3] 통합 마스터 {len(master):,}행 · 로거 {master['logger'].nunique()}대 · "
          f"변수 {len([c for c in master.columns if c not in ('logger', 'timestamp')])}종")
    print(summary[["로거", "시작", "종료", "기간(일)", "관측행", "기록간격(분)",
                   "결측ts", "변수수"]].to_string(index=False))

    if not res["range_report"].empty:
        print("\n    범위 이탈값 결측 처리:")
        for _, r in res["range_report"].iterrows():
            print(f"      - [{r['logger']}] {r['변수']}: {r['결측처리건수']:,}건")

    print("\n[3/3] Excel 리포트 작성 중...")
    xlsx = Path(args.out) / "archive_report.xlsx"
    with pd.ExcelWriter(xlsx, engine="openpyxl") as w:
        summary.to_excel(w, sheet_name="logger_summary", index=False)
        res["files"].to_excel(w, sheet_name="files", index=False)
        (res["range_report"] if not res["range_report"].empty
         else pd.DataFrame({"note": ["범위 이탈값 없음"]})).to_excel(
            w, sheet_name="out_of_range", index=False)
        # 원자료 전체는 용량이 커질 수 있어 일자별 관측 수만 수록
        cnt = (master.assign(date=master["timestamp"].dt.date)
               .groupby(["logger", "date"]).size().rename("관측수").reset_index())
        cnt.to_excel(w, sheet_name="daily_record_count", index=False)

    out = Path(args.out)
    print(f"    → {out / archive.MASTER_NAME}  (원자료 통합)")
    print(f"    → {out / archive.CLEAN_NAME}  (QC 적용, 분석·모니터링용)")
    print(f"    → {xlsx}")
    print("\n다음 단계:")
    print(f"  모니터링   python scripts/run_monitor.py --archive {out} --by-logger")
    print(f"  시차 매칭  python scripts/run_preprocess.py --archive {out} "
          f"--survey-start 2026-04-01 --survey-interval 10 --survey-count 6 --out outputs/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
