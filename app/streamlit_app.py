#!/usr/bin/env python3
"""환경데이터 QC · 전처리 · 센서검증 통합 대시보드 (Streamlit).

실행:
    streamlit run app/streamlit_app.py

탭 구성
  1. 모니터링   : 결측·센서오류 자동 점검 결과, 상태카드, 결측 히트맵, 시계열
  2. 전처리     : 10분 → 일별 → 생육구간(7·10일) 시차 매칭 + 결과 다운로드
  3. 센서 검증  : 정기검증 기한 현황, 센서 상호비교, 검증 로그 기록
  4. 설정       : 임계값 확인 및 임시 조정, 알림 테스트 발송
"""

from __future__ import annotations

import io
import sys
from datetime import datetime, date
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import alerts as alert_mod          # noqa: E402
from src import archive, io_logger, preprocess, qc_rules, registry, sensor_check, sensor_map   # noqa: E402
from src.config import PROJECT_ROOT, load_config, resolve_path  # noqa: E402

st.set_page_config(page_title="환경데이터 QC 대시보드", page_icon="🌱", layout="wide")

LEVEL_COLOR = {"CRITICAL": "#d64545", "WARN": "#e08b3a", "INFO": "#3a7ad6"}
STATUS_COLOR = {"정상": "#2e8b57", "주의": "#e08b3a", "위험": "#d64545"}


# =====================================================================
# 데이터 로딩
# =====================================================================
def _process(raw, log, cfg, replicate, overrides):
    """읽은 원자료를 표준화 + 격자 정합까지 처리(로거 기종·간격 무관)."""
    ts_df, ts_report = io_logger.prepare_timestamp(raw)
    interval = qc_rules.resolve_interval(cfg, ts_df)      # 설정이 auto 면 자료에서 추정
    std, map_report = io_logger.standardize(ts_df, replicate=replicate, overrides=overrides)
    grid, gap_report = io_logger.reindex_full_grid(std, interval_minutes=interval)
    ts_report["interval_minutes"] = interval
    return grid, map_report, gap_report, ts_report, log


@st.cache_data(show_spinner=False)
def load_from_bytes(files: list[tuple[str, bytes]], _cfg: dict, replicate: str,
                    overrides_key: tuple = ()):
    """업로드된 파일들을 읽어 처리(캐시). 어떤 형식이든 자동 인식한다."""
    sources = [(io.BytesIO(b), name) for name, b in files]
    raw, log = io_logger.load_env_files(sources)
    return _process(raw, log, _cfg, replicate, dict(overrides_key))


@st.cache_data(show_spinner=False)
def load_from_paths(paths: tuple[str, ...], _cfg: dict, replicate: str,
                    overrides_key: tuple = ()):
    raw, log = io_logger.load_env_files(list(paths))
    return _process(raw, log, _cfg, replicate, dict(overrides_key))


@st.cache_data(show_spinner=False)
def load_archive_master(arc_dir: str):
    """통합 아카이브 마스터를 읽는다(캐시)."""
    return archive.load_master(arc_dir, clean=False)


def df_download(df: pd.DataFrame, label: str, filename: str, key: str | None = None):
    st.download_button(label, df.to_csv(index=False).encode("utf-8-sig"),
                       file_name=filename, mime="text/csv", key=key)


def excel_bytes(sheets: dict[str, pd.DataFrame]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        for name, df in sheets.items():
            if df is None:
                continue
            df.to_excel(w, sheet_name=name[:31], index=False)
    return buf.getvalue()


# =====================================================================
# 사이드바 — 데이터 입력
# =====================================================================
cfg = load_config()
st.sidebar.title("🌱 환경데이터 QC")
st.sidebar.caption(f"현장: {cfg['site'].get('name', '-')} · "
                   f"기록간격 {cfg['site'].get('interval_minutes', 'auto')}")

st.sidebar.markdown("**① 파일 넣기 → ② 탭에서 확인**")
source_mode = st.sidebar.radio("데이터 입력", ["파일 업로드", "서버 경로", "통합 아카이브"],
                              horizontal=True,
                              help="통합 아카이브 = build_archive.py 로 만든 전체 환경데이터 마스터")
replicate = st.sidebar.selectbox(
    "중복 센서 처리", ["first", "mean", "keep"], index=0,
    help="같은 변수 센서가 여러 개일 때: first=첫 센서, mean=평균(같은 위치 반복일 때만), keep=모두 보존")

overrides = st.session_state.get("col_overrides", {})
overrides_key = tuple(sorted(overrides.items()))
grid = map_report = gap_report = ts_report = None
load_log: list[str] = []

if source_mode == "파일 업로드":
    ups = st.sidebar.file_uploader(
        "환경 로거 파일 (.xlsx/.xls/.csv/.txt, 여러 개 가능)",
        type=["xlsx", "xls", "csv", "txt", "tsv"], accept_multiple_files=True,
        help="ZL6 뿐 아니라 어떤 로거 파일이든 시간 열만 있으면 자동 인식합니다.")
    if ups:
        payload = [(u.name, u.getvalue()) for u in ups]
        st.session_state["loaded_names"] = [u.name for u in ups]
        with st.spinner("파일 처리 중..."):
            grid, map_report, gap_report, ts_report, load_log = load_from_bytes(
                payload, cfg, replicate, overrides_key)
elif source_mode == "통합 아카이브":
    arc_dir = st.sidebar.text_input("아카이브 경로", value="outputs/archive",
                                    help="build_archive.py 의 --out 디렉터리")
    arc_path = PROJECT_ROOT / arc_dir if not Path(arc_dir).is_absolute() else Path(arc_dir)
    if (arc_path / archive.MASTER_NAME).exists():
        master_all = load_archive_master(str(arc_path))
        loggers_in = sorted(master_all["logger"].unique()) if "logger" in master_all else ["(전체)"]
        pick_logger = st.sidebar.selectbox("로거", loggers_in)
        sub = master_all[master_all["logger"] == pick_logger] if "logger" in master_all else master_all
        sub = sub.drop(columns=[c for c in ("logger",) if c in sub.columns]).dropna(axis=1, how="all")
        st.session_state["loaded_names"] = [f"{pick_logger}.xlsx"]
        interval_a = qc_rules.resolve_interval(cfg, sub)
        grid, gap_report = io_logger.reindex_full_grid(sub.reset_index(drop=True),
                                                       interval_minutes=interval_a)
        map_report = pd.DataFrame()
        ts_report = {"timestamp_column": "timestamp(아카이브)", "duplicate_rows": 0,
                     "start": sub["timestamp"].min(), "end": sub["timestamp"].max(),
                     "n_rows": len(sub), "interval_minutes": interval_a}
        load_log = [f"아카이브 {arc_dir} → 로거 {pick_logger} ({len(sub):,}행)",
                    f"전체 아카이브: {len(master_all):,}행 / 로거 {master_all['logger'].nunique()}대"]
    else:
        st.sidebar.error(f"{arc_dir}/{archive.MASTER_NAME} 이 없습니다. "
                         f"먼저 build_archive.py 를 실행하세요.")
else:
    pattern = st.sidebar.text_input("파일 경로/글롭", value="data/*.xlsx")
    if st.sidebar.button("불러오기", type="primary"):
        import glob
        paths = tuple(sorted(glob.glob(str(PROJECT_ROOT / pattern) if not Path(pattern).is_absolute() else pattern))
                      or sorted(glob.glob(pattern)))
        if not paths:
            st.sidebar.error("파일을 찾지 못했습니다.")
        else:
            st.session_state["paths"] = paths
    if st.session_state.get("paths"):
        st.session_state["loaded_names"] = [Path(p).name for p in st.session_state["paths"]]
        with st.spinner("파일 처리 중..."):
            grid, map_report, gap_report, ts_report, load_log = load_from_paths(
                st.session_state["paths"], cfg, replicate, overrides_key)

if grid is not None:
    st.sidebar.success(f"{len(grid):,}행 · {ts_report['start']:%Y-%m-%d} ~ {ts_report['end']:%Y-%m-%d}")

    # --- 자동 인식 결과: 무엇을 어떻게 읽었는지 항상 보이게 --------------
    with st.sidebar.expander("🔍 자동 인식 결과 / 수정", expanded=False):
        st.write(f"- **시간 열**: `{ts_report['timestamp_column']}`")
        st.write(f"- **기록 간격**: {ts_report.get('interval_minutes', 10):g}분 (자동 추정)")
        st.write(f"- **중복 timestamp**: {ts_report['duplicate_rows']:,}건 제거")
        detected = [c for c in qc_rules.value_columns(grid)]
        st.write(f"- **인식 변수({len(detected)})**: " +
                 ", ".join(f"`{c}`" for c in detected))
        st.caption("표준 변수로 인식되지 않은 열도 이름 그대로 보존되어 집계·감시됩니다.")

        if map_report is not None and not map_report.empty:
            st.markdown("**열 매핑을 고치려면** (자동 인식이 틀렸을 때만)")
            options = ["(자동)"] + io_logger.STANDARD_ORDER + ["제외"]
            src_cols = map_report["원본열"].astype(str).tolist()
            pick_col = st.selectbox("원본 열", src_cols, key="ov_col")
            pick_var = st.selectbox("이 열의 의미", options, key="ov_var")
            c1, c2 = st.columns(2)
            if c1.button("적용", key="ov_apply"):
                ov = dict(st.session_state.get("col_overrides", {}))
                if pick_var == "(자동)":
                    ov.pop(pick_col, None)
                else:
                    ov[pick_col] = pick_var
                st.session_state["col_overrides"] = ov
                st.rerun()
            if c2.button("초기화", key="ov_reset"):
                st.session_state["col_overrides"] = {}
                st.rerun()
            if overrides:
                st.info("수동 지정: " + ", ".join(f"{k}→{v}" for k, v in overrides.items()))

    with st.sidebar.expander("로드 로그"):
        for line in load_log:
            st.write("- " + line)
else:
    st.sidebar.info("파일을 넣으면 아래 4개 탭이 채워집니다.")

tab_mon, tab_pre, tab_ver, tab_cfg = st.tabs(
    ["📊 모니터링", "🔁 전처리(생육 매칭)", "🔬 센서 정기검증", "⚙️ 설정"])


# =====================================================================
# 1. 모니터링 탭
# =====================================================================
with tab_mon:
    st.header("결측 · 센서오류 자동 모니터링")
    with st.expander("❓ 이 화면 보는 법", expanded=False):
        st.markdown("""
1. **상단 숫자 5개** — 알림 건수(전체/CRITICAL/WARN), 결측 timestamp, 관측 일수.
   빨간 CRITICAL 이 0 이면 오늘은 조치할 게 없습니다.
2. **센서 상태** — 변수별 수신율·범위이탈·최근값. '정상/주의/위험'으로 표시됩니다.
3. **알림 목록** — 무엇이·언제·왜 이상한지. CSV 로 내려받아 보고서에 붙일 수 있습니다.
4. **일자별 결측률** — 날짜×변수 히트맵. 진한 칸이 그날 그 센서가 비어 있던 구간입니다.
5. **시계열 확인** — 의심 변수를 골라 실제 곡선으로 확인합니다.
6. **알림 발송** — Slack·메일로 보내거나(설정 탭에서 채널 켜기), 리포트만 미리봅니다.
""")
    if grid is None:
        st.info("왼쪽 사이드바에서 환경 로거 파일을 불러오세요. (ZL6·국산로거·자체 CSV 모두 가능)")
    else:
        c1, c2, c3 = st.columns([1, 1, 2])
        lookback = c1.number_input("점검 기간(일)", 1, 365, 7)
        min_level = c2.selectbox("표시 등급", ["INFO", "WARN", "CRITICAL"], index=0)
        now_ref = pd.Timestamp(grid["timestamp"].max())
        c3.caption(f"기준시각: 최신 관측 {now_ref:%Y-%m-%d %H:%M} (통신두절 판정은 현재시각 기준)")

        alerts = qc_rules.run_all(grid, cfg, lookback_days=int(lookback), map_report=map_report)
        health = qc_rules.health_score(grid, cfg, days=int(lookback))
        summary = qc_rules.summarize(alerts)

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("전체 알림", summary["total"])
        m2.metric("🔴 CRITICAL", summary["CRITICAL"])
        m3.metric("🟠 WARN", summary["WARN"])
        n_missing_ts = int((grid["qc_status"] == "missing_timestamp_inserted").sum())
        m4.metric("결측 timestamp", f"{n_missing_ts:,}",
                  delta=f"{n_missing_ts / max(len(grid), 1):.1%}", delta_color="inverse")
        m5.metric("관측 일수", f"{grid['timestamp'].dt.date.nunique()}일")

        st.subheader("센서 상태")
        if not health.empty:
            show = health[["변수", "상태", "수신율", "결측률", "범위이탈", "최근값", "평균", "최소", "최대", "마지막관측"]]
            st.dataframe(
                show.style.map(lambda v: f"color:{STATUS_COLOR.get(v, '')};font-weight:600"
                               if v in STATUS_COLOR else "", subset=["상태"])
                    .format({"수신율": "{:.1%}", "결측률": "{:.1%}", "최근값": "{:.2f}",
                             "평균": "{:.2f}", "최소": "{:.2f}", "최대": "{:.2f}"}),
                use_container_width=True, hide_index=True)

        st.subheader("알림 목록")
        order = {"INFO": 0, "WARN": 1, "CRITICAL": 2}
        shown = alerts[alerts["level"].map(order) >= order[min_level]] if not alerts.empty else alerts
        if shown.empty:
            st.success("표시 등급 이상 알림 없음 — 점검 항목 정상.")
        else:
            view = shown[["level", "rule", "label", "message", "start", "end"]].rename(
                columns={"level": "등급", "rule": "규칙", "label": "변수", "message": "내용",
                         "start": "시작", "end": "종료"})
            st.dataframe(
                view.style.map(lambda v: f"color:{LEVEL_COLOR.get(v, '')};font-weight:600"
                               if v in LEVEL_COLOR else "", subset=["등급"]),
                use_container_width=True, hide_index=True, height=320)
            df_download(shown, "알림 CSV 다운로드", f"alerts_{datetime.now():%Y%m%d}.csv", "dl_alerts")

        # --- 결측 히트맵 (날짜 × 변수) -----------------------------------
        st.subheader("일자별 결측률")
        interval = ts_report.get("interval_minutes", 10)
        expected = max(int(round(24 * 60 / interval)), 1)
        tmp = grid.copy()
        tmp["date"] = tmp["timestamp"].dt.date
        varcols = [c for c in qc_rules.STD_VARS if c in tmp.columns and tmp[c].notna().any()]
        if varcols:
            miss = (1 - tmp.groupby("date")[varcols].count() / expected).round(3).reset_index()
            miss["date"] = pd.to_datetime(miss["date"]).dt.strftime("%m-%d")   # 축 라벨용 문자열
            long = miss.melt(id_vars="date", var_name="변수", value_name="결측률")
            long["변수"] = long["변수"].map(io_logger.LABELS).fillna(long["변수"])
            import altair as alt
            chart = (alt.Chart(long).mark_rect().encode(
                x=alt.X("date:O", title="일자", axis=alt.Axis(labelAngle=-60, labelOverlap=True)),
                y=alt.Y("변수:N", title=None),
                color=alt.Color("결측률:Q", scale=alt.Scale(scheme="orangered", domain=[0, 1]),
                                legend=alt.Legend(format=".0%")),
                tooltip=["date:O", "변수:N", alt.Tooltip("결측률:Q", format=".1%")])
                .properties(height=28 * len(varcols) + 40))
            st.altair_chart(chart, use_container_width=True)

        # --- 시계열 확인 ---------------------------------------------------
        st.subheader("시계열 확인")
        sel = st.multiselect("변수 선택", varcols,
                             default=varcols[:2] if len(varcols) >= 2 else varcols,
                             format_func=lambda v: io_logger.LABELS.get(v, v))
        if sel:
            days = st.slider("표시 기간(일)", 1, 60, min(14, max(1, grid["timestamp"].dt.date.nunique())))
            sub = grid[grid["timestamp"] > grid["timestamp"].max() - pd.Timedelta(days=days)]
            st.line_chart(sub.set_index("timestamp")[sel], height=280)

        # --- 알림 발송 -----------------------------------------------------
        st.subheader("알림 발송")
        ch = cfg["alerts"].get("channels", {})
        st.caption(f"활성 채널: " + ", ".join(k for k, v in ch.items() if v) +
                   f" · 최소등급 {cfg['alerts'].get('min_level')} · 쿨다운 {cfg['alerts'].get('cooldown_hours')}시간")
        cc1, cc2 = st.columns(2)
        if cc1.button("지금 발송(쿨다운 적용)"):
            ctx = {"start": str(ts_report["start"]), "end": str(ts_report["end"]),
                   "n_rows": int(ts_report["n_rows"]), "n_missing_ts": n_missing_ts}
            res = alert_mod.dispatch(alerts, cfg, context=ctx, health=health)
            st.success(f"발송 {res['n_sent']}건 / 전체 {res['n_total']}건 · 리포트: {res['report_path']}")
            st.code(res["text"], language=None)
        if cc2.button("리포트 미리보기(발송 없음)"):
            ctx = {"start": str(ts_report["start"]), "end": str(ts_report["end"]),
                   "n_rows": int(ts_report["n_rows"]), "n_missing_ts": n_missing_ts}
            st.markdown(alert_mod.format_markdown(alerts, cfg, ctx, health))

        with st.expander("열 매핑 · 결측 timestamp 상세"):
            st.write("**원본 열 → 표준 변수 매핑**")
            st.dataframe(map_report, use_container_width=True, hide_index=True)
            st.write("**연속 결측 구간**")
            st.dataframe(gap_report if not gap_report.empty else pd.DataFrame({"note": ["결측 없음"]}),
                         use_container_width=True, hide_index=True)


# =====================================================================
# 2. 전처리 탭
# =====================================================================
with tab_pre:
    st.header("환경 → 생육조사 구간 시차 매칭")
    st.caption("생육은 구간 누적 반응이므로, 조사일 하루가 아니라 **직전 조사일 다음날~당일** 구간을 집계해 매칭합니다.")
    with st.expander("❓ 이 화면 보는 법 (수작업 대체 순서)", expanded=False):
        st.markdown("""
1. **위쪽 설정** — GDD 기준온도, 시차(일), 고정창 사용 여부, 범위 이탈값 처리.
   기본값 그대로 두어도 됩니다. 시차는 '환경이 며칠 뒤 생육에 반영되는가'를 볼 때만 씁니다.
2. **① 일별 요약** — 10분(또는 1·15·60분) 자료가 하루 단위로 접힌 표. DLI·평균기온·완전성 포함.
3. **② 생육조사 자료 업로드** — `date`(조사일) 열이 있는 csv/xlsx. 조사간격(7·10일)은 자동 인식.
4. **③ 구간 정의** — 각 조사일에 어떤 환경 기간이 붙었는지 확인합니다.
5. **④ 구간별 환경 요약 / ⑤ 생육+환경 병합** — 마지막 표가 분석에 바로 쓰는 파일입니다.
6. **다운로드** — `merged_env_growth.csv` 또는 Excel 일괄. R 통계·그래프로 그대로 넘기면 됩니다.
""")
    if grid is None:
        st.info("왼쪽 사이드바에서 환경 로거 파일을 먼저 불러오세요.")
    else:
        pcfg = cfg["preprocess"]
        c1, c2, c3, c4 = st.columns(4)
        gdd_base = c1.number_input("GDD 기준온도(℃)", 0.0, 20.0, float(pcfg.get("gdd_base", 10.0)), 0.5)
        lag_days = c2.number_input("시차(일)", 0, 30, int(pcfg.get("lag_days", 0)),
                                   help="구간 전체를 N일 앞당겨 매칭(환경 효과의 지연 반응 검토)")
        use_window = c3.checkbox("고정 창 사용", value=bool(pcfg.get("window_days")))
        window_days = c3.number_input("창 길이(일)", 1, 60, int(pcfg.get("window_days") or 10)) if use_window else None
        mask_range = c4.checkbox("범위 이탈값 결측 처리", value=True,
                                 help="-99.9 같은 오류값이 일평균·일최저를 오염시키지 않도록 제거")
        drop_incomplete = c4.checkbox("불완전일 제외",
                                      value=bool(pcfg.get("drop_incomplete_days", False)),
                                      help=f"레코드 완전성 {pcfg.get('daily_min_completeness', 0.9):.0%} 미만인 날 제외")

        clean = grid.drop(columns=["qc_status"])
        range_report = pd.DataFrame()
        if mask_range:
            clean, range_report = preprocess.mask_out_of_range(clean, cfg["sensors"])
            if not range_report.empty:
                st.warning("범위 이탈값 결측 처리: " +
                           ", ".join(f"{r['변수']} {r['결측처리건수']:,}건" for _, r in range_report.iterrows()))

        daily_kwargs = dict(
            interval_minutes=ts_report.get("interval_minutes", None),
            gdd_base=float(gdd_base),
            photoperiod_ppfd_threshold=float(pcfg.get("photoperiod_ppfd_threshold", 10)),
            daytime_hours=tuple(pcfg.get("daytime_hours", [9, 15])),
            min_completeness=float(pcfg.get("daily_min_completeness", 0.9)),
        )

        # --- 처리구별 집계(센서↔처리구 매핑) --------------------------------
        smap = sensor_map.load_sensor_map()
        logger_names = list((smap.get("loggers") or {}).keys())
        by_trt, map_coverage, frames = False, pd.DataFrame(), {}
        if logger_names:
            loaded = (st.session_state.get("loaded_names") or [""])[0]
            guess = sensor_map.logger_id_from_filename(loaded)
            default_idx = next((i for i, n in enumerate(logger_names) if n in guess or guess in n), 0)
            t1, t2 = st.columns([1, 2])
            use_map = t1.checkbox("처리구별 집계", value=False,
                                  help="한 로거의 센서들이 서로 다른 처리구를 잴 때 사용 "
                                       "(config/sensor_map.yaml)")
            logger_key = t2.selectbox("로거", logger_names, index=default_idx, disabled=not use_map)
            if use_map:
                entry = (smap.get("loggers") or {}).get(logger_key) or {}
                frames = sensor_map.split_by_treatment(clean, entry)
                if not frames:
                    st.error(f"'{logger_key}' 에 처리구 매핑이 없습니다. config/sensor_map.yaml 의 "
                             f"treatments 를 채우세요.")
                else:
                    map_coverage = sensor_map.coverage_report(clean, entry)
                    daily = preprocess.to_daily_by_treatment(frames, **daily_kwargs)
                    by_trt = True
                    st.success(f"처리구 {len(frames)}개로 분리: {', '.join(frames)} · "
                               f"공통변수 {', '.join(entry.get('shared', [])) or '없음'}")
                    unmapped = map_coverage[map_coverage["매핑"] == "미매핑"]["열"].tolist()
                    if unmapped:
                        st.warning("매핑되지 않은 열: " + ", ".join(unmapped))

        if not by_trt:
            daily = preprocess.to_daily(clean, **daily_kwargs)

        st.subheader("① 일별 요약")
        n_bad_day = int((~daily["is_complete"]).sum()) if "is_complete" in daily else 0
        n_days = daily["date"].nunique() if "date" in daily else 0
        st.caption(f"{n_days}일{f' × 처리구 {daily.trt.nunique()}개' if by_trt else ''} · "
                   f"불완전 {n_bad_day}건 "
                   f"(레코드 완전성 {pcfg.get('daily_min_completeness', 0.9):.0%} 미만)")
        st.dataframe(daily, use_container_width=True, hide_index=True, height=240)
        df_download(daily, "일별 요약 CSV", "daily_env_summary.csv", "dl_daily")

        st.subheader("② 조사일 기준 정하기")
        mode = st.radio(
            "조사일을 어떻게 정할까요?",
            ["시작일 + 간격", "조사일 직접 입력", "생육 파일 업로드"],
            horizontal=True,
            help="생육 자료가 아직 없어도 조사일만 정하면 구간 환경을 뽑을 수 있습니다.")

        survey_dates, growth, gfile = None, None, None
        if mode == "시작일 + 간격":
            s1, s2, s3, s4 = st.columns(4)
            default_start = pd.Timestamp(daily["date"].min()).date() if not daily.empty else date.today()
            sv_start = s1.date_input("조사 시작일", value=default_start, key="sv_start")
            sv_interval = s2.number_input("조사 간격(일)", 1, 60, 10, key="sv_int")
            end_mode = s3.selectbox("끝 지정", ["횟수", "종료일"], key="sv_endmode")
            if end_mode == "횟수":
                sv_count = s4.number_input("조사 횟수", 2, 60, 6, key="sv_cnt")
                survey_dates = preprocess.parse_survey_dates(
                    start=sv_start, interval=int(sv_interval), count=int(sv_count))
            else:
                default_end = pd.Timestamp(daily["date"].max()).date() if not daily.empty else date.today()
                sv_end = s4.date_input("조사 종료일", value=default_end, key="sv_end")
                survey_dates = preprocess.parse_survey_dates(
                    start=sv_start, interval=int(sv_interval), end=sv_end)
            st.caption("조사일: " + ", ".join(d.strftime("%Y-%m-%d") for d in survey_dates))

        elif mode == "조사일 직접 입력":
            txt = st.text_area(
                "조사일 목록 (쉼표 또는 줄바꿈으로 구분)",
                value="", height=90, key="sv_text",
                placeholder="2026-04-01, 2026-04-11, 2026-04-21")
            if txt.strip():
                try:
                    survey_dates = preprocess.parse_survey_dates(dates=txt)
                    st.caption(f"조사일 {len(survey_dates)}회 · 간격 "
                               f"{preprocess.detect_cadence(pd.Series(survey_dates))}일(최빈)")
                except ValueError as e:
                    st.error(str(e))
            else:
                st.info("조사일을 입력하면 그 날짜 기준으로 구간이 만들어집니다. "
                        "현장 사정으로 날짜가 밀렸어도 실제 조사일을 그대로 넣으면 됩니다.")

        else:
            gfile = st.file_uploader("생육 자료(csv/xlsx) — 조사일 열 필요",
                                     type=["csv", "xlsx", "xls"], key="growth_up")
            if gfile is None:
                st.info("생육 자료를 올리면 조사간격을 자동 추정해 구간 매칭·병합까지 수행합니다. "
                        "(templates/growth_template.csv 참조)")

        # --- 조사일만 정해진 경우: 구간 환경 요약까지 ------------------------
        if survey_dates is not None and len(survey_dates) >= 1:
            first_start_only = st.date_input(
                "첫 구간 시작일(정식일 등)",
                value=pd.Timestamp(daily["date"].min()).date() if not daily.empty else date.today(),
                key="fs_only")
            intervals = preprocess.build_intervals(
                pd.Series(survey_dates), first_start=pd.Timestamp(first_start_only),
                lag_days=int(lag_days), window_days=int(window_days) if window_days else None)
            env_interval = (preprocess.aggregate_intervals_by_treatment(daily, intervals, drop_incomplete)
                            if by_trt else
                            preprocess.aggregate_intervals(daily, intervals, drop_incomplete))

            st.subheader("③ 구간 정의 (시차 매칭 결과)")
            st.dataframe(
                intervals.assign(
                    조사일=lambda d: d["growth_date"].dt.strftime("%Y-%m-%d"),
                    환경구간=lambda d: d["start"].dt.strftime("%m-%d") + " ~ " + d["end"].dt.strftime("%m-%d"),
                )[["interval_id", "조사일", "환경구간", "days_expected", "lag_days"]]
                .rename(columns={"interval_id": "구간", "days_expected": "일수", "lag_days": "시차(일)"}),
                use_container_width=True, hide_index=True)

            bad = env_interval[env_interval["quality_flag"] != "정상"] if "quality_flag" in env_interval else pd.DataFrame()
            if not bad.empty:
                st.warning(f"품질 주의 구간 {len(bad)}개 — 표의 quality_flag 열 확인")

            st.subheader("④ 구간별 환경 요약")
            st.dataframe(env_interval, use_container_width=True, hide_index=True, height=260)
            e1, e2 = st.columns(2)
            with e1:
                df_download(env_interval, "구간 요약 CSV", "env_interval_summary.csv", "dl_iv_only")
            with e2:
                st.download_button(
                    "Excel 일괄 다운로드",
                    excel_bytes({"daily_env_summary": daily, "interval_definition": intervals,
                                 "env_interval_summary": env_interval, "column_mapping": map_report}),
                    file_name="preprocess_report.xlsx", key="dl_xl_only",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            st.caption("생육 측정값을 붙이려면 위에서 '생육 파일 업로드'를 선택하세요. "
                       "조사일이 같으면 그대로 병합됩니다.")

        if gfile is not None:
            growth = (pd.read_excel(gfile) if Path(gfile.name).suffix.lower() in (".xlsx", ".xls")
                      else pd.read_csv(gfile))
            date_col = st.selectbox("조사일 열", list(growth.columns),
                                    index=list(growth.columns).index("date") if "date" in growth.columns else 0)
            growth[date_col] = pd.to_datetime(growth[date_col])
            cadence = preprocess.detect_cadence(growth[date_col])
            first_start = st.date_input(
                "첫 구간 시작일(정식일 등)",
                value=pd.Timestamp(daily["date"].min()).date() if not daily.empty else date.today(),
                help="첫 조사일은 직전 조사일이 없으므로 시작일을 지정합니다.")

            st.success(f"조사 {growth[date_col].nunique()}회 · 추정 조사간격 **{cadence}일** "
                       f"(7·10일 자동 인식, 불규칙 간격도 그대로 처리)")

            intervals = preprocess.build_intervals(
                growth[date_col], first_start=pd.Timestamp(first_start),
                lag_days=int(lag_days), window_days=int(window_days) if window_days else None)

            trt_col = None
            if by_trt:
                cand = [c for c in growth.columns if c != date_col]
                trt_col = st.selectbox(
                    "생육 자료의 처리구 열", cand,
                    index=cand.index("trt") if "trt" in cand else 0,
                    help="이 열의 값이 sensor_map.yaml 의 처리구명과 같아야 병합됩니다.")
                env_interval = preprocess.aggregate_intervals_by_treatment(
                    daily, intervals, drop_incomplete)
                missing_trt = set(growth[trt_col].astype(str)) - set(env_interval["trt"].astype(str))
                if missing_trt:
                    st.error("매핑에 없는 생육 처리구: " + ", ".join(sorted(missing_trt)) +
                             " → sensor_map.yaml 의 처리구명을 생육자료와 일치시키세요.")
                merged = preprocess.match_growth(growth, env_interval,
                                                 date_col=date_col, trt_col=trt_col)
            else:
                env_interval = preprocess.aggregate_intervals(daily, intervals,
                                                              drop_incomplete_days=drop_incomplete)
                merged = preprocess.match_growth(growth, env_interval, date_col=date_col)

            st.subheader("③ 구간 정의 (시차 매칭 결과)")
            st.dataframe(
                intervals.assign(
                    조사일=lambda d: d["growth_date"].dt.strftime("%Y-%m-%d"),
                    환경구간=lambda d: d["start"].dt.strftime("%m-%d") + " ~ " + d["end"].dt.strftime("%m-%d"),
                )[["interval_id", "조사일", "환경구간", "days_expected", "lag_days"]]
                .rename(columns={"interval_id": "구간", "days_expected": "일수", "lag_days": "시차(일)"}),
                use_container_width=True, hide_index=True)

            bad = env_interval[env_interval["quality_flag"] != "정상"] if "quality_flag" in env_interval else pd.DataFrame()
            if not bad.empty:
                st.warning("품질 주의 구간: " + "; ".join(
                    f"구간{int(r['interval_id'])} {r['quality_flag']}" for _, r in bad.iterrows()))

            st.subheader("④ 구간별 환경 요약")
            st.dataframe(env_interval, use_container_width=True, hide_index=True, height=240)

            st.subheader("⑤ 생육 + 환경 병합")
            st.dataframe(merged, use_container_width=True, hide_index=True, height=240)

            d1, d2, d3 = st.columns(3)
            with d1:
                df_download(env_interval, "구간 요약 CSV", "env_interval_summary.csv", "dl_iv")
            with d2:
                df_download(merged, "병합 CSV", "merged_env_growth.csv", "dl_merged")
            with d3:
                st.download_button(
                    "Excel 일괄 다운로드",
                    excel_bytes({"daily_env_summary": daily, "interval_definition": intervals,
                                 "env_interval_summary": env_interval, "merged_env_growth": merged,
                                 "column_mapping": map_report,
                                 "treatment_mapping": map_coverage if not map_coverage.empty
                                 else pd.DataFrame({"note": ["처리구 매핑 미사용"]}),
                                 "out_of_range": range_report if not range_report.empty
                                 else pd.DataFrame({"note": ["없음"]})}),
                    file_name="preprocess_report.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

            # --- 환경-생육 관계 빠른 확인 --------------------------------
            with st.expander("환경 ↔ 생육 상관 빠르게 보기"):
                num_cols = [c for c in merged.columns
                            if pd.api.types.is_numeric_dtype(merged[c]) and c not in ("interval_id", "rep", "lag_days")]
                env_vars = [c for c in num_cols if c in env_interval.columns]
                growth_vars = [c for c in num_cols if c in growth.columns]
                if env_vars and growth_vars:
                    picks_g = st.multiselect("생육 형질", growth_vars, default=growth_vars[:2])
                    picks_e = st.multiselect("환경 변수", env_vars,
                                             default=[v for v in ("dli_sum", "temp_mean", "gdd_sum") if v in env_vars])
                    if picks_g and picks_e:
                        import altair as alt
                        corr = merged[picks_e + picks_g].corr(numeric_only=True).loc[picks_e, picks_g]
                        long_corr = corr.reset_index(names="환경변수").melt(
                            id_vars="환경변수", var_name="생육형질", value_name="r")
                        heat = (alt.Chart(long_corr).mark_rect().encode(
                            x=alt.X("생육형질:N", title=None),
                            y=alt.Y("환경변수:N", title=None),
                            color=alt.Color("r:Q", scale=alt.Scale(scheme="redblue", domain=[-1, 1])),
                            tooltip=["환경변수", "생육형질", alt.Tooltip("r:Q", format=".3f")])
                            .properties(height=28 * len(picks_e) + 40))
                        text = heat.mark_text(baseline="middle").encode(
                            text=alt.Text("r:Q", format=".2f"),
                            color=alt.value("#222"))
                        st.altair_chart(heat + text, use_container_width=True)
                        st.dataframe(corr.style.format("{:.3f}"), use_container_width=True)
                        st.caption("표본 수가 적으므로 상관계수는 경향 파악용입니다. "
                                   "정식 검정은 R 통계 워크플로우(agri-stats-workflow)로 진행하세요.")


# =====================================================================
# 3. 센서 정기검증 탭
# =====================================================================
with tab_ver:
    st.header("센서 정기 검증 루틴")
    st.caption("절차 상세는 docs/sensor_verification_routine.md 참조")
    with st.expander("❓ 이 화면 보는 법", expanded=False):
        st.markdown("""
1. **① 검증 기한 현황** — 센서별로 다음 점검일과 지연 여부. '지연/미실시'가 있으면 그것부터 처리합니다.
2. **② 현장 상호비교** — 두 센서를 24시간 나란히 두고 기록한 뒤, 두 열을 골라 실행하면
   편차(bias)·MAE·상관 r·합격 여부가 계산됩니다.
3. **③ 검증 결과 기록** — 점검·비교 결과를 남깁니다(편차·합격 판정 자동 계산).
4. **④ 검증 이력·드리프트** — 반복 기록이 쌓이면 연간 드리프트를 추정해 교체 시기를 판단합니다.
""")

    sched = cfg["verification"].get("schedule_days", {})
    cols = st.columns(len(sched))
    for col, (k, v) in zip(cols, sched.items()):
        col.metric(sensor_check.CHECK_LABELS.get(k, k), f"{v}일 주기")

    st.subheader("① 검증 기한 현황")
    log = sensor_check.load_log(cfg)
    if log.empty:
        st.info("검증 로그가 비어 있습니다. 아래 ③에서 첫 기록을 추가하세요.")
    else:
        status = sensor_check.due_status(cfg)
        st.dataframe(status, use_container_width=True, hide_index=True)
        overdue = status[status["상태"].isin(["지연", "미실시"])]
        if not overdue.empty:
            st.error(f"점검 지연/미실시 {len(overdue)}건 — "
                     + ", ".join(f"{r['sensor_id']}({r['점검종류']})" for _, r in overdue.head(5).iterrows()))

    st.subheader("② 현장 상호비교 (동일 조건 24시간 나란히 설치)")
    if grid is None:
        st.info("환경 파일을 불러오면 중복 센서 간 비교를 수행할 수 있습니다.")
    else:
        numcols = [c for c in grid.columns if c not in ("timestamp", "qc_status")]
        c1, c2, c3 = st.columns(3)
        col_ref = c1.selectbox("기준 센서 열", numcols, index=0)
        col_test = c2.selectbox("검증 대상 열", numcols, index=min(1, len(numcols) - 1))
        variable = c3.selectbox("변수 종류", list(cfg["sensors"].keys()),
                                format_func=lambda v: cfg["sensors"][v].get("label", v))
        c4, c5 = st.columns(2)
        d_start = c4.date_input("비교 시작", value=pd.Timestamp(grid["timestamp"].max()).date() - pd.Timedelta(days=1))
        d_end = c5.date_input("비교 종료", value=pd.Timestamp(grid["timestamp"].max()).date())
        daytime_only = st.checkbox("주간(09~16시)만 비교 — 광센서 권장", value=variable in ("ppfd", "solar"))

        if st.button("상호비교 실행"):
            res = sensor_check.cross_check(grid, col_ref, col_test, variable, cfg,
                                           start=d_start, end=pd.Timestamp(d_end) + pd.Timedelta(days=1),
                                           daytime_only=daytime_only)
            if "error" in res:
                st.error(res["error"])
            else:
                r1, r2, r3, r4 = st.columns(4)
                r1.metric("편차(bias)", f"{res['bias']:+.3f}")
                r2.metric("MAE", f"{res['MAE']:.3f}")
                r3.metric("상관 r", f"{res['r']:.4f}" if res["r"] is not None else "-")
                r4.metric("판정", "합격 ✅" if res["result"] == "pass" else
                          ("불합격 ❌" if res["result"] == "fail" else "기준없음"))
                st.caption(f"기준: {res['criterion']} · 비교 관측 {res['n']:,}쌍 · "
                           f"회귀 y = {res['slope']}x + {res['intercept']}")
                daily_pair = sensor_check.daily_pair_table(grid, col_ref, col_test)
                if not daily_pair.empty:
                    st.dataframe(daily_pair, use_container_width=True, hide_index=True)
                st.session_state["cross_result"] = res

    st.subheader("③ 검증 결과 기록")
    with st.form("verif_form"):
        f1, f2, f3, f4 = st.columns(4)
        v_date = f1.date_input("검증일", value=date.today())
        logger_id = f2.text_input("로거 ID", value="z6-")
        sensor_id = f3.text_input("센서 ID/포트", value="")
        sensor_type = f4.selectbox("센서 종류", ["SQ-521", "ATMOS 14", "TEROS 12", "PYR", "기타"])
        g1, g2, g3, g4 = st.columns(4)
        variable_r = g1.selectbox("변수", list(cfg["sensors"].keys()),
                                  format_func=lambda v: cfg["sensors"][v].get("label", v), key="var_rec")
        check_type = g2.selectbox("점검 종류", list(sensor_check.CHECK_LABELS.keys()),
                                  format_func=lambda k: sensor_check.CHECK_LABELS[k])
        ref_val = g3.number_input("기준값", value=0.0, format="%.4f")
        sen_val = g4.number_input("센서값", value=0.0, format="%.4f")
        h1, h2 = st.columns(2)
        operator = h1.text_input("점검자", value="")
        action = h2.text_input("조치 내용", value="")
        note = st.text_input("비고", value="")
        submitted = st.form_submit_button("검증 기록 저장")
        if submitted:
            sensor_check.append_log(cfg, {
                "date": v_date, "logger_id": logger_id, "sensor_id": sensor_id,
                "sensor_type": sensor_type, "variable": variable_r, "check_type": check_type,
                "reference_value": ref_val, "sensor_value": sen_val,
                "operator": operator, "action": action, "note": note,
            })
            st.success(f"저장 완료 → {resolve_path(cfg, cfg['verification']['log_file'])}")
            st.cache_data.clear()

    if not log.empty:
        st.subheader("④ 검증 이력 · 드리프트")
        df_download(log, "검증 로그 CSV", "sensor_verification_log.csv", "dl_log")
        ids = sorted(log["sensor_id"].dropna().astype(str).unique())
        if ids:
            pick = st.selectbox("센서 선택", ids)
            trend = sensor_check.drift_trend(cfg, pick)
            if "note" in trend:
                st.info(trend["note"])
            else:
                t1, t2, t3 = st.columns(3)
                t1.metric("연간 드리프트", f"{trend['drift_per_year']:+.4f}")
                t2.metric("최근 편차", f"{trend['last_deviation']:+.4f}")
                t3.metric("검증 횟수", trend["n"])
                st.dataframe(trend["history"], use_container_width=True, hide_index=True)


# =====================================================================
# 4. 설정 탭
# =====================================================================
with tab_cfg:
    st.header("설정")
    st.caption(f"설정 파일: {cfg.get('_path')}")
    with st.expander("❓ 이 화면 보는 법", expanded=False):
        st.markdown("""
- 여기 표시되는 값은 **`config/qc_config.yaml` 을 읽은 결과**입니다. 화면에서 바꾸는 게 아니라
  그 파일을 수정하고 새로고침(F5)하면 반영됩니다.
- 임계값을 바꾸고 싶을 때(예: 결측 경보 기준, 고온 경보 온도) 이 표에서 현재 값을 확인하세요.
- 알림 채널은 환경변수(`SLACK_WEBHOOK_URL`, `SMTP_*`)를 설정한 뒤 yaml 에서 `true` 로 켭니다.
""")

    st.subheader("센서 물리범위 · 이상 판정 임계값")
    sens = pd.DataFrame(cfg["sensors"]).T.reset_index().rename(columns={"index": "변수키"})
    st.dataframe(sens, use_container_width=True, hide_index=True)

    st.subheader("QC 규칙 임계값")
    flat = {k: v for k, v in cfg["qc"].items() if not isinstance(v, (dict, list))}
    st.dataframe(pd.DataFrame([flat]).T.reset_index().rename(columns={"index": "항목", 0: "값"}),
                 use_container_width=True, hide_index=True)
    st.json({k: v for k, v in cfg["qc"].items() if isinstance(v, (dict, list))}, expanded=False)

    st.subheader("알림 채널")
    ch = cfg["alerts"].get("channels", {})
    st.write(pd.DataFrame([{"채널": k, "활성": "✅" if v else "—"} for k, v in ch.items()]))
    st.caption("Slack: 환경변수 SLACK_WEBHOOK_URL · 이메일: SMTP_HOST/PORT/USER/PASSWORD 설정 후 "
               "config/qc_config.yaml 의 channels 를 true 로 바꾸세요.")
    if st.button("테스트 알림 발송"):
        test = pd.DataFrame([{
            "rule": "TEST", "level": "INFO", "variable": "-", "label": "테스트",
            "start": pd.Timestamp.now(), "end": pd.Timestamp.now(), "value": 0,
            "message": "알림 채널 연결 테스트입니다.", "detail": "",
            "key": f"TEST|{datetime.now():%Y%m%d%H%M%S}", "detected_at": pd.Timestamp.now(),
        }])
        text = alert_mod.format_text(test, cfg, {"start": "-", "end": "-", "n_rows": 0, "n_missing_ts": 0})
        ok_slack = alert_mod.send_slack(text) if ch.get("slack") else None
        ok_mail = alert_mod.send_email("테스트", text, cfg) if ch.get("email") else None
        st.code(text)
        st.write({"slack": ok_slack, "email": ok_mail})

    st.subheader("정기 검증 주기 / 허용오차")
    st.json(cfg["verification"], expanded=False)

    # =================================================================
    # 로거 번호 ↔ 구역 이름 (한 번 지정하면 계속 기억)
    # =================================================================
    st.divider()
    st.subheader("🏷️ 로거 번호 ↔ 구역 이름")
    st.caption("같은 센서 로거는 파일명이 매번 달라져도 **일련번호는 그대로**입니다. "
               "여기서 구역 이름을 한 번 지정하면 이후 업로드부터 자동으로 같은 구역으로 묶입니다.")

    reg = registry.load_registry()
    table = registry.as_table(reg)
    if table.empty:
        st.info("아직 등록된 로거가 없습니다. 파일을 한 번 통합(build_archive)하면 자동 등록됩니다.")
    else:
        st.dataframe(table, use_container_width=True, hide_index=True)
        unnamed = [s for s, e in (reg.get("loggers") or {}).items()
                   if not str((e or {}).get("zone", "")).strip()]
        if unnamed:
            st.warning(f"구역 미지정 {len(unnamed)}대: {', '.join(unnamed)} — 아래에서 이름을 지정하세요.")

        z1, z2, z3 = st.columns([2, 2, 1])
        pick_serial = z1.selectbox("로거 번호", list((reg.get("loggers") or {}).keys()), key="zone_serial")
        cur = str(((reg.get("loggers") or {}).get(pick_serial) or {}).get("zone", ""))
        new_zone = z2.text_input("구역 이름", value=cur, key="zone_name",
                                 placeholder="예: 3구역, 1온실-A")
        z3.write("")
        if z3.button("저장", key="zone_save"):
            registry.set_zone(reg, pick_serial, new_zone)
            registry.save_registry(reg)
            st.success(f"{pick_serial} → {new_zone or '(미지정)'} 저장. "
                       f"다음 통합부터 이 이름으로 묶입니다.")
            st.cache_data.clear()
            st.rerun()

        st.caption("여러 로거에 **같은 구역 이름**을 주면 한 구역으로 합쳐집니다"
                   "(같은 시각의 값이 한 행으로 병합되고, 같은 변수는 `__rep2` 로 나뉘어 둘 다 보존).")
        st.code('python scripts/build_archive.py --zone "22094002=7구역" --list-zones', language="bash")
