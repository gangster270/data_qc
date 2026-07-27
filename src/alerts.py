"""알림 발송 · 중복 억제 · 리포트 생성.

채널: console / file(JSONL) / Slack(Incoming Webhook) / Email(SMTP)
비밀값은 코드·설정파일이 아니라 환경변수에서 읽는다.
    SLACK_WEBHOOK_URL, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD

중복 억제(cooldown): 같은 key 의 알림은 지정 시간 안에 다시 보내지 않는다.
현장 운영에서 같은 결측 구간이 매 시간 재발송되면 알림을 무시하게 되므로 필수.
"""

from __future__ import annotations

import json
import os
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

import pandas as pd

from .config import resolve_path
from .qc_rules import LEVEL_ORDER

LEVEL_EMOJI = {"CRITICAL": "🔴", "WARN": "🟠", "INFO": "🔵"}


# ---------------------------------------------------------------------
# 상태 파일(중복 억제)
# ---------------------------------------------------------------------
def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def filter_new(alerts: pd.DataFrame, cfg: dict, now=None) -> tuple[pd.DataFrame, dict]:
    """등급 필터 + cooldown 을 적용해 '이번에 실제 보낼 알림'만 남긴다."""
    acfg = cfg["alerts"]
    min_level = LEVEL_ORDER.get(acfg.get("min_level", "WARN"), 1)
    state_path = resolve_path(cfg, acfg.get("state_file", "outputs/alert_state.json"))
    state = load_state(state_path)
    now = pd.Timestamp(now) if now is not None else pd.Timestamp(datetime.now())
    cooldown = timedelta(hours=float(acfg.get("cooldown_hours", 12)))

    if alerts.empty:
        return alerts, state

    keep = []
    for _, row in alerts.iterrows():
        if LEVEL_ORDER.get(row["level"], 0) < min_level:
            continue
        last = state.get(row["key"], {}).get("last_sent")
        if last:
            try:
                if now - pd.Timestamp(last) < cooldown:
                    continue                       # 아직 쿨다운 중 → 재발송 생략
            except Exception:
                pass
        keep.append(row)
    new = pd.DataFrame(keep) if keep else alerts.iloc[0:0]
    return new, state


def mark_sent(state: dict, alerts: pd.DataFrame, cfg: dict, now=None) -> None:
    now = pd.Timestamp(now) if now is not None else pd.Timestamp(datetime.now())
    for _, row in alerts.iterrows():
        state[row["key"]] = {
            "last_sent": now.isoformat(timespec="seconds"),
            "level": row["level"],
            "rule": row["rule"],
            "message": row["message"],
        }
    save_state(resolve_path(cfg, cfg["alerts"].get("state_file", "outputs/alert_state.json")), state)


# ---------------------------------------------------------------------
# 리포트 포맷
# ---------------------------------------------------------------------
def format_text(alerts: pd.DataFrame, cfg: dict, context: dict | None = None) -> str:
    """알림을 사람이 읽는 텍스트로 정리(콘솔·Slack·이메일 공통)."""
    site = cfg["site"].get("name", "site")
    ctx = context or {}
    head = [f"[{site}] 환경데이터 QC 알림  ({datetime.now():%Y-%m-%d %H:%M})"]
    if ctx:
        head.append(
            "기간 {} ~ {} | 수신 {:,}행 | 결측 timestamp {:,}건".format(
                ctx.get("start", "-"), ctx.get("end", "-"),
                ctx.get("n_rows", 0), ctx.get("n_missing_ts", 0))
        )
    if alerts.empty:
        head.append("\n신규 알림 없음 — 모든 점검 항목 정상.")
        return "\n".join(head)

    n_crit = int((alerts["level"] == "CRITICAL").sum())
    n_warn = int((alerts["level"] == "WARN").sum())
    head.append(f"신규 알림 {len(alerts)}건 (CRITICAL {n_crit} / WARN {n_warn})\n")

    lines = []
    for level in ("CRITICAL", "WARN", "INFO"):
        sub = alerts[alerts["level"] == level]
        if sub.empty:
            continue
        lines.append(f"{LEVEL_EMOJI[level]} {level} ({len(sub)}건)")
        for _, r in sub.iterrows():
            lines.append(f"  - [{r['rule']}] {r['message']}")
        lines.append("")
    return "\n".join(head + lines).strip()


def format_markdown(alerts: pd.DataFrame, cfg: dict, context: dict | None = None,
                    health: pd.DataFrame | None = None) -> str:
    """Cowork 루틴·이메일 본문용 마크다운 리포트."""
    site = cfg["site"].get("name", "site")
    ctx = context or {}
    md = [f"# {site} 환경데이터 QC 리포트", "",
          f"- 실행시각: {datetime.now():%Y-%m-%d %H:%M}",
          f"- 데이터 기간: {ctx.get('start', '-')} ~ {ctx.get('end', '-')}",
          f"- 수신 레코드: {ctx.get('n_rows', 0):,}행 / 결측 timestamp {ctx.get('n_missing_ts', 0):,}건",
          ""]
    if health is not None and not health.empty:
        md += ["## 센서 상태 요약", "",
               "| 변수 | 상태 | 수신율 | 범위이탈 | 최근값 | 마지막 관측 |",
               "|---|---|---|---|---|---|"]
        for _, r in health.iterrows():
            md.append(f"| {r['변수']} | {r['상태']} | {r['수신율']:.1%} | {r['범위이탈']} | "
                      f"{r['최근값']} | {r['마지막관측']} |")
        md.append("")

    md += ["## 신규 알림", ""]
    if alerts.empty:
        md.append("신규 알림 없음 — 모든 점검 항목 정상.")
    else:
        md += ["| 등급 | 규칙 | 변수 | 내용 |", "|---|---|---|---|"]
        for _, r in alerts.iterrows():
            msg = str(r["message"]).replace("|", "/")
            md.append(f"| {LEVEL_EMOJI.get(r['level'], '')} {r['level']} | {r['rule']} | {r['label']} | {msg} |")
    md += ["", "---", "_data_qc 자동 모니터링_"]
    return "\n".join(md)


# ---------------------------------------------------------------------
# 채널별 발송
# ---------------------------------------------------------------------
def send_console(text: str) -> bool:
    print(text)
    return True


def send_file(alerts: pd.DataFrame, cfg: dict) -> bool:
    """JSONL 로 알림 이력을 남긴다(추후 추세 분석·감사용)."""
    report_dir = resolve_path(cfg, cfg["alerts"].get("report_dir", "outputs/reports"))
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"alerts_{datetime.now():%Y-%m-%d}.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        for _, r in alerts.iterrows():
            rec = {k: (str(v) if isinstance(v, (pd.Timestamp,)) else v) for k, v in r.items()}
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    return True


def send_slack(text: str) -> bool:
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        print("[알림] SLACK_WEBHOOK_URL 미설정 — Slack 발송 생략")
        return False
    try:
        import requests
        resp = requests.post(url, json={"text": text}, timeout=15)
        return resp.status_code < 300
    except Exception as e:
        print(f"[알림] Slack 발송 실패: {e}")
        return False


def send_email(subject: str, body: str, cfg: dict) -> bool:
    ecfg = cfg["alerts"].get("email", {})
    recipients = ecfg.get("recipients") or []
    host = os.environ.get("SMTP_HOST", "")
    if not host or not recipients:
        print("[알림] SMTP_HOST 또는 수신자 미설정 — 이메일 발송 생략")
        return False
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"{ecfg.get('subject_prefix', '[환경데이터 QC]')} {subject}"
    msg["From"] = ecfg.get("sender", user or "noreply@example.org")
    msg["To"] = ", ".join(recipients)
    try:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            if user:
                s.login(user, password)
            s.send_message(msg)
        return True
    except Exception as e:
        print(f"[알림] 이메일 발송 실패: {e}")
        return False


def dispatch(alerts: pd.DataFrame, cfg: dict, context: dict | None = None,
             health: pd.DataFrame | None = None, dry_run: bool = False) -> dict:
    """등급/쿨다운 필터 후 활성 채널로 발송하고 결과를 돌려준다."""
    new, state = filter_new(alerts, cfg)
    text = format_text(new, cfg, context)
    md = format_markdown(new, cfg, context, health)
    channels = cfg["alerts"].get("channels", {})
    results = {"n_total": int(len(alerts)), "n_sent": int(len(new)), "channels": {}, "text": text, "markdown": md}

    # 마크다운 리포트는 알림 유무와 관계없이 항상 남긴다(일일 점검 기록).
    report_dir = resolve_path(cfg, cfg["alerts"].get("report_dir", "outputs/reports"))
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"qc_report_{datetime.now():%Y-%m-%d}.md"
    if not dry_run:
        report_path.write_text(md, encoding="utf-8")
    results["report_path"] = str(report_path)

    if new.empty or dry_run:
        if channels.get("console", True):
            send_console(text)
        return results

    if channels.get("console", True):
        results["channels"]["console"] = send_console(text)
    if channels.get("file", True):
        results["channels"]["file"] = send_file(new, cfg)
    if channels.get("slack", False):
        results["channels"]["slack"] = send_slack(text)
    if channels.get("email", False):
        n_crit = int((new["level"] == "CRITICAL").sum())
        subject = f"{cfg['site'].get('name', 'site')} 알림 {len(new)}건 (CRITICAL {n_crit})"
        results["channels"]["email"] = send_email(subject, text, cfg)

    mark_sent(state, new, cfg)
    return results
