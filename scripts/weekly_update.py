#!/usr/bin/env python3
"""매주 새 자료를 넣고 한 번에 끝내는 명령 — 쌓기 · 점검 · 정리.

새로 내려받은 파일을 폴더에 넣고 이 명령 하나만 실행하면 된다.

    python scripts/weekly_update.py --env "data/신규/*"

하는 일
-------
1. **쌓기**   올린 파일을 원본 그대로 보관하고(`outputs/archive/uploads/`)
              누적 마스터에 이어붙인다. 내용이 같은 파일은 다시 넣지 않고,
              같은 구역·같은 시각은 한 행으로 합쳐지므로 겹쳐 올려도 안전하다.
2. **점검**   누적된 자료 전체를 구역별로 QC 점검하고 알림·리포트를 남긴다.
3. **정리**   조사일 기준으로 시차 매칭해 구간별 환경 표를 만들고,
              결과를 **회차 폴더** `outputs/results/<날짜>/` 에 통째로 남긴다.
              지난주 결과는 그 폴더에 그대로 있으므로 언제든 다시 꺼내 쓴다.

조사일 기준은 한 번만 알려주면 `config/survey.yaml` 에 기억되어
다음 주부터는 `--env` 만 주면 된다.

    # 첫 주 (조사일 기준을 알려줌)
    python scripts/weekly_update.py --env "data/*.xlsx" \\
           --survey-start 2026-04-01 --survey-interval 10 --survey-count 12

    # 그 다음 주부터
    python scripts/weekly_update.py --env "data/신규/*"
"""

from __future__ import annotations

import argparse
import glob
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import archive, store                        # noqa: E402
from src.config import PROJECT_ROOT, load_config      # noqa: E402

SURVEY_FILE = PROJECT_ROOT / "config" / "survey.yaml"
SURVEY_KEYS = ["survey_dates", "survey_start", "survey_interval", "survey_count",
               "survey_end", "first_start", "lag_days", "by_treatment", "growth"]


def expand(patterns: list[str]) -> list[str]:
    out: list[str] = []
    for p in patterns:
        out.extend(sorted(glob.glob(p, recursive=True)))
    seen, uniq = set(), []
    for p in out:
        rp = str(Path(p).resolve())
        if rp not in seen and Path(p).is_file():
            seen.add(rp)
            uniq.append(p)
    return uniq


def load_survey() -> dict:
    if SURVEY_FILE.exists():
        return yaml.safe_load(SURVEY_FILE.read_text(encoding="utf-8")) or {}
    return {}


def save_survey(opts: dict) -> None:
    SURVEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    SURVEY_FILE.write_text(
        "# 조사일 기준 — weekly_update.py 가 기억해 두는 값(직접 고쳐도 됩니다)\n"
        + yaml.safe_dump({k: v for k, v in opts.items() if v not in (None, "", False)},
                         allow_unicode=True, sort_keys=False),
        encoding="utf-8")


def run(cmd: list[str]) -> int:
    print("   $", " ".join(str(c) for c in cmd))
    return subprocess.call([sys.executable] + [str(c) for c in cmd])


def main() -> int:
    ap = argparse.ArgumentParser(description="주간 업데이트: 쌓기 · 점검 · 정리")
    ap.add_argument("--env", nargs="+", default=["data/**/*"],
                    help="이번에 새로 받은 파일 경로/글롭(기본: data/ 아래 전부)")
    ap.add_argument("--store", default="outputs/archive", help="보관함 폴더")
    ap.add_argument("--results", default="outputs/results", help="회차별 결과 폴더")
    ap.add_argument("--config", default=None)
    ap.add_argument("--registry", default=None)
    ap.add_argument("--replicate", choices=["first", "mean", "keep"], default="first")
    ap.add_argument("--rebuild", action="store_true",
                    help="보관된 원본 전부를 처음부터 다시 통합(구역 이름을 새로 지정했을 때)")
    ap.add_argument("--label", default="", help="회차 폴더 이름에 덧붙일 말(예: 3주차)")
    ap.add_argument("--skip-monitor", action="store_true", help="점검 단계 건너뛰기")
    ap.add_argument("--skip-preprocess", action="store_true", help="정리 단계 건너뛰기")
    ap.add_argument("--lookback", type=int, default=7, help="점검할 최근 일수")
    # 조사일 기준 (한 번만 주면 기억된다)
    ap.add_argument("--survey-dates", default=None)
    ap.add_argument("--survey-start", default=None)
    ap.add_argument("--survey-interval", type=int, default=None)
    ap.add_argument("--survey-count", type=int, default=None)
    ap.add_argument("--survey-end", default=None)
    ap.add_argument("--first-start", default=None)
    ap.add_argument("--lag-days", type=int, default=None)
    ap.add_argument("--by-treatment", action="store_true")
    ap.add_argument("--growth", default=None, help="생육조사 파일(있으면 병합까지)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    store_dir = Path(args.store)
    scripts = Path(__file__).resolve().parent

    # ---------------------------------------------------------------
    print("[1/3] 쌓기 — 새 자료를 보관하고 누적 마스터에 이어붙입니다")
    paths = expand(args.env)
    if not paths and not args.rebuild:
        print(f"    파일을 찾지 못했습니다: {' '.join(args.env)}", file=sys.stderr)
        return 2
    res = store.add_files(paths, cfg, store_dir, replicate=args.replicate,
                          registry_path=args.registry, rebuild=args.rebuild)

    ups = res["uploads"]
    if not ups.empty:
        n_new = int((ups["상태"] == "새로 보관").sum())
        n_dup = len(ups) - n_new
        print(f"    올린 파일 {len(ups)}개 → 새로 보관 {n_new}개"
              + (f", 이미 있어 건너뜀 {n_dup}개" if n_dup else ""))
    for line in res.get("log", []):
        print("   ", line)
    print(f"    누적 마스터: {res['before']:,}행 → {res['after']:,}행 "
          f"(이번에 {res['added']:+,}행)")

    unnamed = [s for s, e in ((res.get("registry") or {}).get("loggers") or {}).items()
               if not str((e or {}).get("zone", "")).strip()]
    if unnamed:
        print(f"    ⚠ 구역 이름이 없는 로거 {len(unnamed)}대: {', '.join(unnamed)}")
        print(f'      지정: python scripts/build_archive.py --zone "{unnamed[0]}=1구역"')

    # ---------------------------------------------------------------
    if not args.skip_monitor:
        print("\n[2/3] 점검 — 누적 자료 전체를 구역별로 확인합니다")
        rc = run([scripts / "run_monitor.py", "--archive", store_dir,
                  "--by-logger", "--lookback", args.lookback])
        print(f"    점검 종료코드 {rc} (0=이상없음, 1=주의, 2=조치필요)")
    else:
        print("\n[2/3] 점검 — 건너뜀")

    # ---------------------------------------------------------------
    if args.skip_preprocess:
        print("\n[3/3] 정리 — 건너뜀")
        return 0

    print("\n[3/3] 정리 — 조사일 기준으로 시차 매칭하고 이번 회차로 남깁니다")
    opts = load_survey()
    given = {k: getattr(args, k) for k in SURVEY_KEYS
             if getattr(args, k, None) not in (None, "", False)}
    opts.update(given)
    if not any(opts.get(k) for k in ("survey_dates", "survey_start", "growth")):
        print("    조사일 기준이 없습니다. 한 번만 알려주면 다음부터 기억합니다:")
        print("      --survey-start 2026-04-01 --survey-interval 10 --survey-count 12")
        print("      (또는 --survey-dates \"2026-04-01,2026-04-11,...\" / --growth 생육파일)")
        return 1
    save_survey(opts)
    print(f"    조사일 기준: " + ", ".join(f"{k}={v}" for k, v in opts.items()))

    stamp = datetime.now()
    base = f"{stamp:%Y-%m-%d}" + (f"_{args.label}" if args.label else "")
    folder = Path(args.results) / base
    n = 2
    while folder.exists():
        folder = Path(args.results) / f"{base}({n})"
        n += 1
    folder.mkdir(parents=True)

    cmd = [scripts / "run_all_loggers.py", "--archive", store_dir, "--out", folder]
    flag = {"survey_dates": "--survey-dates", "survey_start": "--survey-start",
            "survey_interval": "--survey-interval", "survey_count": "--survey-count",
            "survey_end": "--survey-end", "first_start": "--first-start",
            "lag_days": "--lag-days", "growth": "--growth"}
    for k, f in flag.items():
        if opts.get(k) not in (None, "", False):
            cmd += [f, opts[k]]
    if opts.get("by_treatment"):
        cmd += ["--by-treatment"]
    rc = run(cmd)
    if rc != 0:
        print("    ⚠ 정리 단계가 실패했습니다. 위 메시지를 확인하세요.", file=sys.stderr)
        return rc

    iv_path = folder / "all_loggers_interval.csv"
    n_iv = len(pd.read_csv(iv_path)) if iv_path.exists() else 0
    store.register_result(folder, args.results, memo=args.label or "주간 업데이트",
                          n_intervals=n_iv, when=stamp)

    print(f"\n완료 — 이번 회차 결과: {folder}")
    for p in sorted(folder.iterdir()):
        print(f"    - {p.name}")
    idx = store.list_results(args.results)
    if len(idx) > 1:
        print(f"\n지난 회차 {len(idx) - 1}개도 그대로 있습니다:")
        for _, r in idx.iloc[1:6].iterrows():
            print(f"    - {r['저장시각']}  {r['폴더']}  (구간 {r['구간수']}개)")
    print(f"\n누적 자료 전체: {store_dir / archive.CLEAN_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
