# 전처리 사양서 — 10분 환경데이터 → 생육조사 구간 매칭

수작업으로 하던 "10분 자료를 생육조사 간격(7·10일)에 맞춰 평균내는" 작업의 코드화 사양.
구현: `src/preprocess.py`, 실행: `scripts/run_preprocess.py`, UI: 대시보드 🔁 탭.

---

## 1. 왜 2단계로 집계하는가

10분 자료를 곧바로 구간 평균하면 두 가지가 무너진다.

1. **결측 편향**: 특정 날짜만 관측이 절반이면 그 날이 구간 평균에 절반 가중치로만 들어간다.
   일별로 한 번 접은 뒤 다시 평균해야 각 날짜가 동등한 가중치를 갖는다.
2. **적산량 왜곡**: DLI·적산온도는 "하루 단위 적산"이 물리적 의미다.
   10분 값을 그대로 평균하면 광량은 mol 단위가 아니라 순간값 평균이 되어 해석이 불가능해진다.

따라서 **10분 → 일별 → 구간**의 2단계를 표준으로 한다.
중간 산출물 `daily_env_summary.csv` 는 반드시 보관한다(결측 추적·재집계의 근거).

---

## 2. Step 1 — 원자료 → 일별

기록 간격은 **자료에서 자동 추정**한다(1·5·10·15·30·60분 …). 하루 기대 레코드 수,
DLI 적분(값 × 간격초), 완전성, 고착 판정 시간이 모두 그 간격을 따라 자동으로 맞춰진다.
`config/qc_config.yaml` 의 `site.interval_minutes` 에 숫자를 넣으면 고정할 수 있다.


| 환경변수 | 일별 집계 | 근거 |
|---|---|---|
| 온도 | mean / min / max / 일교차(max−min) | 평균기온과 일교차가 서로 다른 생리 반응에 관여 |
| 상대습도 | mean / min / max | 구간 노출 평균 |
| VPD | **10분에서 계산 후** mean, 주간(9~15시) 별도 | 일평균 T·RH 로 뒤늦게 계산하면 과소평가(Jensen 부등식) |
| PPFD | **일적산 → DLI** = Σ(PPFD×600)/10⁶ | mol·m⁻²·d⁻¹ 가 광 반응의 표준 단위 |
| 광주기 | PPFD > 10 µmol 인 시간 수 | 야간보광(NI/SL) 효과 확인용 |
| 일사량 | 일적산 MJ·m⁻²·d⁻¹ = Σ(W/m²×600)/10⁶ | PYR 전용 로거의 DLI 추정 근거 |
| 배지온도·배지습도 | mean / min / max | 근권 환경 |
| EC / CO₂ | mean | 평균 농도 |
| 적산온도(GDD) | max(일평균T − 기준온도, 0) | 기준온도는 `preprocess.gdd_base` (기본 10℃) |
| 완전성 | `n_records / 144`, `is_complete` | 부분일 판별(10분 → 하루 144 레코드) |
| 변수별 결측률 | 1 − 유효값수/144 | 어느 센서가 얼마나 비었는지 |

표준 변수가 아닌 열(수온·pH·풍속·관수량 등)도 **평균/최저/최고**로 함께 요약된다.
로거마다 다른 변수를 버리지 않기 위한 설계이며, 별도 설정이 필요 없다.

집계 **전에** 다음을 수행한다.
- 중복 timestamp 제거(첫 값 유지) — 재내려받기로 겹치는 것은 정상.
- 10분 격자 재색인 — 빠진 시각을 빈 행으로 삽입하고 `qc_status` 로 표시.
- **범위 이탈값 결측 처리** — `-99.9` 같은 오류코드 하나가 일최저·일평균을 통째로 망친다.
  기본 활성(`--keep-out-of-range` 로 해제 가능), 처리 건수는 리포트에 남는다.

---

## 3. Step 2 — 일별 → 생육 구간 (시차 매칭)

### 3.1 구간 정의

생육은 **구간 누적 반응**이다. 조사일 하루의 환경이 아니라 그 사이에 쌓인 환경이 원인이다.

```
기본(가변구간)  : [직전 조사일 + 1일,  당일]
고정창(window_days=N) : [당일 − N + 1,  당일]
시차(lag_days=L) : 위 구간 전체를 L일 앞당김  → [start − L, end − L]
```

- 첫 구간은 직전 조사일이 없으므로 **정식일 등 시작일**(`--first-start`)을 지정한다.
  지정하지 않으면 추정 조사간격만큼 소급한다.
- 조사간격(7일/10일)은 **자동 추정**한다(조사일 간격의 최빈값). 7↔10일 혼재도 그대로 처리된다.
- 시차(lag)는 "환경 효과가 며칠 뒤 생육에 나타나는가"를 검토할 때 쓴다.
  예) `--lag-days 3` → 조사일 3일 전까지의 환경과 매칭. 여러 lag 로 돌려 상관이 가장 높은
  구간을 찾는 방식으로 지연 반응을 탐색할 수 있다(사후 선택이므로 결론은 신중히).

### 3.2 구간 집계 규칙

| 성격 | 변수 | 집계 |
|---|---|---|
| 평균형 | 온도·습도·VPD·배지온도·배지습도·EC·CO₂ | 일별 값의 **구간 평균** |
| 극값형 | 일최저·일최고 | 구간 중 **최저의 최저 / 최고의 최고**, 그리고 **일최저·일최고의 평균** |
| 적산형 | DLI·GDD·일사량 | 구간 **합계**(누적) + 일평균 둘 다 산출 |
| 누적형 | cum_dli / cum_gdd / cum_solar_MJ | 시험 시작부터의 **누적** |

극값을 두 가지(구간 최대 / 일최대의 평균)로 모두 내는 이유: 전자는 스트레스 사건(단 하루의
고온)을, 후자는 통상적인 낮 최고 수준을 나타내며 해석이 다르다.

### 3.3 품질 플래그

각 구간에 `quality_flag` 가 붙는다.
- `일수부족(7/10일)` — 환경자료가 구간 전체를 덮지 못함(로거 설치 전 기간, 장기 결측 등)
- `레코드결측(평균완전성 0.83)` — 구간 내 날짜들의 관측 완전성 평균이 90% 미만
- `환경자료 없음` — 해당 구간에 일별 자료가 전혀 없음

**플래그가 붙은 구간을 그대로 분석에 넣지 말 것.** 제외하거나, 논문·보고서에 결측 사실을 명시한다.

---

### 3.4 처리구별 집계 (한 로거의 센서가 서로 다른 처리구일 때)

한 로거의 TEROS/SQ-521 이 처리구별로 하나씩 꽂혀 있으면, 대표 센서 하나만 쓰면
다른 처리구의 배지환경이 잘못 붙는다. `config/sensor_map.yaml` 에 포트↔처리구를 적고
`--by-treatment` 로 실행하면 다음이 처리구 단위로 수행된다.

```
10분 자료 → 처리구별 분리 → 처리구별 일별 요약 → 처리구별 구간 집계
                                              → 생육(조사일 × 처리구) 병합
```

- 매핑 키(`TRT1` 자리)는 **생육자료의 처리구 값과 정확히 같아야** 병합된다.
  다르면 실행 중 "매핑에 없는 생육 처리구" 경고가 뜬다.
- `shared` 에 적은 변수(기온·PPFD 등 구역에 하나뿐인 센서)는 모든 처리구에 동일 적용된다.
- 열 번호 `var__rep1..N` 은 **파일의 포트 순서**를 그대로 따른다. 죽은 포트가 있어도
  번호가 밀리지 않으므로 매핑이 어긋나지 않는다(대표 열은 살아 있는 센서로 따로 고른다).

```bash
python scripts/run_preprocess.py --env "data/z6-20917_*.xlsx" --growth data/growth.csv \
       --by-treatment --growth-trt-col trt --first-start 2026-05-28 --out outputs/
```

산출되는 `env_interval_summary.csv`·`merged_env_growth.csv` 에는 `trt` 열이 추가된다.

---

## 4. 산출물

| 파일 | 내용 |
|---|---|
| `daily_env_summary.csv` | 일별 요약(완전성·결측률 포함) |
| `env_interval_summary.csv` | 구간별 환경 요약(시차 반영, 품질 플래그) |
| `merged_env_growth.csv` | 생육 각 측정행 + 구간 환경 → **분석 투입용 최종 파일** |
| `preprocess_report.xlsx` | 위 3종 + 구간정의 + 열매핑 + 범위이탈 + 결측 timestamp + 실행요약 |

---

## 5. 실행 예

```bash
# 일별 요약만
python scripts/run_preprocess.py --env "data/*.xlsx" --out outputs/

# 생육 구간 매칭(조사간격 자동 추정)
python scripts/run_preprocess.py --env "data/*.xlsx" --growth data/growth.csv \
    --first-start 2026-04-01 --out outputs/

# 시차 3일 + 고정 10일창 + 불완전일 제외
python scripts/run_preprocess.py --env "data/*.xlsx" --growth data/growth.csv \
    --lag-days 3 --window-days 10 --drop-incomplete-days --out outputs/lag3/

# 처리구별 집계(한 로거의 센서가 처리구별로 꽂혀 있을 때)
python scripts/run_preprocess.py --env "data/z6-20917_*.xlsx" --growth data/growth.csv \
    --by-treatment --growth-trt-col trt --out outputs/

# (참고) 중복 센서가 '같은 위치의 공간반복'인 경우에만 평균
python scripts/run_preprocess.py --env "data/*.xlsx" --replicate mean --out outputs/
```

```bash
# 로거 여러 대 일괄 처리 → 로거별 + 전체 통합 산출물
python scripts/run_all_loggers.py --env "data/*.xlsx" --by-treatment --out outputs/all
python scripts/run_all_loggers.py --env "data/*.xlsx" --growth data/growth.csv \
       --by-treatment --first-start 2026-04-01 --out outputs/all
```

통합 산출물에는 `logger`·`trt` 열이 붙어, 로거·처리구를 한 파일에서 비교할 수 있다.

생육 파일 형식은 `templates/growth_template.csv` 참조 (조사일 열 이름은 `--growth-date-col` 로 지정).

---

## 6. 자주 하는 실수

| 실수 | 올바른 처리 |
|---|---|
| 조사일 하루 환경만 매칭 | 직전 구간 전체를 집계해 매칭 |
| PPFD 를 평균으로 처리 | 일적산(DLI)으로 변환 후 구간 합계 |
| 적산변수(DLI·GDD)를 구간 평균만 산출 | 합계(누적)를 함께 산출 |
| 부분일(결측 많은 날) 그대로 사용 | `completeness` 확인 후 제외/표시 |
| 조사간격 7·10일 하드코딩 | 조사일에서 자동 추정, 불규칙 허용 |
| -99.9 등 오류값을 그대로 평균 | 범위 이탈값 결측 처리 후 집계 |
| 10분 원자료 QC 없이 집계 | 먼저 모니터링(`run_monitor.py`)으로 결측·오류 확인 |
| 처리구별 센서를 평균내 하나로 | 처리구가 다르면 `sensor_map.yaml` + `--by-treatment` 로 분리 |
| 타임존 불일치로 날짜 경계 어긋남 | timestamp 를 KST 로 통일 후 `date` 산출 |

---

## 7. 다음 단계

- **결측 보정**(외부 기상 데이터 기반 PPFD 채우기): `agri-ppfd-gap-fill`
- **통계 검정**(정규성·등분산·ANOVA·사후검정): `agri-stats-workflow`, R 스크립트는 `R/env_growth_match.R` 참조
- **그래프**: `agri-ggplot-style`, `agri-ppfd-plot`
