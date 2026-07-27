# Cowork 자동화 구성 (정기 실행 + 알림)

`scripts/run_monitor.py` 는 혼자서도 돌아가지만, Cowork(Claude) 루틴으로 감싸면
**"이상값을 사람이 읽는 문장으로 판단해 주는" 단계**가 붙는다.

```
[Cowork Routine 정시 실행]
        │
        ├─ scripts/run_monitor.py  →  규칙 기반 알림 + qc_report_YYYY-MM-DD.md
        │
        └─ Claude 가 리포트를 읽고
              · 새로 생긴 이상인지, 어제와 같은 이상인지 판단
              · 원인 후보와 현장 조치 우선순위 정리
              · 조치 필요 시에만 사람에게 알림(메일/Slack/푸시)
```

규칙 엔진은 "무엇이 이상한가"를, Cowork 루틴은 "그래서 오늘 무엇을 해야 하는가"를 담당한다.

---

## 1. 루틴 3종

| 루틴 | 주기 (KST) | cron (UTC) | 목적 |
|---|---|---|---|
| 일일 QC 점검 | 매일 08:00 | `0 23 * * *` | 결측·센서오류 확인, CRITICAL 즉시 통보 |
| 주간 센서 점검 알림 | 월 08:30 | `30 23 * * 0` | 육안점검·상호비교 기한 도래 알림 |
| 조사일 전처리 | 조사 다음날 09:00 | `0 0 * * *` (조건부) | 생육 조사자료 입력 시 구간 매칭 재실행 |

> Cowork/Claude Routine 의 cron 은 **UTC** 로 해석된다. KST(UTC+9) 08:00 은 전날 23:00 UTC 이며,
> 요일 지정이 있으면 요일도 하루 앞당겨야 한다(월요일 08:30 KST → 일요일 23:30 UTC).

---

## 2. 등록 방법

### 방법 A — Cowork 대화창에서 등록 (권장)

Claude 에게 그대로 요청한다.

```
매일 아침 8시(KST)에 data_qc 저장소에서 환경데이터 QC 루틴을 실행하는 Routine 을 만들어줘.
프롬프트는 cowork/daily_qc_prompt.md 내용을 사용해줘.
```

Claude 가 `create_trigger` 로 등록한다. 등록 후 `list_triggers` 로 확인하고,
`fire_trigger` 로 즉시 1회 시험 실행해 결과를 확인한다.

### 방법 B — 로컬 cron (Cowork 없이 규칙 엔진만)

```bash
# 매일 07:50 아카이브 갱신 → 08:00 점검 (종료코드 0=정상, 1=WARN, 2=CRITICAL)
50 7 * * * cd /path/to/data_qc && /usr/bin/python3 scripts/build_archive.py \
    --env "data/**/*.xlsx" "data/**/*.csv" --out outputs/archive >> outputs/archive.log 2>&1
 0 8 * * * cd /path/to/data_qc && /usr/bin/python3 scripts/run_monitor.py \
    --archive outputs/archive --by-logger --lookback 7 >> outputs/monitor.log 2>&1
```

### 방법 C — GitHub Actions (자료가 저장소/클라우드에 있을 때)

`.github/workflows/qc-monitor.yml` 예시:

```yaml
name: 환경데이터 QC 모니터링
on:
  schedule:
    - cron: "0 23 * * *"      # KST 08:00
  workflow_dispatch:
jobs:
  monitor:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.11"}
      - run: pip install -r requirements.txt
      - run: python scripts/run_monitor.py --env "data/*.xlsx" --lookback 7 --json
        env:
          SLACK_WEBHOOK_URL: ${{ secrets.SLACK_WEBHOOK_URL }}
      - uses: actions/upload-artifact@v4
        if: always()
        with: {name: qc-report, path: outputs/reports/}
```

---

## 3. 일일 QC 루틴 프롬프트

전문은 `cowork/daily_qc_prompt.md`. 요지는 다음과 같다.

1. 아카이브 갱신 후 점검:
   `python scripts/build_archive.py --env "<자료경로>/**/*.xlsx" --out outputs/archive`
   `python scripts/run_monitor.py --archive outputs/archive --by-logger --lookback 7 --json`
2. 종료코드·JSON 요약·`outputs/reports/qc_report_*.md` 를 읽는다
3. **어제 리포트와 비교**해 신규/지속/해소 이상을 구분한다
4. CRITICAL 이 있으면 원인 후보와 조치 순서를 3줄 이내로 정리해 사람에게 알린다
5. 모두 정상이면 **알리지 않는다**(무알림이 정상 신호 — 알림 피로 방지)

---

## 4. 알림 채널 연결

| 채널 | 설정 |
|---|---|
| Slack | 환경변수 `SLACK_WEBHOOK_URL` + `qc_config.yaml` → `alerts.channels.slack: true` |
| 이메일 | `SMTP_HOST/PORT/USER/PASSWORD` + `alerts.email.recipients` 목록 + `channels.email: true` |
| 파일 | 기본 활성 — `outputs/reports/alerts_YYYY-MM-DD.jsonl` |
| 콘솔 | 기본 활성 — cron 로그로 수집 |

중복 발송 억제: 같은 이상은 `alerts.cooldown_hours`(기본 12시간) 안에 다시 보내지 않는다.
`outputs/alert_state.json` 이 발송 이력을 들고 있으므로, 강제로 다시 받고 싶으면 이 파일을 지운다.

---

## 5. 운영 시 주의

- **야간보광(NI/SL) 시험 중이면** `qc.night_light_enabled: false` 를 유지한다(정상 보광을 오탐).
- 로거를 수동으로 내려받는 운영이라면, 루틴 실행 전에 최신 파일이 폴더에 있어야 한다.
  자동 동기화가 없으면 `R09_logger_offline` 이 매일 뜨므로 `offline_warn_minutes` 를
  내려받기 주기에 맞춰 늘린다(예: 주 1회 내려받기 → 10080분).
- 센서 청소·재삽입 직후에는 계단 변화로 `R05_spike`·`R04_flatline` 이 뜰 수 있다.
  검증 로그에 개입 기록을 남겨 두면 원인 판단이 즉시 된다.
- 새 시험구를 추가하면 `config/qc_config.yaml` 의 센서 범위를 먼저 확인한다
  (예: 배지 종류가 바뀌면 VWC 상한이 달라진다).
