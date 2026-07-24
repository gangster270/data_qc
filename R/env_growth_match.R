# =====================================================================
# 환경(10분) → 생육조사 구간(7·10일) 시차 매칭  [R 버전]
#
# Python 파이프라인(src/preprocess.py)과 동일한 규칙을 R 로 구현한 것.
# 이후 통계분석(agri-stats-workflow)·그래프(agri-ggplot-style)가 R 이므로,
# 분석을 R 안에서 끝내고 싶을 때 이 스크립트를 사용한다.
#
# 입력
#   env_10min.csv : timestamp, temp, rh, soil_temp, vwc, ppfd, solar, ec
#                   (Python QC 를 거친 표준화 자료 권장)
#   growth.csv    : date, trt, rep, 생육형질들...
# 출력
#   daily_env_summary.csv / env_interval_summary.csv / merged_env_growth.csv
# =====================================================================

library(tidyverse)
library(lubridate)

# ---------------------------------------------------------------------
# 0. 설정
# ---------------------------------------------------------------------
ENV_FILE   <- "data/env_10min.csv"
GROWTH_FILE<- "data/growth.csv"
OUT_DIR    <- "outputs"

INTERVAL_MIN <- 10          # 기록 간격(분)
GDD_BASE     <- 10          # 적산온도 기준온도(℃)
LAG_DAYS     <- 0           # 시차(일): 환경 구간을 N일 앞당김
WINDOW_DAYS  <- NA          # 고정 창 길이(일). NA 면 직전 조사일 다음날~당일
FIRST_START  <- NA          # 첫 구간 시작일("2026-04-01"). NA 면 자동
MIN_COMPLETENESS <- 0.90    # 하루 유효 레코드 비율 기준

# 센서 물리범위(범위 이탈값은 집계 전에 결측 처리)
RANGES <- list(
  temp = c(-20, 60), rh = c(0, 100), soil_temp = c(-10, 60),
  vwc = c(0, 0.75), ppfd = c(0, 2500), solar = c(0, 1400), ec = c(0, 20)
)

dir.create(OUT_DIR, showWarnings = FALSE, recursive = TRUE)
expected_records <- 24 * 60 / INTERVAL_MIN     # 10분 → 144
interval_sec     <- INTERVAL_MIN * 60

# ---------------------------------------------------------------------
# 1. 읽기 · 정리
# ---------------------------------------------------------------------
env <- read_csv(ENV_FILE, show_col_types = FALSE) %>%
  mutate(timestamp = ymd_hms(timestamp, tz = "Asia/Seoul", quiet = TRUE)) %>%
  filter(!is.na(timestamp)) %>%
  arrange(timestamp) %>%
  distinct(timestamp, .keep_all = TRUE)        # 재내려받기 중복 제거(첫 값 유지)

# 범위 이탈값 → NA (오류코드 -99.9 하나가 일평균·일최저를 망친다)
for (v in names(RANGES)) {
  if (v %in% names(env)) {
    lim <- RANGES[[v]]
    n_bad <- sum(!is.na(env[[v]]) & (env[[v]] < lim[1] | env[[v]] > lim[2]))
    if (n_bad > 0) {
      message(sprintf("범위 이탈값 결측 처리: %s %d건", v, n_bad))
      env[[v]][!is.na(env[[v]]) & (env[[v]] < lim[1] | env[[v]] > lim[2])] <- NA
    }
  }
}

# VPD 는 반드시 10분 자료에서 계산 후 평균(일평균 T·RH 로 계산하면 과소평가)
if (all(c("temp", "rh") %in% names(env))) {
  env <- env %>%
    mutate(es  = 0.6108 * exp(17.27 * temp / (temp + 237.3)),
           vpd = es * (1 - rh / 100)) %>%
    select(-es)
}

growth <- read_csv(GROWTH_FILE, show_col_types = FALSE) %>%
  mutate(date = as_date(date))

# ---------------------------------------------------------------------
# 2. Step 1 — 10분 → 일별
# ---------------------------------------------------------------------
daily <- env %>%
  mutate(date = as_date(timestamp),
         hour = hour(timestamp)) %>%
  group_by(date) %>%
  summarise(
    n_records   = sum(!is.na(temp) | !is.na(rh) | !is.na(ppfd)),
    completeness= n_records / expected_records,
    is_complete = completeness >= MIN_COMPLETENESS,
    temp_mean = mean(temp, na.rm = TRUE),
    temp_min  = min(temp,  na.rm = TRUE),
    temp_max  = max(temp,  na.rm = TRUE),
    temp_amp  = temp_max - temp_min,                       # 일교차
    gdd       = pmax(mean(temp, na.rm = TRUE) - GDD_BASE, 0),
    rh_mean   = mean(rh, na.rm = TRUE),
    vpd_mean  = mean(vpd, na.rm = TRUE),
    vpd_day   = mean(vpd[hour >= 9 & hour < 15], na.rm = TRUE),
    soil_temp_mean = mean(soil_temp, na.rm = TRUE),
    vwc_mean  = mean(vwc, na.rm = TRUE),
    ec_mean   = mean(ec,  na.rm = TRUE),
    dli       = sum(ppfd, na.rm = TRUE) * interval_sec / 1e6,   # mol m-2 d-1
    photoperiod_h = sum(ppfd > 10, na.rm = TRUE) * INTERVAL_MIN / 60,
    solar_MJ  = sum(solar, na.rm = TRUE) * interval_sec / 1e6,  # MJ m-2 d-1
    .groups = "drop"
  ) %>%
  mutate(across(where(is.numeric), ~ ifelse(is.infinite(.), NA, .)))

write_csv(daily, file.path(OUT_DIR, "daily_env_summary.csv"))
message(sprintf("일별 요약 %d일 (불완전일 %d일)", nrow(daily), sum(!daily$is_complete)))

# ---------------------------------------------------------------------
# 3. Step 2 — 조사일 → 구간 정의(시차 반영)
# ---------------------------------------------------------------------
g_dates <- sort(unique(growth$date))

# 조사간격 자동 추정(7/10일). 하드코딩하지 않는다.
diff_days <- as.integer(diff(g_dates))
cadence   <- if (length(diff_days)) as.integer(names(sort(table(diff_days), decreasing = TRUE))[1]) else 10L
message("추정 조사간격: ", cadence, "일")

interval_tbl <- tibble(end = g_dates) %>%
  mutate(
    start = if (!is.na(WINDOW_DAYS)) end - days(WINDOW_DAYS - 1) else lag(end) + days(1),
    start = if_else(is.na(start),
                    if (!is.na(FIRST_START)) as_date(FIRST_START) else min(g_dates) - days(cadence - 1),
                    start),
    # 시차: 구간 전체를 LAG_DAYS 만큼 과거로 이동
    start = start - days(LAG_DAYS),
    end   = end   - days(LAG_DAYS),
    interval_id = row_number(),
    growth_date = g_dates,
    days_expected = as.integer(end - start) + 1
  )

# ---------------------------------------------------------------------
# 4. 구간 집계 (평균형=평균, 적산형=합계)
# ---------------------------------------------------------------------
env_by_interval <- interval_tbl %>%
  rowwise() %>%
  mutate(agg = list(
    daily %>%
      filter(date >= start, date <= end) %>%
      summarise(
        days_used   = n(),
        record_completeness = mean(completeness, na.rm = TRUE),
        n_incomplete_days   = sum(!is_complete),
        temp_mean = mean(temp_mean, na.rm = TRUE),
        temp_min  = min(temp_min,  na.rm = TRUE),      # 구간 중 최저
        temp_max  = max(temp_max,  na.rm = TRUE),      # 구간 중 최고
        temp_min_mean = mean(temp_min, na.rm = TRUE),  # 일최저의 평균
        temp_max_mean = mean(temp_max, na.rm = TRUE),
        temp_amp_mean = mean(temp_amp, na.rm = TRUE),
        rh_mean   = mean(rh_mean,  na.rm = TRUE),
        vpd_mean  = mean(vpd_mean, na.rm = TRUE),
        vpd_day   = mean(vpd_day,  na.rm = TRUE),
        soil_temp_mean = mean(soil_temp_mean, na.rm = TRUE),
        vwc_mean  = mean(vwc_mean, na.rm = TRUE),
        ec_mean   = mean(ec_mean,  na.rm = TRUE),
        dli_mean  = mean(dli, na.rm = TRUE),           # 일평균 DLI
        dli_sum   = sum(dli,  na.rm = TRUE),           # 구간 누적 광량
        gdd_sum   = sum(gdd,  na.rm = TRUE),
        solar_MJ_sum = sum(solar_MJ, na.rm = TRUE),
        photoperiod_h = mean(photoperiod_h, na.rm = TRUE)
      )
  )) %>%
  unnest(agg) %>%
  ungroup() %>%
  mutate(
    lag_days     = LAG_DAYS,
    day_coverage = days_used / days_expected,
    cum_dli      = cumsum(replace_na(dli_sum, 0)),
    cum_gdd      = cumsum(replace_na(gdd_sum, 0)),
    quality_flag = case_when(
      days_used == 0                  ~ "환경자료 없음",
      day_coverage < 0.9              ~ sprintf("일수부족(%d/%d일)", days_used, days_expected),
      record_completeness < 0.9       ~ sprintf("레코드결측(%.2f)", record_completeness),
      TRUE                            ~ "정상"
    )
  ) %>%
  rename(env_start = start, env_end = end)

write_csv(env_by_interval, file.path(OUT_DIR, "env_interval_summary.csv"))

bad <- filter(env_by_interval, quality_flag != "정상")
if (nrow(bad)) {
  message("⚠ 품질 주의 구간:")
  walk2(bad$interval_id, bad$quality_flag, ~ message("  - 구간", .x, ": ", .y))
}

# ---------------------------------------------------------------------
# 5. 생육 자료와 병합 (핵심 산출물)
# ---------------------------------------------------------------------
merged <- growth %>%
  left_join(env_by_interval, by = c("date" = "growth_date"))

write_csv(merged, file.path(OUT_DIR, "merged_env_growth.csv"))
message("병합 완료 → ", file.path(OUT_DIR, "merged_env_growth.csv"))

# ---------------------------------------------------------------------
# 6. 구간 생육량 · 생육속도 (선택)
# ---------------------------------------------------------------------
if (all(c("trt", "rep") %in% names(merged))) {
  merged <- merged %>%
    arrange(trt, rep, date) %>%
    group_by(trt, rep) %>%
    mutate(across(any_of(c("fresh_wt", "dry_wt", "plant_height", "leaf_number")),
                  list(delta = ~ . - lag(.),
                       rate  = ~ (. - lag(.)) / days_expected),
                  .names = "{.col}_{.fn}")) %>%
    ungroup()
  write_csv(merged, file.path(OUT_DIR, "merged_env_growth.csv"))
}

# ---------------------------------------------------------------------
# 7. 관계 분석 예시 (통계 상세는 agri-stats-workflow 참조)
# ---------------------------------------------------------------------
if (all(c("dli_sum", "temp_mean") %in% names(merged)) && "fresh_wt" %in% names(merged)) {
  print(merged %>%
          select(any_of(c("dli_sum", "temp_mean", "temp_max", "vpd_day", "fresh_wt", "dry_wt"))) %>%
          cor(use = "pairwise.complete.obs") %>% round(3))
  print(summary(lm(fresh_wt ~ dli_sum + temp_mean, data = merged)))
}
