"""화면에 쓰는 말 모음 — 코드·전문용어를 사람 말로 바꾸는 사전.

규칙 이름(R06_daytime_dark)이나 열 이름(vwc_mean)을 그대로 보여주면
무슨 뜻인지 알 수 없다. 화면에는 **무슨 일이 났고 무엇을 하면 되는지**를 쓰고,
원래 코드는 참고용으로만 작게 표시한다.
"""

from __future__ import annotations

# ---------------------------------------------------------------------
# 알림 규칙 → (제목, 뜻, 할 일)
# ---------------------------------------------------------------------
RULE_TEXT = {
    "R01_timestamp_gap": (
        "기록이 끊겼습니다",
        "그 시간 동안 로거가 아무것도 기록하지 않았습니다.",
        "정전·배터리·통신을 확인하세요. 짧은 끊김은 그냥 두어도 됩니다."),
    "R02_missing_ratio": (
        "그날 자료가 많이 비었습니다",
        "해당 센서 값이 하루 중 상당 부분 비어 있습니다.",
        "커넥터가 헐겁지 않은지, 그날 점검·교체가 있었는지 확인하세요."),
    "R03_out_of_range": (
        "있을 수 없는 값이 들어왔습니다",
        "센서가 낼 수 없는 범위의 숫자입니다(배선 문제일 때 흔함).",
        "케이블·커넥터를 점검하세요. 이 값들은 집계에서 자동 제외됩니다."),
    "R04_flatline": (
        "값이 전혀 변하지 않습니다",
        "같은 숫자가 오래 이어집니다. 센서가 멈췄거나 연결이 빠진 상태일 수 있습니다.",
        "센서가 제자리에 꽂혀 있는지 확인하세요. 전 구간 같은 값이면 미연결입니다."),
    "R05_spike": (
        "값이 갑자기 튑니다",
        "짧은 시간에 비현실적으로 크게 변했습니다(접촉 불량·노이즈).",
        "배선 정리와 접지를 확인하세요."),
    "R06_daytime_dark": (
        "낮인데 광센서가 어둡습니다",
        "한낮에도 광량이 거의 0입니다. 센서가 떨어졌거나 완전히 가려진 상태입니다.",
        "센서 위치와 오염(먼지·물때)을 확인하세요."),
    "R07_night_light": (
        "밤에 빛이 감지됩니다",
        "야간에 광량이 잡힙니다. 야간보광 처리구라면 정상입니다.",
        "보광 시험 중이면 무시하세요. 아니라면 빛 새는 곳을 찾으세요."),
    "R08_rh_saturated": (
        "습도가 계속 100%에 붙어 있습니다",
        "결로나 필터 오염일 때 나타납니다.",
        "센서 필터를 확인·교체하고 통풍 상태를 보세요."),
    "R09_logger_offline": (
        "최근 자료가 들어오지 않습니다",
        "마지막 기록이 오래됐습니다. 로거가 꺼졌거나 아직 내려받지 않은 것입니다.",
        "수동으로 내려받는 운영이면 정상입니다. 아니면 전원·통신을 확인하세요."),
    "R10_pair_divergence": (
        "같은 종류 센서끼리 값 차이가 큽니다",
        "나란히 설치한 센서 사이 편차가 허용치를 넘었습니다.",
        "처리구가 서로 다르면 정상입니다(기본 꺼짐). 같은 자리라면 교정이 필요합니다."),
    "R11_transmittance_drop": (
        "광 투과가 갑자기 떨어졌습니다",
        "바깥 대비 안쪽 광량 비율이 급락했습니다.",
        "광센서 렌즈 오염, 차광막, 구조물 그늘을 확인하세요."),
    "R12_error_value": (
        "센서가 오류값을 냈습니다",
        "숫자가 아닌 오류 표시(#VALUE!, ERROR 등)가 섞여 있습니다.",
        "해당 기간 자료는 해석에서 제외하고 센서를 점검하세요."),
    "R13_heat_event": (
        "작물에 위험한 고온이 있었습니다",
        "센서 오류가 아니라 실제로 온도가 높았습니다.",
        "그날 환기·차광 상황을 확인하고, 그 구간 생육 해석에 반영하세요."),
    "R00_rule_error": (
        "점검 중 오류가 났습니다", "규칙 하나가 실행되지 못했습니다.",
        "다른 점검 결과는 정상입니다. 반복되면 알려주세요."),
}

LEVEL_TEXT = {
    "CRITICAL": ("🔴", "조치 필요"),
    "WARN": ("🟠", "확인 권장"),
    "INFO": ("🔵", "참고"),
}

# ---------------------------------------------------------------------
# 열 이름 → 사람이 읽는 이름
# ---------------------------------------------------------------------
COLUMN_TEXT = {
    "date": "날짜", "trt": "처리구", "logger": "구역", "serial": "로거번호",
    "completeness": "수신율", "n_records": "기록수", "is_complete": "완전한날",
    "temp_mean": "평균기온", "temp_min": "최저기온", "temp_max": "최고기온",
    "temp_amp": "일교차", "temp_amp_mean": "평균일교차",
    "rh_mean": "평균습도", "rh_min": "최저습도", "rh_max": "최고습도",
    "vpd_mean": "VPD", "vpd_day": "주간VPD",
    "soil_temp_mean": "배지온도", "soil_temp_min": "배지온도(최저)", "soil_temp_max": "배지온도(최고)",
    "vwc_mean": "배지수분", "vwc_min": "배지수분(최저)", "vwc_max": "배지수분(최고)",
    "ec_mean": "EC", "co2_mean": "CO2",
    "dli": "DLI(하루광량)", "dli_sum": "누적광량", "dli_mean": "일평균광량",
    "photoperiod_h": "광주기(시간)", "ppfd_day_mean": "주간평균PPFD", "ppfd_max": "최고PPFD",
    "solar_MJ": "일사량", "solar_MJ_sum": "누적일사량",
    "gdd": "적산온도", "gdd_sum": "누적적산온도", "cum_dli": "시험누적광량", "cum_gdd": "시험누적적산온도",
    "growth_date": "조사일", "env_start": "환경 시작일", "env_end": "환경 종료일",
    "days_expected": "구간일수", "days_used": "사용일수", "day_coverage": "구간충족률",
    "record_completeness": "수신율", "quality_flag": "품질", "interval_id": "구간",
    "lag_days": "시차(일)", "n_incomplete_days": "불완전일",
}

# 표에서 기본으로 보여줄 열(나머지는 '전체 열 보기'로)
DAILY_KEYS = ["date", "trt", "completeness", "temp_mean", "temp_min", "temp_max",
              "rh_mean", "vpd_mean", "dli", "photoperiod_h", "soil_temp_mean",
              "vwc_mean", "ec_mean", "solar_MJ", "gdd"]
INTERVAL_KEYS = ["interval_id", "trt", "growth_date", "env_start", "env_end", "days_used",
                 "temp_mean", "temp_min", "temp_max", "rh_mean", "vpd_mean",
                 "dli_sum", "dli_mean", "gdd_sum", "vwc_mean", "soil_temp_mean",
                 "quality_flag"]

VARIABLE_TEXT = {
    "temp": "기온", "rh": "습도", "soil_temp": "배지온도", "vwc": "배지수분",
    "ppfd": "광량(PPFD)", "solar": "일사량", "ec": "EC", "co2": "CO2",
    "timestamp": "기록시각", "logger": "로거",
}


def rule_title(rule: str) -> str:
    return RULE_TEXT.get(str(rule), (str(rule), "", ""))[0]


def rule_meaning(rule: str) -> str:
    return RULE_TEXT.get(str(rule), ("", "", ""))[1]


def rule_action(rule: str) -> str:
    return RULE_TEXT.get(str(rule), ("", "", ""))[2]


def var_name(var: str) -> str:
    base = str(var).split("__rep")[0]
    label = VARIABLE_TEXT.get(base, base)
    if "__rep" in str(var):
        label += f" #{str(var).split('__rep')[1]}"
    return label


def friendly_columns(df, keys=None):
    """열 이름을 사람이 읽는 이름으로 바꾸고, 지정한 열만 남긴다."""
    if df is None or df.empty:
        return df
    if keys:
        cols = [c for c in keys if c in df.columns]
        cols += [c for c in df.columns if c not in cols and c in ("logger",)]
        df = df[cols]
    return df.rename(columns={c: COLUMN_TEXT.get(c, c) for c in df.columns})
