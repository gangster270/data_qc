# data_qc — 환경데이터 전처리 · QC 자동 모니터링 · 센서 검증

10분 단위 환경 로거 자료(온도·습도·배지온도·배지습도·PPFD·일사량·EC)를
**생육조사 간격(7일·10일)에 맞춰 시차 매칭**하고, **결측·센서오류를 자동 감시**하며,
**센서 정기 검증 이력을 관리**하는 파이프라인.

```
로거 파일(.xlsx/.csv)
   │
   ├─▶ [전처리]  10분 → 일별 → 생육 구간(시차 매칭) → merged_env_growth.csv
   │              scripts/run_preprocess.py · src/preprocess.py · R/env_growth_match.R
   │
   ├─▶ [모니터링] 13종 QC 규칙 → 알림(콘솔/파일/Slack/메일) + 일일 리포트
   │              scripts/run_monitor.py · src/qc_rules.py · src/alerts.py
   │
   ├─▶ [대시보드] 4개 탭(모니터링·전처리·센서검증·설정)
   │              app/streamlit_app.py
   │
   └─▶ [센서 검증] 기한 관리 · 상호비교 · 드리프트 추적
                  src/sensor_check.py · docs/sensor_verification_routine.md
```

---

## 빠른 시작

```bash
pip install -r requirements.txt

# 0) 동작 확인용 합성 자료 생성 + 테스트
python tests/make_sample_data.py
python tests/test_pipeline.py           # 13/13 통과

# 1) 전처리 — 환경 10분 → 생육 구간 매칭
python scripts/run_preprocess.py --env "data/*.xlsx" --growth data/growth.csv \
       --first-start 2026-04-01 --out outputs/

# 2) 모니터링 — 최근 7일 결측·센서오류 점검 + 알림
python scripts/run_monitor.py --env "data/*.xlsx" --lookback 7

# 3) 대시보드
streamlit run app/streamlit_app.py
```

설정은 전부 `config/qc_config.yaml` 한 곳에 있다(임계값·알림채널·검증주기).

---

## 1. 전처리 — 시차 매칭

수작업으로 하던 "10분 자료를 조사간격에 맞춰 평균" 을 2단계 집계로 코드화했다.

- **Step 1 (10분 → 일별)**: 온도=평균/최저/최고/일교차, PPFD=**일적산 DLI**,
  VPD=10분에서 계산 후 평균, GDD=max(일평균T−기준온도,0), 관측 완전성(n/144) 산출
- **Step 2 (일별 → 구간)**: 각 조사일에 **직전 조사일 다음날~당일** 구간을 매칭.
  평균형은 구간 평균, 적산형(DLI·GDD·일사량)은 **합계와 일평균 둘 다** + 시험 시작부터 누적
- **시차(lag)**: `--lag-days 3` → 구간 전체를 3일 앞당겨 매칭(지연 반응 검토)
- **고정창**: `--window-days 10` → 조사일 직전 10일 고정
- 조사간격 7·10일은 **자동 추정**, 불규칙 간격도 그대로 처리
- 범위 이탈값(-99.9 등)은 집계 전에 결측 처리하고 처리 건수를 리포트에 남김
- 구간별 `quality_flag` 로 일수부족·레코드결측을 표시 → **플래그 붙은 구간은 분석에서 제외**

산출물: `daily_env_summary.csv`, `env_interval_summary.csv`,
**`merged_env_growth.csv`**(분석 투입용), `preprocess_report.xlsx`

상세 사양: [`docs/preprocessing_spec.md`](docs/preprocessing_spec.md)
R 버전(통계·그래프를 R 에서 이어갈 때): [`R/env_growth_match.R`](R/env_growth_match.R)

---

## 2. 자동 모니터링 — QC 규칙 13종

| 규칙 | 감지 대상 |
|---|---|
| R01 `timestamp_gap` | 기록 누락(연속 결측 구간) |
| R02 `missing_ratio` | 변수별 일 결측률 초과 |
| R03 `out_of_range` | 물리적으로 불가능한 값 |
| R04 `flatline` | 동일값 연속(센서 고착·통신 정지) — 야간 PPFD 0 은 제외 |
| R05 `spike` | 10분 간 비현실적 급변 |
| R06 `daytime_dark` | 주간에 광센서가 어두움(탈락·차폐·오염) |
| R07 `night_light` | 야간 광 검출 ※ **야간보광(NI/SL) 시험 중이면 비활성 유지** |
| R08 `rh_saturated` | 습도 99% 이상 장시간(결로·필터 오염) |
| R09 `logger_offline` | 최신 관측 지연(전원·통신) |
| R10 `pair_divergence` | 중복 센서 간 편차 초과(드리프트) |
| R11 `transmittance_drop` | 내부PPFD/외부일사 비율 급락(오염·차광막) |
| R12 `error_value` | `#VALUE!`·`ERROR`·`inf` 등 진성 오류값 |
| R13 `heat_event` | 작물 위험 수준 고온(기온 45℃·배지온도 40℃ 초과) — 센서 오류가 아닌 실제 사건 |

- 등급: INFO / WARN / CRITICAL. 종료코드 0·1·2 로 스케줄러에서 분기 가능.
- **중복 억제**: 같은 이상은 `cooldown_hours`(기본 12h) 안에 재발송하지 않는다.
- 알림 채널: 콘솔 · 파일(JSONL) · Slack(`SLACK_WEBHOOK_URL`) · 메일(`SMTP_*`).
  비밀값은 설정 파일이 아니라 **환경변수**로 넣는다.
- 매 실행마다 `outputs/reports/qc_report_YYYY-MM-DD.md` 를 남긴다(알림이 없어도 기록).

실측 로거 5대로 검증하며 반영한 규칙(상세: [`docs/logger_inventory.md`](docs/logger_inventory.md)):
배지수분은 **% 단위**(METER TEROS 출력), 센서 범위는 **작물 기준이 아니라 센서 사양** 기준
(실제 70℃ 고온사고가 오류로 지워지는 것 방지), EC 0 지속은 배지 건조 시 정상,
미연결 포트(전 구간 0)는 고착과 구분해 경보.

**NaN 은 오류가 아니다.** 중간에 설치된 센서의 앞부분 결측을 오류로 몰아 열을 통째로 버리지
않도록, 열 제거·오류 판정은 진성 오류토큰에만 반응한다.

---

## 3. Cowork 자동화

규칙 엔진이 "무엇이 이상한가"를, Cowork 루틴이 "그래서 오늘 무엇을 해야 하는가"를 담당한다.

- 일일 QC 점검(매일 08:00 KST) / 주간 센서 점검 알림 / 조사일 전처리 재실행
- 등록 방법(Cowork Routine · cron · GitHub Actions)과 프롬프트 전문:
  [`cowork/COWORK_ROUTINE.md`](cowork/COWORK_ROUTINE.md), [`cowork/daily_qc_prompt.md`](cowork/daily_qc_prompt.md)
- 원칙: **정상이면 알리지 않는다.** 매일 오는 알림은 곧 무시된다.

---

## 4. 대시보드 (Streamlit)

```bash
streamlit run app/streamlit_app.py
```

| 탭 | 기능 |
|---|---|
| 📊 모니터링 | 센서 상태 카드, 알림 목록, 일자별 결측률 히트맵, 시계열, 즉시 발송 |
| 🔁 전처리 | 시차·창 설정 → 일별/구간/병합 결과 미리보기 + CSV·Excel 다운로드, 환경↔생육 상관 |
| 🔬 센서 검증 | 검증 기한 현황, 상호비교(bias·MAE·r·판정), 검증 기록 입력, 드리프트 추이 |
| ⚙️ 설정 | 임계값 확인, 알림 채널 상태, 테스트 발송 |

---

## 5. 센서 정기 검증 루틴

| 주기 | 항목 |
|---|---|
| 매일(자동) | QC 리포트 확인, CRITICAL 당일 조치 |
| 주 1회 | 육안 점검·청소(확산캡·방사차폐·TEROS 삽입상태·배터리) |
| 월 1회 | 센서 상호비교(24시간 병렬 설치) → bias·MAE·r 로 합격 판정 |
| 분기 1회 | 기준기 대조(온습도계·광량자계·EC 표준액·중량법), 로거 시각 동기화 |
| 2년 1회 | 제조사 재교정(SQ-521·PYR) |

허용오차: 온도 ±0.5℃ · 습도 ±3% · 배지온도 ±0.5℃ · 배지수분 ±3%(m³/m³ 환산 0.03) · EC ±0.3 · PPFD/일사 ±5%

절차·체크리스트·기록양식: [`docs/sensor_verification_routine.md`](docs/sensor_verification_routine.md)
기록 스키마: [`templates/sensor_calibration_log.csv`](templates/sensor_calibration_log.csv)

> **원자료는 덮어쓰지 않는다.** 청소·재삽입·교체·시각 재설정은 모두 개입 이벤트이며
> 기록 없이 수행하지 않는다(값의 계단 변화 원인 추적).

---

## 6. 구조

```
config/qc_config.yaml          임계값·알림·검증주기 (단일 설정 지점)
src/io_logger.py               ZL6 형식 읽기, 열 표준화, 10분 격자 정합
src/preprocess.py              일별·구간 집계, 시차 매칭, 범위 이탈 처리
src/qc_rules.py                QC 규칙 13종 + 센서 상태표
src/alerts.py                  중복 억제, 리포트, 채널 발송
src/sensor_check.py            검증 로그·기한·상호비교·드리프트
scripts/run_preprocess.py      전처리 CLI
scripts/run_monitor.py         모니터링 CLI(스케줄 실행용)
app/streamlit_app.py           대시보드
R/env_growth_match.R           R 버전 시차 매칭
tests/                         합성 자료 생성기 + 검증 테스트 13종
docs/logger_inventory.md       실측 로거 5대 센서 구성·상태 대장
docs/, cowork/, templates/     사양서·SOP·루틴 프롬프트·양식
```

관련 스킬: `agri-logger-qc`(원자료 QC) · `agri-env-growth-match`(매칭 표준) ·
`agri-ppfd-gap-fill`(결측 보정) · `agri-stats-workflow`(통계) · `agri-ggplot-style`(그래프)
