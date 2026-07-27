#!/usr/bin/env python3
"""환경데이터 QC 대시보드 (Streamlit) — 단계형 화면.

실행:
    streamlit run app/streamlit_app.py

화면은 **할 일 순서**로 되어 있다.
    1단계 자료 넣기(왼쪽) → 2단계 상태 점검 → 3단계 결과 만들기
    (+ 센서 점검, 설정)

전문용어는 화면에 쓰지 않는다. 규칙 코드·열 이름은 app/ui_text.py 에서
사람 말로 바꿔 보여주고, 원래 이름은 참고용으로만 작게 표시한다.
"""

from __future__ import annotations

import io
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import alerts as alert_mod          # noqa: E402
from src import archive, io_logger, preprocess, qc_rules, registry, sensor_check, sensor_map   # noqa: E402
from src.config import PROJECT_ROOT, load_config, resolve_path  # noqa: E402
import ui_text as T                          # noqa: E402

st.set_page_config(page_title="환경데이터 QC", page_icon="🌱", layout="wide")

STATUS_COLOR = {"정상": "#2e8b57", "주의": "#e08b3a", "위험": "#d64545"}


# =====================================================================
# 공통 도우미
# =====================================================================
def _process(raw, log, cfg, replicate, overrides):
    ts_df, ts_report = io_logger.prepare_timestamp(raw)
    interval = qc_rules.resolve_interval(cfg, ts_df)
    std, map_report = io_logger.standardize(ts_df, replicate=replicate, overrides=overrides)
    grid, gap_report = io_logger.reindex_full_grid(std, interval_minutes=interval)
    ts_report["interval_minutes"] = interval
    return grid, map_report, gap_report, ts_report, log


@st.cache_data(show_spinner=False)
def load_from_bytes(files: list[tuple[str, bytes]], _cfg: dict, replicate: str, overrides_key: tuple = ()):
    sources = [(io.BytesIO(b), name) for name, b in files]
    raw, log = io_logger.load_env_files(sources)
    return _process(raw, log, _cfg, replicate, dict(overrides_key))


@st.cache_data(show_spinner=False)
def load_from_paths(paths: tuple[str, ...], _cfg: dict, replicate: str, overrides_key: tuple = ()):
    raw, log = io_logger.load_env_files(list(paths))
    return _process(raw, log, _cfg, replicate, dict(overrides_key))


@st.cache_data(show_spinner=False)
def load_archive_master(arc_dir: str):
    return archive.load_master(arc_dir, clean=False)


def df_download(df: pd.DataFrame, label: str, filename: str, key: str | None = None,
                help: str | None = None):
    st.download_button(label, df.to_csv(index=False).encode("utf-8-sig"),
                       file_name=filename, mime="text/csv", key=key, help=help)


def excel_bytes(sheets: dict[str, pd.DataFrame]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        for name, df in sheets.items():
            if df is None:
                continue
            df.to_excel(w, sheet_name=name[:31], index=False)
    return buf.getvalue()


def dates_only(df: pd.DataFrame) -> pd.DataFrame:
    """날짜 칸에 00:00:00 이 붙어 지저분해 보이지 않게 날짜만 남긴다."""
    out = df.copy()
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            if (out[c].dt.time == pd.Timestamp("00:00").time()).all():
                out[c] = out[c].dt.strftime("%Y-%m-%d")
    return out


def show_table(df: pd.DataFrame, keys=None, key: str = "", height: int = 260,
               caption: str = ""):
    """표를 쉬운 이름·핵심 열만 보여주고, 원하면 전체 열도 볼 수 있게 한다."""
    if df is None or df.empty:
        st.info("표시할 자료가 없습니다.")
        return
    show_all = st.checkbox("전체 열 보기", value=False, key=f"all_{key}",
                           help="계산에 쓰인 모든 열을 그대로 봅니다(원래 열 이름).")
    view = dates_only(df if show_all else T.friendly_columns(df, keys))
    st.dataframe(view, use_container_width=True, hide_index=True, height=height)
    if caption:
        st.caption(caption)


def verdict(title: str, detail: str, level: str = "ok"):
    """화면 맨 위 '한 줄 결론'."""
    icon = {"ok": "✅", "warn": "🟠", "bad": "🔴", "info": "ℹ️"}[level]
    box = {"ok": st.success, "warn": st.warning, "bad": st.error, "info": st.info}[level]
    box(f"### {icon} {title}\n{detail}")


def next_step(text: str):
    st.markdown(f"<div style='margin-top:.6rem;padding:.6rem .9rem;border-left:4px solid #2e8b57;"
                f"background:#f3f8f4;border-radius:4px'>👉 <b>다음에 할 일</b> — {text}</div>",
                unsafe_allow_html=True)


# =====================================================================
# 왼쪽: 1단계 — 자료 넣기
# =====================================================================
cfg = load_config()
st.sidebar.title("🌱 환경데이터 QC")
st.sidebar.markdown("#### 1단계 · 자료 넣기")

source_mode = st.sidebar.radio(
    "어디에 있는 자료인가요?",
    ["내 컴퓨터 파일", "서버 폴더", "모아둔 전체 자료"],
    help="처음이라면 '내 컴퓨터 파일'을 고르고 로거에서 내려받은 파일을 그대로 올리세요.")

overrides = st.session_state.get("col_overrides", {})
overrides_key = tuple(sorted(overrides.items()))
grid = map_report = gap_report = ts_report = None
load_log: list[str] = []
replicate = "first"

if source_mode == "내 컴퓨터 파일":
    ups = st.sidebar.file_uploader(
        "로거에서 내려받은 파일을 올리세요",
        type=["xlsx", "xls", "csv", "txt", "tsv"], accept_multiple_files=True,
        help="엑셀·CSV 모두 가능하고 여러 개를 한 번에 올려도 됩니다. "
             "형식은 자동으로 알아봅니다.")
    if ups:
        payload = [(u.name, u.getvalue()) for u in ups]
        st.session_state["loaded_names"] = [u.name for u in ups]
        with st.spinner("파일을 읽는 중..."):
            grid, map_report, gap_report, ts_report, load_log = load_from_bytes(
                payload, cfg, replicate, overrides_key)

elif source_mode == "모아둔 전체 자료":
    arc_dir = st.sidebar.text_input("모아둔 자료 위치", value="outputs/archive",
                                    help="build_archive.py 로 만든 폴더입니다.")
    arc_path = PROJECT_ROOT / arc_dir if not Path(arc_dir).is_absolute() else Path(arc_dir)
    if (arc_path / archive.MASTER_NAME).exists():
        master_all = load_archive_master(str(arc_path))
        zones = sorted(master_all["logger"].unique()) if "logger" in master_all else ["(전체)"]
        pick_zone = st.sidebar.selectbox("구역 선택", zones)
        sub = master_all[master_all["logger"] == pick_zone] if "logger" in master_all else master_all
        sub = sub.drop(columns=[c for c in ("logger", "serial") if c in sub.columns]).dropna(axis=1, how="all")
        st.session_state["loaded_names"] = [f"{pick_zone}.xlsx"]
        interval_a = qc_rules.resolve_interval(cfg, sub)
        grid, gap_report = io_logger.reindex_full_grid(sub.reset_index(drop=True), interval_minutes=interval_a)
        map_report = pd.DataFrame()
        ts_report = {"timestamp_column": "timestamp", "duplicate_rows": 0,
                     "start": sub["timestamp"].min(), "end": sub["timestamp"].max(),
                     "n_rows": len(sub), "interval_minutes": interval_a}
        load_log = [f"{pick_zone} 구역 {len(sub):,}줄",
                    f"전체 {len(master_all):,}줄 · 구역 {master_all['logger'].nunique()}곳"]
    else:
        st.sidebar.error("모아둔 자료가 없습니다. 먼저 파일을 통합하세요.\n\n"
                         "`python scripts/build_archive.py --env \"data/**/*.xlsx\"`")
else:
    pattern = st.sidebar.text_input("파일 위치", value="data/*.xlsx",
                                    help="서버에 있는 파일 경로입니다. * 로 여러 개를 한 번에 지정합니다.")
    if st.sidebar.button("불러오기", type="primary", use_container_width=True):
        import glob
        paths = tuple(sorted(glob.glob(str(PROJECT_ROOT / pattern)
                                       if not Path(pattern).is_absolute() else pattern))
                      or sorted(glob.glob(pattern)))
        if not paths:
            st.sidebar.error("그 위치에 파일이 없습니다.")
        else:
            st.session_state["paths"] = paths
    if st.session_state.get("paths"):
        st.session_state["loaded_names"] = [Path(p).name for p in st.session_state["paths"]]
        with st.spinner("파일을 읽는 중..."):
            grid, map_report, gap_report, ts_report, load_log = load_from_paths(
                st.session_state["paths"], cfg, replicate, overrides_key)

# --- 자료를 읽었을 때: 무엇을 읽었는지 쉬운 말로 -------------------------
if grid is not None:
    n_days = grid["timestamp"].dt.date.nunique()
    detected = qc_rules.value_columns(grid)
    st.sidebar.success(
        f"**읽기 완료**\n\n"
        f"- 기간 {ts_report['start']:%Y-%m-%d} ~ {ts_report['end']:%Y-%m-%d} ({n_days}일)\n"
        f"- 측정 항목 {len(detected)}개\n"
        f"- {ts_report.get('interval_minutes', 10):g}분마다 기록")
    st.sidebar.caption("항목: " + ", ".join(T.var_name(c) for c in detected))

    with st.sidebar.expander("자동으로 알아본 내용 고치기"):
        st.caption("아래가 실제와 다를 때만 손대면 됩니다.")
        st.write(f"- 시간이 적힌 칸: `{ts_report['timestamp_column']}`")
        st.write(f"- 기록 간격: {ts_report.get('interval_minutes', 10):g}분")
        if map_report is not None and not map_report.empty:
            options = ["(자동)"] + io_logger.STANDARD_ORDER + ["제외"]
            src_cols = map_report["원본열"].astype(str).tolist()
            pick_col = st.selectbox("파일의 칸 이름", src_cols, key="ov_col")
            pick_var = st.selectbox("이 칸은 무엇인가요?", options, key="ov_var",
                                    format_func=lambda v: T.VARIABLE_TEXT.get(v, v))
            c1, c2 = st.columns(2)
            if c1.button("적용", key="ov_apply", use_container_width=True):
                ov = dict(st.session_state.get("col_overrides", {}))
                ov.pop(pick_col, None) if pick_var == "(자동)" else ov.update({pick_col: pick_var})
                st.session_state["col_overrides"] = ov
                st.rerun()
            if c2.button("되돌리기", key="ov_reset", use_container_width=True):
                st.session_state["col_overrides"] = {}
                st.rerun()
            if overrides:
                st.info("직접 지정: " + ", ".join(f"{k}→{T.VARIABLE_TEXT.get(v, v)}" for k, v in overrides.items()))
    with st.sidebar.expander("읽은 파일 목록"):
        for line in load_log:
            st.write("- " + line)

st.sidebar.divider()
st.sidebar.caption("도움말: `docs/dashboard_guide.md`")


# =====================================================================
# 자료가 없을 때 — 시작 안내 화면
# =====================================================================
if grid is None:
    st.title("🌱 환경데이터 QC")
    st.markdown("#### 온실 센서 자료를 확인하고, 생육조사 날짜에 맞춰 정리해 주는 도구입니다.")
    st.info("**왼쪽에서 파일을 올리면 시작됩니다.** 로거에서 내려받은 파일을 그대로 올리세요 "
            "(엑셀·CSV, 여러 개 가능). 형식·기록 간격·항목 이름은 자동으로 알아봅니다.")

    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("### 1️⃣ 자료 넣기")
        st.write("왼쪽에 파일을 올립니다. 여러 로거·여러 번 내려받은 파일을 한꺼번에 넣어도 됩니다.")
        st.caption("같은 로거는 번호로 알아보고 같은 구역으로 묶습니다.")
    with c2:
        st.markdown("### 2️⃣ 상태 점검")
        st.write("빠진 자료·고장 의심 센서를 자동으로 찾아 **무엇을 해야 하는지**까지 알려줍니다.")
        st.caption("문제가 없으면 '이상 없음'만 뜹니다.")
    with c3:
        st.markdown("### 3️⃣ 결과 만들기")
        st.write("조사 날짜만 정하면 그 사이 환경을 자동으로 묶어 **분석용 표**를 만들어 줍니다.")
        st.caption("엑셀·CSV로 바로 내려받습니다.")

    st.divider()
    with st.expander("어떤 파일을 넣을 수 있나요?"):
        st.markdown("""
- **METER ZL6**(ZENTRA Cloud 내려받기), **HOBO**(한글 CSV), 국산 로거, 직접 만든 엑셀·CSV
- 기록 간격 1·5·10·15·30·60분 — 자동으로 알아봅니다
- 시트가 여러 개(`Config 1`, `Config 2`)로 나뉜 엑셀도 전부 읽어 합칩니다
- 날짜·시간 칸만 있으면 됩니다. 온도·습도·광량 같은 항목 이름은 표기가 달라도 인식합니다
- 모르는 항목(수온·pH·풍속 등)도 버리지 않고 그대로 정리합니다
""")
    st.stop()


# =====================================================================
# 자료가 있을 때 — 단계별 화면
# =====================================================================
tab_check, tab_result, tab_sensor, tab_setting = st.tabs(
    ["2️⃣ 상태 점검", "3️⃣ 결과 만들기", "🔬 센서 점검", "⚙️ 설정"])


# ---------------------------------------------------------------------
# 2단계 — 상태 점검
# ---------------------------------------------------------------------
with tab_check:
    st.header("2단계 · 자료 상태 점검")
    st.caption("빠진 자료와 고장 의심 센서를 자동으로 찾습니다. 아무것도 누르지 않아도 됩니다.")

    c1, c2 = st.columns([1, 3])
    lookback = c1.number_input("최근 며칠을 볼까요?", 1, 400, 7,
                               help="기본 7일. 과거 전체를 보려면 숫자를 크게 하세요.")
    alerts = qc_rules.run_all(grid, cfg, lookback_days=int(lookback), map_report=map_report)
    health = qc_rules.health_score(grid, cfg, days=int(lookback))
    summary = qc_rules.summarize(alerts)
    n_bad, n_warn = summary["CRITICAL"], summary["WARN"]

    # --- 한 줄 결론 ---------------------------------------------------
    if n_bad:
        top = alerts[alerts["level"] == "CRITICAL"].iloc[0]
        verdict(f"조치가 필요한 문제 {n_bad}건",
                f"가장 급한 것: **{T.rule_title(top['rule'])}** — {T.var_name(top['variable'])} "
                f"({top['start']:%m월 %d일})" if pd.notna(top["start"]) else
                f"가장 급한 것: **{T.rule_title(top['rule'])}**", "bad")
    elif n_warn:
        verdict(f"확인해 볼 것 {n_warn}건", "급하지는 않습니다. 주간 점검 때 함께 보세요.", "warn")
    else:
        verdict("이상 없습니다", f"최근 {int(lookback)}일 자료에서 문제를 찾지 못했습니다.", "ok")

    n_missing_ts = int((grid["qc_status"] == "missing_timestamp_inserted").sum())
    m1, m2, m3 = c2.columns(3)
    m1.metric("측정 항목", f"{len(qc_rules.value_columns(grid))}개")
    m2.metric("관측 일수", f"{grid['timestamp'].dt.date.nunique()}일")
    m3.metric("빠진 기록", f"{n_missing_ts:,}개",
              delta=f"{n_missing_ts / max(len(grid), 1):.1%}", delta_color="inverse")

    st.divider()

    # --- 할 일 목록 ---------------------------------------------------
    st.subheader("무엇을 하면 되나요?")
    if alerts.empty:
        st.success("할 일이 없습니다. 자료가 정상입니다.")
    else:
        for level in ("CRITICAL", "WARN", "INFO"):
            sub = alerts[alerts["level"] == level]
            if sub.empty or (level == "INFO" and not st.session_state.get("show_info")):
                continue
            icon, label = T.LEVEL_TEXT[level]
            st.markdown(f"**{icon} {label} ({len(sub)}건)**")
            for rule, grp in sub.groupby("rule", sort=False):
                first = grp.iloc[0]
                when = ""
                if pd.notna(first["start"]):
                    when = f"{first['start']:%m월 %d일}"
                    if len(grp) > 1:
                        when += f" 외 {len(grp) - 1}건"
                who = ", ".join(sorted({T.var_name(v) for v in grp["variable"]})[:4])
                with st.expander(f"{icon} {T.rule_title(rule)} — {who} {('· ' + when) if when else ''}"):
                    st.write(f"**무슨 뜻인가요?** {T.rule_meaning(rule)}")
                    st.write(f"**어떻게 하나요?** {T.rule_action(rule)}")
                    detail = grp[["variable", "start", "end", "message"]].copy()
                    detail["variable"] = detail["variable"].map(T.var_name)
                    detail = detail.rename(columns={"variable": "항목", "start": "시작",
                                                    "end": "끝", "message": "자세히"})
                    st.dataframe(detail, use_container_width=True, hide_index=True, height=140)
                    st.caption(f"규칙 코드: `{rule}`")
        st.checkbox("참고 항목도 보기", key="show_info")
        df_download(alerts, "⬇ 점검 결과 내려받기(CSV)", f"점검결과_{datetime.now():%Y%m%d}.csv", "dl_alerts")

    # --- 항목별 상태 ---------------------------------------------------
    st.subheader("항목별 상태")
    if not health.empty:
        h = health.copy()
        h["항목"] = h["키"].map(T.var_name)
        view = h[["항목", "상태", "수신율", "최근값", "평균", "최소", "최대", "마지막관측"]]
        st.dataframe(
            view.style.map(lambda v: f"color:{STATUS_COLOR.get(v, '')};font-weight:600"
                           if v in STATUS_COLOR else "", subset=["상태"])
                .format({"수신율": "{:.1%}", "최근값": "{:.2f}", "평균": "{:.2f}",
                         "최소": "{:.2f}", "최대": "{:.2f}"}),
            use_container_width=True, hide_index=True)
        st.caption("수신율 = 그 기간에 들어와야 할 기록 중 실제로 들어온 비율입니다.")

    # --- 눈으로 확인 ---------------------------------------------------
    with st.expander("그래프로 확인하기"):
        varcols = [c for c in qc_rules.value_columns(grid) if grid[c].notna().any()]
        sel = st.multiselect("보고 싶은 항목", varcols,
                             default=varcols[:2] if len(varcols) >= 2 else varcols,
                             format_func=T.var_name)
        if sel:
            days = st.slider("최근 며칠", 1, 60, min(14, max(1, grid["timestamp"].dt.date.nunique())))
            sub = grid[grid["timestamp"] > grid["timestamp"].max() - pd.Timedelta(days=days)]
            st.line_chart(sub.set_index("timestamp")[sel], height=280)

        if varcols:
            st.markdown("**날짜별로 자료가 얼마나 비었는지**")
            interval = ts_report.get("interval_minutes", 10)
            expected = max(int(round(24 * 60 / interval)), 1)
            tmp = grid.copy()
            tmp["date"] = tmp["timestamp"].dt.date
            miss = (1 - tmp.groupby("date")[varcols].count() / expected).round(3).reset_index()
            miss["date"] = pd.to_datetime(miss["date"]).dt.strftime("%m-%d")
            long = miss.melt(id_vars="date", var_name="항목", value_name="빈 비율")
            long["항목"] = long["항목"].map(T.var_name)
            import altair as alt
            st.altair_chart(
                alt.Chart(long).mark_rect().encode(
                    x=alt.X("date:O", title="날짜", axis=alt.Axis(labelAngle=-60, labelOverlap=True)),
                    y=alt.Y("항목:N", title=None),
                    color=alt.Color("빈 비율:Q", scale=alt.Scale(scheme="orangered", domain=[0, 1]),
                                    legend=alt.Legend(format=".0%")),
                    tooltip=["date:O", "항목:N", alt.Tooltip("빈 비율:Q", format=".1%")])
                .properties(height=26 * len(varcols) + 40), use_container_width=True)
            st.caption("진한 칸일수록 그날 그 항목이 많이 비었다는 뜻입니다.")

    # --- 알림 보내기 ---------------------------------------------------
    with st.expander("점검 결과를 메일·Slack으로 보내기"):
        ch = cfg["alerts"].get("channels", {})
        st.caption("켜져 있는 채널: " + (", ".join(k for k, v in ch.items() if v) or "없음") +
                   f" · 같은 문제는 {cfg['alerts'].get('cooldown_hours')}시간 안에 다시 보내지 않습니다.")
        ctx = {"start": str(ts_report["start"]), "end": str(ts_report["end"]),
               "n_rows": int(ts_report["n_rows"]), "n_missing_ts": n_missing_ts}
        b1, b2 = st.columns(2)
        if b1.button("지금 보내기", use_container_width=True):
            res = alert_mod.dispatch(alerts, cfg, context=ctx, health=health)
            st.success(f"{res['n_sent']}건 보냈습니다. 기록: {res['report_path']}")
        if b2.button("보내지 않고 미리보기", use_container_width=True):
            st.markdown(alert_mod.format_markdown(alerts, cfg, ctx, health))

    next_step("문제가 없으면 위쪽 <b>3️⃣ 결과 만들기</b> 탭으로 넘어가세요.")


# ---------------------------------------------------------------------
# 3단계 — 결과 만들기
# ---------------------------------------------------------------------
with tab_result:
    st.header("3단계 · 생육조사 날짜에 맞춰 정리하기")
    st.caption("10분마다 쌓인 자료를 조사 날짜 사이 구간으로 묶어, 분석에 바로 쓰는 표를 만듭니다.")

    pcfg = cfg["preprocess"]

    # --- 고급 설정(기본값으로 두어도 됨) --------------------------------
    with st.expander("⚙️ 세부 설정 (그대로 두어도 됩니다)"):
        s1, s2, s3 = st.columns(3)
        gdd_base = s1.number_input("적산온도 기준온도(℃)", 0.0, 20.0,
                                   float(pcfg.get("gdd_base", 10.0)), 0.5,
                                   help="이 온도를 넘은 만큼만 적산합니다. 엽채류·들깨는 보통 10℃.")
        lag_days = s2.number_input("시차(일)", 0, 30, int(pcfg.get("lag_days", 0)),
                                   help="환경 효과가 며칠 뒤 나타난다고 볼 때만 씁니다. 보통 0.")
        use_window = s3.checkbox("조사일 직전 며칠만 쓰기", value=bool(pcfg.get("window_days")))
        window_days = s3.number_input("며칠", 1, 60, int(pcfg.get("window_days") or 10)) if use_window else None
        s4, s5 = st.columns(2)
        mask_range = s4.checkbox("이상한 값 빼고 계산", value=True,
                                 help="-99.9 같은 오류값이 평균을 망치지 않게 합니다.")
        drop_incomplete = s5.checkbox("자료가 많이 빈 날 제외",
                                      value=bool(pcfg.get("drop_incomplete_days", False)))

    clean = grid.drop(columns=["qc_status"])
    range_report = pd.DataFrame()
    if mask_range:
        clean, range_report = preprocess.mask_out_of_range(clean, cfg["sensors"])

    daily_kwargs = dict(
        interval_minutes=ts_report.get("interval_minutes", None),
        gdd_base=float(gdd_base),
        photoperiod_ppfd_threshold=float(pcfg.get("photoperiod_ppfd_threshold", 10)),
        daytime_hours=tuple(pcfg.get("daytime_hours", [9, 15])),
        min_completeness=float(pcfg.get("daily_min_completeness", 0.9)),
    )

    # --- 처리구 나누기(해당될 때만) --------------------------------------
    smap = sensor_map.load_sensor_map()
    logger_names = list((smap.get("loggers") or {}).keys())
    by_trt, map_coverage, frames = False, pd.DataFrame(), {}
    if logger_names:
        with st.expander("한 로거로 여러 처리구를 재고 있나요?"):
            st.caption("센서마다 다른 처리구를 재고 있으면 처리구별로 나눠서 계산합니다. "
                       "`config/sensor_map.yaml` 에 어느 포트가 어느 처리구인지 적어 두어야 합니다.")
            loaded = (st.session_state.get("loaded_names") or [""])[0]
            guess = sensor_map.logger_id_from_filename(loaded)
            idx = next((i for i, n in enumerate(logger_names) if n in guess or guess in n), 0)
            use_map = st.checkbox("처리구별로 나누기", value=False)
            logger_key = st.selectbox("어느 로거인가요?", logger_names, index=idx, disabled=not use_map)
            if use_map:
                entry = (smap.get("loggers") or {}).get(logger_key) or {}
                frames = sensor_map.split_by_treatment(clean, entry)
                if not frames:
                    st.error(f"'{logger_key}' 에 처리구 정보가 없습니다.")
                else:
                    map_coverage = sensor_map.coverage_report(clean, entry)
                    daily = preprocess.to_daily_by_treatment(frames, **daily_kwargs)
                    by_trt = True
                    st.success(f"처리구 {len(frames)}개로 나눴습니다: {', '.join(frames)}")

    if not by_trt:
        daily = preprocess.to_daily(clean, **daily_kwargs)

    # --- 조사 날짜 정하기 ------------------------------------------------
    st.subheader("조사 날짜만 정해 주세요")
    mode = st.radio("어떻게 정할까요?",
                    ["시작일과 간격만 알려주기", "조사한 날짜를 직접 적기", "생육조사 파일 올리기"],
                    horizontal=True,
                    help="생육 자료가 아직 없어도 앞의 두 방법으로 환경 표를 만들 수 있습니다.")

    survey_dates, growth, gfile = None, None, None
    default_start = pd.Timestamp(daily["date"].min()).date() if not daily.empty else date.today()
    default_end = pd.Timestamp(daily["date"].max()).date() if not daily.empty else date.today()

    if mode == "시작일과 간격만 알려주기":
        q1, q2, q3, q4 = st.columns(4)
        sv_start = q1.date_input("첫 조사일", value=default_start,
                                 help="정식일이나 첫 조사일을 넣으세요.")
        sv_interval = q2.number_input("며칠마다 조사하나요?", 1, 60, 10)
        end_mode = q3.selectbox("어디까지?", ["횟수로", "날짜로"])
        if end_mode == "횟수로":
            sv_count = q4.number_input("몇 번 조사하나요?", 2, 60, 6)
            survey_dates = preprocess.parse_survey_dates(start=sv_start, interval=int(sv_interval),
                                                         count=int(sv_count))
        else:
            sv_end = q4.date_input("마지막 조사일", value=default_end)
            survey_dates = preprocess.parse_survey_dates(start=sv_start, interval=int(sv_interval),
                                                         end=sv_end)
        st.caption("조사일: " + ", ".join(d.strftime("%m월 %d일") for d in survey_dates))

    elif mode == "조사한 날짜를 직접 적기":
        txt = st.text_area("조사한 날짜를 쉼표로 적어 주세요", value="", height=90,
                           placeholder="2026-04-01, 2026-04-11, 2026-04-21")
        if txt.strip():
            try:
                survey_dates = preprocess.parse_survey_dates(dates=txt)
                st.caption(f"조사 {len(survey_dates)}번 · 보통 "
                           f"{preprocess.detect_cadence(pd.Series(survey_dates))}일 간격")
            except ValueError as e:
                st.error(str(e))
        else:
            st.info("날짜가 며칠씩 밀렸어도 **실제 조사한 날짜**를 그대로 적으면 됩니다.")

    else:
        gfile = st.file_uploader("생육조사 파일 (엑셀·CSV)", type=["csv", "xlsx", "xls"], key="growth_up")
        if gfile is None:
            st.info("조사일이 적힌 칸(예: `date`)이 있는 파일을 올리면, 생육값까지 함께 붙여 드립니다.")

    # --- 결과 만들기 -----------------------------------------------------
    def _render_result(intervals, env_interval, merged=None):
        st.divider()
        st.subheader("만들어진 결과")
        good = (env_interval["quality_flag"] == "정상").sum() if "quality_flag" in env_interval else len(env_interval)
        bad = len(env_interval) - good
        if bad:
            verdict(f"구간 {len(env_interval)}개 완성 (주의 {bad}개)",
                    "주의 구간은 그 기간 환경자료가 부족합니다. 표의 '품질' 칸을 확인하세요.", "warn")
        else:
            verdict(f"구간 {len(env_interval)}개 완성", "모든 구간에 환경자료가 충분합니다.", "ok")

        st.markdown("**① 어떤 기간이 어느 조사일에 붙었나요?**")
        view = intervals.assign(
            **{"조사일": lambda d: d["growth_date"].dt.strftime("%Y-%m-%d"),
               "사용한 환경 기간": lambda d: (d["start"].dt.strftime("%m월 %d일") + " ~ "
                                       + d["end"].dt.strftime("%m월 %d일"))}
        )[["interval_id", "조사일", "사용한 환경 기간", "days_expected"]].rename(
            columns={"interval_id": "구간", "days_expected": "일수"})
        st.dataframe(view, use_container_width=True, hide_index=True)

        st.markdown("**② 구간별 환경 요약** — 이 표가 분석에 쓰는 결과입니다.")
        show_table(env_interval, T.INTERVAL_KEYS, key="iv", height=280,
                   caption="누적광량 = 그 구간 동안 받은 빛의 총량(DLI 합계). "
                           "품질이 '정상'이 아닌 구간은 분석에서 빼는 것이 좋습니다.")

        if merged is not None:
            st.markdown("**③ 생육값 + 환경** — 통계·그래프에 바로 넣는 최종 표입니다.")
            st.dataframe(dates_only(merged).head(200), use_container_width=True,
                         hide_index=True, height=260)
            if len(merged) > 200:
                st.caption(f"화면에는 200줄만 보입니다(전체 {len(merged):,}줄은 내려받기에 포함).")

        st.markdown("**내려받기**")
        d1, d2, d3 = st.columns(3)
        with d1:
            df_download(env_interval, "⬇ 구간 환경 (CSV)", "구간환경.csv", "dl_iv2")
        with d2:
            if merged is not None:
                df_download(merged, "⬇ 최종 표 (CSV)", "생육_환경_병합.csv", "dl_mg2")
            else:
                df_download(daily, "⬇ 하루별 요약 (CSV)", "하루별요약.csv", "dl_daily2")
        with d3:
            sheets = {"하루별요약": daily, "구간정의": intervals, "구간환경": env_interval}
            if merged is not None:
                sheets["생육_환경"] = merged
            st.download_button("⬇ 엑셀 한 번에", excel_bytes(sheets),
                               file_name="환경정리결과.xlsx", key="dl_xl2",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               use_container_width=True)
        if merged is not None:
            next_step("내려받은 <code>생육_환경_병합.csv</code> 를 R·엑셀에서 그대로 분석하면 됩니다.")
        else:
            next_step("내려받은 <code>구간환경.csv</code> 를 R·엑셀에서 그대로 분석하면 됩니다. "
                      "생육 값까지 한 표로 붙이려면 위에서 <b>생육조사 파일 올리기</b>를 고르세요.")

    if survey_dates is not None and len(survey_dates) >= 1:
        first_start = st.date_input("첫 구간은 언제부터 계산할까요?", value=default_start,
                                    help="정식일을 넣으면 됩니다. 첫 조사일 이전 기간을 어디서부터 볼지 정합니다.",
                                    key="fs_only")
        intervals = preprocess.build_intervals(
            pd.Series(survey_dates), first_start=pd.Timestamp(first_start),
            lag_days=int(lag_days), window_days=int(window_days) if window_days else None)
        env_interval = (preprocess.aggregate_intervals_by_treatment(daily, intervals, drop_incomplete)
                        if by_trt else preprocess.aggregate_intervals(daily, intervals, drop_incomplete))
        _render_result(intervals, env_interval)

    elif gfile is not None:
        growth = (pd.read_excel(gfile) if Path(gfile.name).suffix.lower() in (".xlsx", ".xls")
                  else pd.read_csv(gfile))
        g1, g2 = st.columns(2)
        date_col = g1.selectbox("조사일이 적힌 칸", list(growth.columns),
                                index=list(growth.columns).index("date") if "date" in growth.columns else 0)
        growth[date_col] = pd.to_datetime(growth[date_col])
        trt_col = None
        if by_trt:
            cand = [c for c in growth.columns if c != date_col]
            trt_col = g2.selectbox("처리구가 적힌 칸", cand,
                                   index=cand.index("trt") if "trt" in cand else 0)
        first_start = st.date_input("첫 구간은 언제부터 계산할까요?", value=default_start, key="fs_growth")
        st.success(f"조사 {growth[date_col].nunique()}번 · 보통 "
                   f"{preprocess.detect_cadence(growth[date_col])}일 간격으로 조사하셨네요.")

        intervals = preprocess.build_intervals(
            growth[date_col], first_start=pd.Timestamp(first_start),
            lag_days=int(lag_days), window_days=int(window_days) if window_days else None)
        if by_trt:
            env_interval = preprocess.aggregate_intervals_by_treatment(daily, intervals, drop_incomplete)
            missing_trt = set(growth[trt_col].astype(str)) - set(env_interval["trt"].astype(str))
            if missing_trt:
                st.error("생육 자료에는 있는데 센서 쪽에 없는 처리구: " + ", ".join(sorted(missing_trt)))
            merged = preprocess.match_growth(growth, env_interval, date_col=date_col, trt_col=trt_col)
        else:
            env_interval = preprocess.aggregate_intervals(daily, intervals, drop_incomplete)
            merged = preprocess.match_growth(growth, env_interval, date_col=date_col)
        _render_result(intervals, env_interval, merged)

    # --- 하루별 요약(참고) -----------------------------------------------
    with st.expander("하루별 요약도 보기 (참고)"):
        n_bad_day = int((~daily["is_complete"]).sum()) if "is_complete" in daily else 0
        st.caption(f"{daily['date'].nunique()}일 · 자료가 부족한 날 {n_bad_day}건")
        show_table(daily, T.DAILY_KEYS, key="daily", height=260,
                   caption="DLI = 하루 동안 작물이 받은 빛의 총량입니다.")
        if not range_report.empty:
            st.caption("이상한 값 제외: " +
                       ", ".join(f"{r['변수']} {r['결측처리건수']:,}개" for _, r in range_report.iterrows()))


# ---------------------------------------------------------------------
# 센서 점검
# ---------------------------------------------------------------------
with tab_sensor:
    st.header("센서 점검")
    st.caption("센서를 믿어도 되는지 정기적으로 확인하고 기록해 둡니다.")

    log = sensor_check.load_log(cfg)
    if log.empty:
        verdict("아직 점검 기록이 없습니다", "아래 ②에서 첫 기록을 남기면 다음 점검일이 자동으로 계산됩니다.", "info")
    else:
        status = sensor_check.due_status(cfg)
        overdue = status[status["상태"].isin(["지연", "미실시"])]
        if len(overdue):
            verdict(f"점검할 때가 지난 센서 {len(overdue)}대",
                    ", ".join(f"{r['sensor_id']}({r['점검종류']})" for _, r in overdue.head(4).iterrows()), "warn")
        else:
            verdict("점검 일정 정상", "지연된 점검이 없습니다.", "ok")
        st.subheader("① 점검 일정")
        st.dataframe(status, use_container_width=True, hide_index=True)

    st.subheader("② 두 센서 비교하기")
    st.caption("센서 두 개를 하루 동안 나란히 두고 기록한 뒤, 두 값을 비교해 오차를 봅니다.")
    numcols = [c for c in grid.columns if c not in ("timestamp", "qc_status")]
    c1, c2, c3 = st.columns(3)
    col_ref = c1.selectbox("기준이 되는 센서", numcols, index=0, format_func=T.var_name)
    col_test = c2.selectbox("확인할 센서", numcols, index=min(1, len(numcols) - 1), format_func=T.var_name)
    variable = c3.selectbox("무엇을 재는 센서인가요?", list(cfg["sensors"].keys()),
                            format_func=lambda v: T.VARIABLE_TEXT.get(v, v))
    c4, c5 = st.columns(2)
    d_start = c4.date_input("비교 시작", value=pd.Timestamp(grid["timestamp"].max()).date() - pd.Timedelta(days=1))
    d_end = c5.date_input("비교 끝", value=pd.Timestamp(grid["timestamp"].max()).date())
    daytime_only = st.checkbox("낮 시간만 비교(광센서는 권장)", value=variable in ("ppfd", "solar"))

    if st.button("비교하기", type="primary"):
        res = sensor_check.cross_check(grid, col_ref, col_test, variable, cfg,
                                       start=d_start, end=pd.Timestamp(d_end) + pd.Timedelta(days=1),
                                       daytime_only=daytime_only)
        if "error" in res:
            st.error(res["error"])
        else:
            ok = res["result"] == "pass"
            verdict("합격" if ok else ("불합격" if res["result"] == "fail" else "판정 기준 없음"),
                    f"두 센서 차이 **{res['bias']:+.3f}** · 기준 {res['criterion']} · "
                    f"함께 움직인 정도 r={res['r']}", "ok" if ok else "warn")
            r1, r2, r3 = st.columns(3)
            r1.metric("평균 차이", f"{res['bias']:+.3f}")
            r2.metric("평균 오차 크기", f"{res['MAE']:.3f}")
            r3.metric("비교한 관측 수", f"{res['n']:,}")
            pair = sensor_check.daily_pair_table(grid, col_ref, col_test)
            if not pair.empty:
                st.dataframe(pair, use_container_width=True, hide_index=True)

    st.subheader("③ 점검 결과 남기기")
    with st.form("verif_form"):
        f1, f2, f3, f4 = st.columns(4)
        v_date = f1.date_input("점검한 날", value=date.today())
        logger_id = f2.text_input("로거 번호", value="")
        sensor_id = f3.text_input("센서 번호(또는 포트)", value="")
        sensor_type = f4.selectbox("센서 종류", ["SQ-521", "ATMOS 14", "TEROS 12", "PYR", "기타"])
        g1, g2, g3, g4 = st.columns(4)
        variable_r = g1.selectbox("무엇을 재나요?", list(cfg["sensors"].keys()),
                                  format_func=lambda v: T.VARIABLE_TEXT.get(v, v), key="var_rec")
        check_type = g2.selectbox("어떤 점검인가요?", list(sensor_check.CHECK_LABELS.keys()),
                                  format_func=lambda k: sensor_check.CHECK_LABELS[k])
        ref_val = g3.number_input("기준기 값", value=0.0, format="%.4f")
        sen_val = g4.number_input("센서 값", value=0.0, format="%.4f")
        h1, h2 = st.columns(2)
        operator = h1.text_input("점검한 사람", value="")
        action = h2.text_input("한 일(청소·재설치 등)", value="")
        note = st.text_input("메모", value="")
        if st.form_submit_button("기록 저장", type="primary"):
            sensor_check.append_log(cfg, {
                "date": v_date, "logger_id": logger_id, "sensor_id": sensor_id,
                "sensor_type": sensor_type, "variable": variable_r, "check_type": check_type,
                "reference_value": ref_val, "sensor_value": sen_val,
                "operator": operator, "action": action, "note": note})
            st.success("저장했습니다. 다음 점검일이 자동으로 계산됩니다.")
            st.cache_data.clear()

    if not log.empty:
        with st.expander("지난 점검 기록 · 센서 노화 추이"):
            df_download(log, "⬇ 점검 기록 (CSV)", "센서점검기록.csv", "dl_log")
            ids = sorted(log["sensor_id"].dropna().astype(str).unique())
            if ids:
                pick = st.selectbox("센서 선택", ids)
                trend = sensor_check.drift_trend(cfg, pick)
                if "note" in trend:
                    st.info(trend["note"])
                else:
                    t1, t2, t3 = st.columns(3)
                    t1.metric("1년에 벌어지는 오차", f"{trend['drift_per_year']:+.4f}")
                    t2.metric("최근 오차", f"{trend['last_deviation']:+.4f}")
                    t3.metric("점검 횟수", trend["n"])
                    st.dataframe(trend["history"], use_container_width=True, hide_index=True)

    next_step("점검 절차와 합격 기준은 <code>docs/sensor_verification_routine.md</code> 에 정리돼 있습니다.")


# ---------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------
with tab_setting:
    st.header("설정")

    st.subheader("🏷️ 로거 번호 ↔ 구역 이름")
    st.caption("같은 로거는 파일 이름이 달라져도 번호로 알아봅니다. "
               "여기서 구역 이름을 한 번 정해 두면 다음부터 자동으로 그 구역으로 묶입니다.")
    reg = registry.load_registry()
    table = registry.as_table(reg)
    if table.empty:
        st.info("아직 등록된 로거가 없습니다. 파일을 한 번 통합하면 자동으로 등록됩니다.")
    else:
        st.dataframe(table, use_container_width=True, hide_index=True)
        unnamed = [s for s, e in (reg.get("loggers") or {}).items()
                   if not str((e or {}).get("zone", "")).strip()]
        if unnamed:
            st.warning(f"구역 이름이 없는 로거 {len(unnamed)}대: {', '.join(unnamed)}")
        z1, z2, z3 = st.columns([2, 2, 1])
        pick_serial = z1.selectbox("로거 번호", list((reg.get("loggers") or {}).keys()), key="zone_serial")
        cur = str(((reg.get("loggers") or {}).get(pick_serial) or {}).get("zone", ""))
        new_zone = z2.text_input("이 로거는 어느 구역인가요?", value=cur, key="zone_name",
                                 placeholder="예: 3구역, 1온실-A")
        z3.write("")
        if z3.button("저장", key="zone_save", use_container_width=True):
            registry.set_zone(reg, pick_serial, new_zone)
            registry.save_registry(reg)
            st.success(f"{pick_serial} → {new_zone or '(미지정)'} 저장했습니다.")
            st.cache_data.clear()
            st.rerun()
        st.caption("여러 로거에 **같은 구역 이름**을 주면 한 구역 자료로 합쳐집니다.")

    st.divider()
    st.subheader("경보 기준")
    st.caption(f"아래 값은 `{cfg.get('_path')}` 를 읽은 것입니다. "
               f"바꾸려면 그 파일을 고치고 브라우저에서 F5 를 누르세요.")
    sens = pd.DataFrame(cfg["sensors"]).T.reset_index().rename(columns={"index": "항목"})
    sens["항목"] = sens["항목"].map(lambda v: T.VARIABLE_TEXT.get(v, v))
    st.dataframe(sens[["항목", "label", "unit", "min", "max"]].rename(
        columns={"label": "이름", "unit": "단위", "min": "가능한 최소", "max": "가능한 최대"}),
        use_container_width=True, hide_index=True)

    with st.expander("자세한 규칙 값 보기"):
        flat = {k: v for k, v in cfg["qc"].items() if not isinstance(v, (dict, list))}
        st.dataframe(pd.DataFrame([flat]).T.reset_index().rename(columns={"index": "항목", 0: "값"}),
                     use_container_width=True, hide_index=True)
        st.json({k: v for k, v in cfg["qc"].items() if isinstance(v, (dict, list))}, expanded=False)

    st.subheader("알림 보내는 곳")
    ch = cfg["alerts"].get("channels", {})
    st.write(pd.DataFrame([{"채널": k, "켜짐": "✅" if v else "—"} for k, v in ch.items()]))
    st.caption("Slack 은 `SLACK_WEBHOOK_URL`, 메일은 `SMTP_*` 환경변수를 넣은 뒤 "
               "설정 파일에서 켜면 됩니다.")
    if st.button("시험 삼아 한 통 보내기"):
        test = pd.DataFrame([{
            "rule": "TEST", "level": "INFO", "variable": "-", "label": "테스트",
            "start": pd.Timestamp.now(), "end": pd.Timestamp.now(), "value": 0,
            "message": "알림 연결 시험입니다.", "detail": "",
            "key": f"TEST|{datetime.now():%Y%m%d%H%M%S}", "detected_at": pd.Timestamp.now()}])
        text = alert_mod.format_text(test, cfg, {"start": "-", "end": "-", "n_rows": 0, "n_missing_ts": 0})
        ok_slack = alert_mod.send_slack(text) if ch.get("slack") else None
        ok_mail = alert_mod.send_email("테스트", text, cfg) if ch.get("email") else None
        st.code(text)
        st.write({"slack": ok_slack, "email": ok_mail})
