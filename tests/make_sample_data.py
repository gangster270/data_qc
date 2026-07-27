#!/usr/bin/env python3
"""테스트·데모용 합성 데이터 생성기.

ZL6 export 형태(3행 헤더, 단위 접두 변수명)의 10분 환경자료와,
10일 간격 생육조사 자료를 만든다. 결측·고착·범위이탈을 의도적으로 주입해
모니터링 규칙이 실제로 잡아내는지 확인할 수 있다.

    python tests/make_sample_data.py --out tests/sample
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def build_env(start="2026-04-01", days=60, seed=42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ts = pd.date_range(start, periods=days * 144, freq="10min")
    hour = ts.hour + ts.minute / 60
    doy = ts.dayofyear

    # 일주기 + 계절 추세 + 잡음
    temp = 22 + 7 * np.sin((hour - 9) / 24 * 2 * np.pi) + 0.03 * (doy - doy[0]) + rng.normal(0, 0.6, len(ts))
    rh = np.clip(75 - 1.6 * (temp - 22) + rng.normal(0, 4, len(ts)), 20, 100)
    soil_temp = 21 + 3 * np.sin((hour - 12) / 24 * 2 * np.pi) + rng.normal(0, 0.3, len(ts))
    vwc = np.clip(0.34 + 0.03 * np.sin(doy / 3) + rng.normal(0, 0.004, len(ts)), 0.1, 0.6)
    ec = np.clip(1.8 + rng.normal(0, 0.08, len(ts)), 0, 6)

    # 광: 일출~일몰 sin 곡선 × 일별 구름 계수
    daylight = np.clip(np.sin((hour - 6) / 13 * np.pi), 0, None)
    cloud = np.repeat(rng.uniform(0.35, 1.0, days), 144)
    ppfd = daylight * 1250 * cloud * (1 + rng.normal(0, 0.05, len(ts)))
    ppfd = np.clip(ppfd, 0, None)
    solar = ppfd / 2.057 * rng.uniform(0.97, 1.03, len(ts))     # 동일 로거 PYR

    df = pd.DataFrame({
        "Timestamp": ts,
        " °C Air Temperature": temp.round(2),
        "% Relative Humidity": rh.round(1),
        " °C Soil Temperature": soil_temp.round(2),
        "% Water Content": vwc.round(4),
        " mS/cm Saturation Extract EC": ec.round(3),
        " µmol·m⁻²·s⁻¹ PPFD": ppfd.round(1),
        " W/m² Solar Radiation": solar.round(1),
        " °C Logger Temperature": (temp + 1.5).round(2),        # 제외되어야 하는 로거 내부채널
        "% Battery Percent": 95,
    })

    # --- 이상 주입 -------------------------------------------------------
    # (1) 30~31일차 8시간 기록 누락(로거 다운)
    drop = (df["Timestamp"] >= "2026-04-30 02:00") & (df["Timestamp"] < "2026-04-30 10:00")
    df = df[~drop].reset_index(drop=True)

    # (2) 40일차 배지온도 고착(동일값 20시간)
    m = (df["Timestamp"] >= "2026-05-10 00:00") & (df["Timestamp"] < "2026-05-10 20:00")
    df.loc[m, " °C Soil Temperature"] = 19.44

    # (3) 45일차 습도 센서 결측(하루 전체)
    m = (df["Timestamp"] >= "2026-05-15") & (df["Timestamp"] < "2026-05-16")
    df.loc[m, "% Relative Humidity"] = np.nan

    # (4) 50일차 온도 범위 이탈(배선 접촉 불량)
    m = (df["Timestamp"] >= "2026-05-20 12:00") & (df["Timestamp"] < "2026-05-20 15:00")
    df.loc[m, " °C Air Temperature"] = -99.9

    # (5) 55일차 PPFD 주간 암흑(센서 탈락)
    m = (df["Timestamp"] >= "2026-05-25") & (df["Timestamp"] < "2026-05-26")
    df.loc[m, " µmol·m⁻²·s⁻¹ PPFD"] = 0.0

    # (6) 오류 토큰 섞기(엑셀 계산 오류) — 숫자열에 문자열을 넣으므로 object 로 변환
    idx = df.index[(df["Timestamp"] >= "2026-05-05 10:00") & (df["Timestamp"] < "2026-05-05 11:00")]
    # (엑셀은 '#VALUE!' 를 오류셀로 저장해 되읽을 때 사라지므로 'ERROR' 토큰을 사용)
    df[" mS/cm Saturation Extract EC"] = df[" mS/cm Saturation Extract EC"].astype(object)
    df.loc[idx, " mS/cm Saturation Extract EC"] = "ERROR"

    return df


def write_zl6_style(df: pd.DataFrame, path: Path) -> None:
    """ZL6 export 처럼 상단에 2줄의 메타 행을 붙여 저장한다(헤더는 3행)."""
    header_rows = [
        ["z6-99999"] + [f"Port {i}" for i in range(1, len(df.columns))],
        ["Records: %d" % len(df)] + ["ATMOS 14", "ATMOS 14", "TEROS 12", "TEROS 12",
                                     "TEROS 12", "SQ-521", "PYR", "Logger", "Battery"],
        list(df.columns),
    ]
    body = df.astype(object).values.tolist()
    out = pd.DataFrame(header_rows + body)
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_excel(path, index=False, header=False, sheet_name="Processed Data Config 1")


def build_growth(start="2026-04-10", n=6, cadence=10, seed=7) -> pd.DataFrame:
    """10일 간격 생육조사(처리구 2 × 반복 3)."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range(start, periods=n, freq=f"{cadence}D")
    rows = []
    for i, d in enumerate(dates, start=1):
        for trt in ("NI", "NI+SL"):
            boost = 1.0 if trt == "NI" else 1.15
            for rep in (1, 2, 3):
                rows.append({
                    "date": d.date(),
                    "trt": trt,
                    "rep": rep,
                    "plant_height": round(8 + 3.2 * i * boost + rng.normal(0, 0.8), 1),
                    "leaf_number": int(4 + 1.8 * i * boost + rng.normal(0, 0.7)),
                    "fresh_wt": round(5 + 4.6 * i * boost + rng.normal(0, 1.2), 2),
                    "dry_wt": round((5 + 4.6 * i * boost) * 0.11 + rng.normal(0, 0.15), 3),
                })
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="tests/sample")
    args = ap.parse_args()
    out = Path(args.out)

    env = build_env()
    write_zl6_style(env, out / "z6-99999_260601-0900.xlsx")
    growth = build_growth()
    growth.to_csv(out / "growth.csv", index=False, encoding="utf-8-sig")

    print(f"환경자료: {out / 'z6-99999_260601-0900.xlsx'} ({len(env):,}행)")
    print(f"생육자료: {out / 'growth.csv'} ({len(growth)}행, 조사 {growth['date'].nunique()}회)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
