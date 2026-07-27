# data_qc — 환경데이터 전처리 · QC 자동 모니터링 · 센서 검증

환경 로거 자료(온도·습도·배지온도·배지습도·PPFD·일사량·EC 등)를
**생육조사 간격(7일·10일)에 맞춰 시차 매칭**하고, **결측·센서오류를 자동 감시**하며,
**센서 정기 검증 이력을 관리**하는 파이프라인.

**같은 로거는 알아서 한 구역으로 묶인다.** 파일명이 매번 달라져도(`260703_22094002.csv`,
`260710_22094002.csv`) 파일 안의 **로거 일련번호**를 읽어 같은 자료로 잇는다. 번호에 구역
이름을 한 번 지정해 두면(`config/logger_registry.yaml`) 이후 업로드부터 자동 적용되고,
한 구역에 로거가 여러 대면 **같은 시각의 값이 한 행으로 합쳐진다**(같은 변수는 `__rep`로 분리 보존).
센서 구성이 바뀌어 엑셀 시트가 `Config 1`, `Config 2` 로 나뉘어도 전부 읽어 포트 기준으로 잇는다.

**로거 기종을 가리지 않는다.** METER ZL6 뿐 아니라 국산 로거·자체 기록 파일도
**시간(또는 날짜+시간) 열만 있으면** 그대로 처리된다 — 기록 간격(1·5·10·15·30·60분),
구분자·인코딩(CP949 포함), 헤더 위치, 변수명 표기(`SoilTemp`/`배지 온도`/`Soil Temperature`)를
자동 인식하고, 표준 변수가 아닌 열(수온·pH·풍속 등)도 버리지 않고 집계·감시한다.

📖 **대시보드 사용법(설치·화면 설명)**: [`docs/dashboard_guide.md`](docs/dashboard_guide.md)
📥 **코드를 내 컴퓨터로 가져오기(GitHub 처음이신 분)**: [`docs/github_guide.md`](docs/github_guide.md)

```
로거 파일(.xlsx/.csv, 기종·간격 무관, 여러 대·여러 번)
   │
   ├─▶ [통합 아카이브] 로거번호 자동 인식 → **구역별로 병합** · 날짜순 정렬 ·
   │                  변수 표준화 · 중복 정리 · 증분 업데이트
   │                  scripts/build_archive.py · src/archive.py · src/registry.py
   │                  → env_master.csv (원자료) / env_master_clean.csv (QC 적용)
   │
   ├─▶ [전처리]  10분 → 일별 → 생육 구간(시차 매칭) → merged_env_growth.csv
   │              scripts/run_preprocess.py · src/preprocess.py · R/env_growth_match.R
   │
   ├─▶ [모니터링] 13종 QC 규칙 → 알림(콘솔/파일/Slack/메일) + 일일 리포트
   │              scripts/run_monitor.py · src/qc_rules.py · src/alerts.py
   │
   ├─▶ [주간 업데이트] 쌓기·점검·정리를 명령 한 줄로 + 회차별 결과 보관
   │              scripts/weekly_update.py · src/store.py
   │
   ├─▶ [대시보드] 5개 탭(상태점검·결과만들기·쌓인자료·센서점검·설정)
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
python tests/test_pipeline.py           # 32/32 통과

# 1) 통합 — 보유한 모든 환경데이터를 하나로 (새 파일 받으면 다시 실행만)
python scripts/build_archive.py --env "data/**/*.xlsx" "data/**/*.csv" --out outputs/archive

# 1-a) 로거 번호에 구역 이름 지정 (한 번만; 이후 자동 기억)
python scripts/build_archive.py --zone "22094002=3구역" --zone "z6-20917=3구역" --list-zones

# 1-0) 전처리 — 환경 10분 → 생육 구간 매칭
python scripts/run_preprocess.py --env "data/*.xlsx" --growth data/growth.csv \
       --first-start 2026-04-01 --out outputs/

# 1-1) 조사일을 직접 정해 시차 매칭 (생육 파일 없이도 가능)
python scripts/run_all_loggers.py --archive outputs/archive --by-treatment \
       --survey-start 2026-04-01 --survey-interval 10 --survey-count 6 --out outputs/all

# 2) 모니터링 — 아카이브 전체를 로거별로 점검 + 알림
python scripts/run_monitor.py --archive outputs/archive --by-logger --lookback 7

# 2-1) 매주 이것 하나면 끝 — 쌓기 + 점검 + 정리 + 회차 보관
#      조사일 기준은 처음 한 번만 주면 config/survey.yaml 에 기억된다
python scripts/weekly_update.py --env "data/신규/*" \
       --survey-start 2026-04-01 --survey-interval 10 --survey-count 12
python scripts/weekly_update.py --env "data/신규/*"      # 다음 주부터는 이것만

# 3) 대시보드
streamlit run app/streamlit_app.py
```

설정은 전부 `config/qc_config.yaml` 한 곳에 있다(임계값·알림채널·검증주기).

---

## 1. 전처리 — 시차 매칭

수작업으로 하던 "10분 자료를 조사간격에 맞춰 평균" 을 2단계 집계로 코드화했다.

- **Step 1 (원자료 → 일별)**: 기록 간격은 자료에서 자동 추정(1·5·10·15·30·60분): 온도=평균/최저/최고/일교차, PPFD=**일적산 DLI**,
  VPD=10분에서 계산 후 평균, GDD=max(일평균T−기준온도,0), 관측 완전성(n/144) 산출
- **Step 2 (일별 → 구간)**: 각 조사일에 **직전 조사일 다음날~당일** 구간을 매칭.
  평균형은 구간 평균, 적산형(DLI·GDD·일사량)은 **합계와 일평균 둘 다** + 시험 시작부터 누적
- **조사일 기준을 직접 지정**: 생육 파일이 없어도 `--survey-start/--survey-interval/--survey-count`
  또는 `--survey-dates "2026-04-01,2026-04-11,…"` 로 원하는 조사일을 넣으면 그 기준으로 매칭
- **시차(lag)**: `--lag-days 3` → 구간 전체를 3일 앞당겨 매칭(지연 반응 검토)
- **고정창**: `--window-days 10` → 조사일 직전 10일 고정
- 조사간격 7·10일은 **자동 추정**, 불규칙 간격도 그대로 처리
- 범위 이탈값(-99.9 등)은 집계 전에 결측 처리하고 처리 건수를 리포트에 남김
- 구간별 `quality_flag` 로 일수부족·레코드결측을 표시 → **플래그 붙은 구간은 분석에서 제외**
- **처리구별 집계**: 한 로거의 센서가 처리구별로 꽂혀 있으면 `config/sensor_map.yaml` 에
  포트↔처리구를 적고 `--by-treatment` → 생육 자료와 (조사일 × 처리구)로 병합

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
| R10 `pair_divergence` | 중복 센서 간 편차 초과(드리프트) ※ 반복 센서가 서로 다른 처리구인 현장은 **기본 비활성** |
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
pip install -r requirements.txt
streamlit run app/streamlit_app.py     # 터미널에 뜨는 http://localhost:8501 접속
```

화면은 **할 일 순서**로 되어 있다 — 왼쪽 사이드바 `1단계 자료 넣기` 에 파일을 올리면
아래 탭이 채워진다. 전문용어는 화면에 쓰지 않는다(규칙 코드·열 이름은 `app/ui_text.py`
에서 사람 말로 옮긴다). 화면별 상세 안내는 [`docs/dashboard_guide.md`](docs/dashboard_guide.md).

| 탭 | 기능 |
|---|---|
| 2️⃣ 상태 점검 | 한 줄 결론 → 규칙별로 묶은 알림(뜻·조치 포함), 항목별 상태, 결측 히트맵, 시계열, 즉시 발송 |
| 3️⃣ 결과 만들기 | 조사 날짜만 지정 → 구간 정의·구간별 환경·생육 병합 + CSV·Excel 다운로드, 회차 저장 |
| 📦 쌓인 자료 | 올린 파일을 보관함에 누적, 구역별 현황·업로드 이력, **지난 회차 결과 다시 받기** |
| 🔬 센서 점검 | 점검 기한, 두 센서 비교(bias·MAE·r·판정), 점검 기록, 드리프트 추이 |
| ⚙️ 설정 | 로거번호↔구역 이름 지정, 임계값 확인, 알림 채널 상태·테스트 발송 |

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
config/sensor_map.yaml         포트 ↔ 처리구 매핑 (처리구별 집계용)
config/logger_registry.yaml    로거 일련번호 ↔ 구역 이름 등록부 (자동 기억)
config/survey.yaml             조사일 기준 기억(weekly_update.py 가 자동 생성)
src/io_logger.py               ZL6 형식 읽기, 열 표준화, 10분 격자 정합
src/preprocess.py              일별·구간 집계, 시차 매칭, 범위 이탈 처리
src/qc_rules.py                QC 규칙 13종 + 센서 상태표
src/alerts.py                  중복 억제, 리포트, 채널 발송
src/sensor_check.py            검증 로그·기한·상호비교·드리프트
src/sensor_map.py              센서↔처리구 매핑, 처리구 분리
src/archive.py                 전체 환경데이터 통합·구역 병합·증분 업데이트
src/registry.py                로거번호 ↔ 구역 등록부
src/store.py                   주간 보관함(원본 누적·중복 방지) · 회차별 결과 보관
scripts/build_archive.py       전체 환경데이터 통합 아카이브 생성/갱신
scripts/run_preprocess.py      전처리 CLI(조사일 직접 지정 가능)
scripts/run_all_loggers.py     로거 일괄 전처리 + 통합 산출물
scripts/run_monitor.py         모니터링 CLI(스케줄 실행용)
scripts/weekly_update.py       주간 업데이트(쌓기·점검·정리) 한 줄 실행
app/streamlit_app.py           대시보드
R/env_growth_match.R           R 버전 시차 매칭
tests/                         합성 자료 생성기 + 검증 테스트 32종
docs/dashboard_guide.md        대시보드 사용법(설치·화면별 안내)
docs/github_guide.md           코드 내려받기·업데이트 안내(GitHub 입문)
docs/logger_inventory.md       실측 로거 5대 센서 구성·상태 대장
docs/, cowork/, templates/     사양서·SOP·루틴 프롬프트·양식
```

관련 스킬: `agri-logger-qc`(원자료 QC) · `agri-env-growth-match`(매칭 표준) ·
`agri-ppfd-gap-fill`(결측 보정) · `agri-stats-workflow`(통계) · `agri-ggplot-style`(그래프)
