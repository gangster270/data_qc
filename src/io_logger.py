"""10분 단위 환경 로거 파일 읽기 · 열 이름 표준화.

METER ZL6 / ZENTRA Cloud export(.xlsx) 특유의 형식을 다룬다.
  - 시트 우선순위: 'Processed Data'(보정값) > Metadata 아닌 첫 시트
  - 헤더가 1행이 아니라 3행(실변수명 + 단위 접두)에 있음 → Timestamp 셀로 헤더행 자동 탐지
  - 포트별 빈 열, 중복 열 이름 존재
  - 같은 로거를 여러 번 내려받아 기간이 겹침 → 중복 timestamp 다수(정상)

여기서는 '표준 변수키'로 정규화까지 수행한다:
  temp(온도) / rh(습도) / soil_temp(배지온도) / vwc(배지습도) /
  ppfd(PPFD) / solar(일사량) / ec(EC) / co2(CO2)

주의(agri-logger-qc 규칙 유지):
  - NaN 은 오류가 아니다. 진성 오류토큰(#VALUE!, ERROR, inf 등)만 오류로 센다.
  - 여러 파일 병합은 '열의 합집합' 기준. 첫 파일 스키마로 고정하면 센서가 조용히 사라진다.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------
# 탐지 키워드
# ---------------------------------------------------------------------
TIMESTAMP_CANDIDATES = [
    "timestamp", "datetime", "date_time", "date", "time",
    "일시", "측정일시", "측정시간", "수집시간", "날짜",
]

# 표준 변수키 → (include 키워드, exclude 키워드)
# 'Air Temperature' 와 'Soil Temperature' 가 모두 temperature 를 포함하므로
# 온도(기온)는 soil/substrate/water/logger 를 반드시 제외한다.
# 로거 제조사마다 표기가 다르므로 별칭을 넓게 잡되, 서로 침범하지 않도록 exclude 로 가른다.
# 비교는 _normalize_key() 로 공백·기호·대소문자를 없앤 뒤 수행한다
# (SoilTemp / soil_temp / Soil Temperature / 배지 온도 → 모두 동일 인식).
VARIABLE_CANDIDATES = {
    "temp": {
        "include": ["air temperature", "air_temp", "air temp", "기온", "대기온도", "실내온도",
                    "외기온도", "외부온도", "온도", "temperature", "temp"],
        "exclude": ["soil", "substrate", "근권", "배지", "토양", "지중", "수온", "급액", "양액",
                    "water", "logger", "device", "dew", "배양액"],
    },
    "rh": {
        "include": ["relative humidity", "humidity", "humid", "rh", "상대습도", "습도"],
        "exclude": ["soil", "substrate", "근권", "배지", "토양"],
    },
    "soil_temp": {
        "include": ["soil temperature", "soil temp", "substrate_temp", "medium_temp",
                    "근권온도", "배지온도", "토양온도", "지중온도", "지온"],
        "exclude": [],
    },
    "vwc": {
        "include": ["water content", "soil moisture", "substrate_moisture", "medium_moisture",
                    "vwc", "근권수분", "배지수분", "배지습도", "토양수분", "함수율", "수분함량", "수분"],
        "exclude": ["급액", "양액"],
    },
    "ppfd": {"include": ["ppfd", "par", "광량", "광합성광량자속밀도", "광량자", "광합성유효광"], "exclude": []},
    "solar": {"include": ["solar radiation", "solar", "일사", "전천일사", "일사량", "복사"], "exclude": []},
    "ec": {"include": ["saturation extract ec", "conductivity", "ec", "전기전도도"], "exclude": ["급액", "양액"]},
    "co2": {"include": ["co2", "carbon dioxide", "이산화탄소", "탄산가스"], "exclude": []},
}

# 작물환경 변수가 아닌 로거 내부/장비 채널 — 표준화 대상에서 제외
EXCLUDE_KEYWORDS = [
    "logger temperature", "logger temp", "device temp",
    "battery", "voltage", "reference pressure", "barometer", "atmospheric pressure",
]

# 값이 아니라 행 번호인 열(HOBO 의 '#' 등)
INDEX_COL_NAMES = {"#", "no", "no.", "번호", "index", "idx", "seq", "순번"}

NAN_TOKENS = {"", "nan", "na", "n/a", "#n/a", "null", "none", "-"}
HARD_ERROR_TOKENS = {
    "#value!", "#div/0!", "#ref!", "#name?", "#null!", "#num!",
    "error", "err", "inf", "+inf", "-inf",
    "overrange", "underrange", "openc", "shortc",
}

STANDARD_ORDER = ["temp", "rh", "soil_temp", "vwc", "ppfd", "solar", "ec", "co2"]
LABELS = {
    "temp": "온도", "rh": "습도", "soil_temp": "배지온도", "vwc": "배지습도",
    "ppfd": "PPFD", "solar": "일사량", "ec": "EC", "co2": "CO2",
}


# ---------------------------------------------------------------------
# 1. 파일 읽기
# ---------------------------------------------------------------------
def pick_sheets(sheet_names) -> list[str]:
    """읽을 시트를 **모두** 고른다.

    센서 구성이 바뀌면 로거가 `Processed Data Config 1`, `Config 2` … 처럼
    시트를 나눠서 내보낸다. 한 시트만 읽으면 구성 변경 이후(또는 이전) 자료가
    통째로 사라지므로 **같은 종류의 시트를 전부 읽어 합친다.**

    우선순위: Processed(보정값) 전부 > Raw 전부 > Metadata 아닌 시트 전부.
    (Processed 와 Raw 는 같은 자료의 두 표현이므로 절대 함께 읽지 않는다)
    """
    names = [str(s) for s in sheet_names]
    processed = [s for s in names if "processed" in s.lower()]
    if processed:
        return processed
    raw = [s for s in names if "raw" in s.lower()]
    if raw:
        return raw
    non_meta = [s for s in names if "metadata" not in s.lower()]
    return non_meta or names[:1]


_HEADER_HINTS = ("timestamp", "일시", "datetime", "날짜", "date/time", "date time")


def _detect_header_row(raw: pd.DataFrame, max_scan: int = 20) -> int:
    """실제 변수명이 있는 헤더 행 번호를 찾는다.

    ZL6 는 3행(index 2), HOBO 는 2행(index 1)에 헤더가 있고 그 위에 메타행이 있다.
    """
    keys = {k.lower() for k in TIMESTAMP_CANDIDATES}
    for i in range(min(max_scan, len(raw))):
        cells = [str(c).strip().lower() for c in raw.iloc[i].tolist() if c is not None]
        if not cells:
            continue
        if any(c in keys for c in cells):
            return i
        if any(any(h in c for h in _HEADER_HINTS) for c in cells):
            return i
    return 0


def _build_unique_columns(header_cells, port_cells=None) -> list[str]:
    """헤더 셀을 고유 열 이름으로 만든다.

    port_cells(위쪽 'Port N' 행)가 있으면 **포트를 접두로 붙인다**.
    센서 구성이 바뀌어 시트가 나뉘어도 포트 기준으로 열이 맞물려,
    Config 1·2 를 합칠 때 다른 포트의 값이 한 열로 섞이지 않는다.
    """
    columns, seen = [], {}
    for i, c in enumerate(header_cells):
        name = "" if c is None else str(c).strip()
        if name == "" or name.lower() == "nan":
            name = f"Unnamed_{i}"
        if port_cells is not None and i < len(port_cells):
            port = "" if port_cells[i] is None else str(port_cells[i]).strip()
            if re.fullmatch(r"[Pp]ort\s*\d+", port) and name.lower() != "timestamp":
                name = f"{port.replace(' ', '')} {name}".strip()
        if name in seen:
            seen[name] += 1
            name = f"{name}.{seen[name]}"
        else:
            seen[name] = 0
        columns.append(name)
    return columns


CSV_ENCODINGS = ["utf-8-sig", "cp949", "euc-kr", "utf-16", "latin1"]


def _decode_text(source) -> str:
    """파일/버퍼를 문자열로 읽는다(UTF-8·CP949·EUC-KR·UTF-16 순으로 시도)."""
    if hasattr(source, "read"):
        if hasattr(source, "seek"):
            source.seek(0)
        data = source.read()
        if isinstance(data, str):
            return data
    else:
        data = Path(source).read_bytes()
    for enc in CSV_ENCODINGS:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _read_csv_any_encoding(source, sep=None) -> pd.DataFrame:
    """구분자·인코딩·불규칙 행 길이를 모두 견디는 CSV 리더.

    로거 CSV 는 상단에 필드 수가 다른 메타행이 섞여 있는 경우가 흔하다
    (예: HOBO 의 `"차트 제목: 22094002"` 한 칸짜리 첫 행). pandas 기본 리더는
    이런 파일에서 'Expected 1 fields' 로 실패하므로, 직접 파싱해 최대 열 수에
    맞춰 채운다.
    """
    text = _decode_text(source)
    lines = [ln for ln in text.splitlines() if ln.strip() != ""]
    if not lines:
        return pd.DataFrame()

    if sep is None:                        # 앞부분에서 가장 많이 쓰인 구분자 추정
        head = lines[:30]
        counts = {d: sum(ln.count(d) for ln in head) for d in [",", ";", "\t", "|"]}
        sep = max(counts, key=counts.get)
        if counts[sep] == 0:
            sep = ","

    rows = list(csv.reader(lines, delimiter=sep))
    width = max(len(r) for r in rows)
    rows = [r + [None] * (width - len(r)) for r in rows]
    return pd.DataFrame(rows, dtype=object)


# 로거 일련번호(자체 센서 로거 번호)를 파일 안에서 찾는 패턴
_SERIAL_PATTERNS = [
    re.compile(r"차트\s*제목\s*[:：]\s*([\w\-]+)"),          # HOBO 한국어 export
    re.compile(r"plot\s*title\s*[:：]\s*([\w\-]+)", re.I),   # HOBO 영문 export
    re.compile(r"LGR\s*S/N\s*[:：]\s*([\w\-]+)", re.I),      # HOBO 헤더
    re.compile(r"serial(?:\s*number)?\s*[:：]\s*([\w\-]+)", re.I),
    re.compile(r"\b(z6-\d{4,6})\b", re.I),                   # METER ZL6
]
def _looks_like_date_token(tok: str) -> bool:
    """`260703`(YYMMDD)·`20260703`(YYYYMMDD)처럼 내려받은 날짜로 보이는 토큰인가.

    8자리 일련번호(22094002)가 날짜로 오인되지 않도록 월·일 범위까지 확인한다
    (22-09-40 → 40일은 없으므로 날짜가 아니다).
    """
    if not tok.isdigit():
        return False
    if len(tok) == 6:
        mm, dd = int(tok[2:4]), int(tok[4:6])
    elif len(tok) == 8 and tok[:2] in ("19", "20"):
        mm, dd = int(tok[4:6]), int(tok[6:8])
    else:
        return False
    return 1 <= mm <= 12 and 1 <= dd <= 31


def detect_serial_from_text(text: str) -> str | None:
    """파일 상단 텍스트에서 로거 일련번호를 찾는다."""
    for pat in _SERIAL_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1).strip()
    return None


def serial_from_filename(filename: str) -> str | None:
    """파일명에서 로거 번호로 보이는 토큰을 고른다.

    `260703_22094002.csv` 처럼 앞에 내려받은 날짜가 붙는 경우가 많다. 날짜로
    보이는 토큰(YYMMDD·YYYYMMDD)은 건너뛰고, 남은 토큰 중 **숫자를 포함한
    가장 긴 것**을 로거 번호로 본다(업로드 과정에서 붙는 해시 접두도 제거).
    """
    stem = Path(str(filename)).stem
    stem = re.sub(r"^[0-9a-f]{6,}-", "", stem)            # 업로드 해시 접두 제거
    tokens = [tok for tok in re.split(r"[_\s]+", stem) if tok]   # '-' 는 번호의 일부(z6-03959)

    # ① 글자+숫자가 섞인 토큰(z6-03959, PAR1-22070169)이 있으면 그것이 로거 번호
    named = [tok for tok in tokens if re.search(r"[A-Za-z가-힣]", tok) and re.search(r"\d{3,}", tok)]
    if named:
        return named[0]
    # ② 숫자만이면 '내려받은 날짜'로 보이는 토큰을 빼고 첫 번째를 쓴다
    numeric = [tok for tok in tokens if re.search(r"\d{4,}", tok) and not _looks_like_date_token(tok)]
    if numeric:
        return numeric[0]
    # ③ 번호처럼 보이는 토큰이 없으면 첫 토큰을 식별자로 쓴다
    #    (`구역A_1.xlsx`, `구역A_2.xlsx` 처럼 뒤에 회차만 붙는 이름도 한 로거로 묶이도록)
    return (tokens[0] if tokens else stem) or None


def _frame_from_raw(raw: pd.DataFrame, sheet: str | None = None) -> pd.DataFrame:
    """헤더 행을 찾아 표를 정리한다(포트 접두 포함)."""
    hrow = _detect_header_row(raw)
    port_cells = raw.iloc[hrow - 2].tolist() if hrow >= 2 else None
    columns = _build_unique_columns(raw.iloc[hrow].tolist(), port_cells)
    df = raw.iloc[hrow + 1:].copy()
    df.columns = columns
    df = df.reset_index(drop=True).dropna(axis=1, how="all")
    if sheet:
        df["_sheet"] = sheet
    return df


def read_env_file(source, filename: str | None = None) -> pd.DataFrame:
    """단일 환경 로거 파일(.xlsx/.xls/.csv/.txt/.tsv)을 DataFrame 으로 읽는다.

    ZL6 전용이 아니다. 구분자·인코딩·헤더 위치를 자동 판별하므로 국산 로거·
    HOBO·자체 기록 파일도 그대로 읽힌다.

    **엑셀은 시트를 모두 읽어 합친다.** 센서 구성이 바뀌면 로거가
    `Processed Data Config 1`, `Config 2` … 로 시트를 나눠 내보내는데,
    한 시트만 읽으면 나머지 기간이 통째로 사라진다. 열은 포트 기준으로
    이름이 붙으므로 구성이 달라도 같은 포트끼리 이어진다.
    """
    name = filename or (Path(source).name if isinstance(source, (str, Path)) else "uploaded")
    suffix = Path(name).suffix.lower()

    serial = None
    if suffix in (".csv", ".txt", ".tsv"):
        sep = "\t" if suffix == ".tsv" else None   # None → 구분자 자동 추론(, ; \t |)
        raw = _read_csv_any_encoding(source, sep=sep)
        if raw.empty:
            raise ValueError(f"{name}: 빈 파일")
        head_text = " ".join(str(c) for c in raw.iloc[:3].to_numpy().ravel() if c is not None)
        serial = detect_serial_from_text(head_text)
        df = _frame_from_raw(raw)
    else:
        xls = pd.ExcelFile(source)
        sheets = pick_sheets(xls.sheet_names)
        frames = []
        for sheet in sheets:
            raw = pd.read_excel(xls, sheet_name=sheet, header=None, dtype=object)
            if raw.empty:
                continue
            part = _frame_from_raw(raw, sheet=sheet)
            if len(part):
                frames.append(part)
        if not frames:
            raise ValueError(f"{name}: 빈 파일")
        first_raw = pd.read_excel(xls, sheet_name=sheets[0], header=None, dtype=object, nrows=3)
        head_text = " ".join(str(c) for c in first_raw.to_numpy().ravel() if c is not None)
        serial = detect_serial_from_text(head_text)
        if serial is None:
            a1 = str(first_raw.iloc[0, 0]).strip() if first_raw.shape[1] else ""
            # 임의의 제목 텍스트를 번호로 오인하지 않도록, 숫자 4자리 이상 포함한
            # 짧은 식별자일 때만 채택한다(예: 'z6-03959', 'A-10293').
            if a1 and len(a1) <= 24 and re.search(r"\d{4,}", a1) and " " not in a1:
                serial = a1
        # 시트별 구성이 달라도 열의 합집합으로 이어붙인다(포트 기준 정렬)
        df = pd.concat(frames, ignore_index=True, sort=False) if len(frames) > 1 else frames[0]

    df["_source_file"] = name
    df["_serial"] = serial or serial_from_filename(name) or Path(name).stem
    return df


def load_env_files(sources: list) -> tuple[pd.DataFrame, list[str]]:
    """여러 파일을 '열의 합집합' 기준으로 병합한다.

    sources: 경로 문자열 리스트, 또는 (buffer, filename) 튜플 리스트.
    반환: (병합 DataFrame, 로그 메시지 리스트)
    """
    frames, log = [], []
    for s in sources:
        try:
            if isinstance(s, tuple):
                df = read_env_file(s[0], s[1])
            else:
                df = read_env_file(s)
            frames.append(df)
            log.append(f"읽음: {df['_source_file'].iloc[0]} ({len(df):,}행 × {df.shape[1] - 1}열)")
        except Exception as e:  # 개별 파일 실패가 전체를 막지 않게
            log.append(f"실패: {s} → {e}")
    if not frames:
        raise ValueError("읽을 수 있는 파일이 없습니다.")
    merged = pd.concat(frames, ignore_index=True, sort=False)   # 합집합 병합
    log.append(f"병합 결과: {len(merged):,}행 × {merged.shape[1]}열")
    return merged, log


# ---------------------------------------------------------------------
# 2. timestamp 처리
# ---------------------------------------------------------------------
# 날짜/시각 패턴 — 구분자 뒤 공백(`05. 19. 25`)과 한글 표기(`05시 30분`)까지 인식
_DATE_PAT = re.compile(r"\d{4}[-/.]\s?\d{1,2}[-/.]\s?\d{1,2}|\d{1,2}[-/.]\s?\d{1,2}[-/.]\s?\d{2,4}|"
                       r"\d{1,4}년\s?\d{1,2}월\s?\d{1,2}일")
_TIME_PAT = re.compile(r"\d{1,2}:\d{2}|\d{1,2}시\s?\d{1,2}분")


_KOREAN_DT_MAP = {"오전": "AM", "오후": "PM", "시": ":", "분": ":", "초": ""}
_KOREAN_DT_FORMATS = ["%m.%d.%y%p%I:%M:%S", "%Y.%m.%d%p%I:%M:%S",
                      "%m.%d.%Y%p%I:%M:%S", "%y.%m.%d%p%I:%M:%S"]


def parse_datetime_series(series: pd.Series) -> pd.Series:
    """시각 문자열을 datetime 으로 파싱한다(한글 표기 포함).

    HOBO 한국어 export 는 `05. 19. 25 오후 05시 30분 01초` 형태로 내보내는데
    pandas 가 그대로는 해석하지 못한다. 오전/오후·시·분·초를 치환해 파싱한다.
    """
    out = pd.to_datetime(series, errors="coerce")
    if out.notna().mean() > 0.9:
        return out

    text = series.astype(str)
    if not text.str.contains("오전|오후|시|분", regex=True).any():
        return out

    conv = text.str.strip()
    for k, v in _KOREAN_DT_MAP.items():
        conv = conv.str.replace(k, v, regex=False)
    conv = conv.str.replace(r"\s+", "", regex=True).str.replace(":+", ":", regex=True)
    conv = conv.str.rstrip(":")
    best = out
    for fmt in _KOREAN_DT_FORMATS:
        cand = pd.to_datetime(conv, format=fmt, errors="coerce")
        if cand.notna().mean() > (best.notna().mean() if best is not None else 0):
            best = cand
        if best.notna().mean() > 0.95:
            break
    return best


def detect_interval_minutes(timestamps, default: float = 10.0) -> float:
    """기록 간격(분)을 자료에서 추정한다(간격 최빈값).

    10분 로거만 있는 게 아니다. 1·5·15·30·60분 어느 것이든 자료에서 읽어
    DLI·완전성·고착 판정에 일관되게 쓴다.
    """
    ts = pd.to_datetime(pd.Series(list(timestamps)), errors="coerce").dropna().sort_values()
    if len(ts) < 3:
        return float(default)
    diffs = ts.diff().dt.total_seconds().div(60).dropna()
    diffs = diffs[diffs > 0]
    if diffs.empty:
        return float(default)
    mode = diffs.mode()
    value = float(mode.iloc[0]) if len(mode) else float(diffs.median())
    # 0.1분 미만 단위의 미세한 흔들림은 반올림해 정리
    return round(value, 2) if value < 1 else round(value)


def _classify_datetime_content(series: pd.Series) -> tuple[float, float]:
    """열 값에 날짜/시각 패턴이 각각 얼마나 들어 있는지 비율로 반환."""
    sample = series.dropna().astype(str).head(300)
    if sample.empty:
        return 0.0, 0.0
    return (float(sample.str.contains(_DATE_PAT, regex=True).mean()),
            float(sample.str.contains(_TIME_PAT, regex=True).mean()))


def detect_date_time_pair(df: pd.DataFrame) -> tuple[str, str] | None:
    """날짜 열과 시간 열이 따로 있는 형식을 찾는다(예: '날짜' + '시각')."""
    date_cols, time_cols = [], []
    for col in df.columns:
        if col in ("_source_file", "timestamp"):
            continue
        has_date, has_time = _classify_datetime_content(df[col])
        if has_date > 0.8 and has_time < 0.3:
            date_cols.append(col)
        elif has_time > 0.8 and has_date < 0.3:
            time_cols.append(col)
    if date_cols and time_cols:
        return date_cols[0], time_cols[0]
    return None


def detect_timestamp_column(df: pd.DataFrame) -> str | None:
    """내용 기반으로 timestamp 열을 찾는다.

    이름만 보고 고르면 'time'(시각만 있는 열)이 오늘 날짜를 달고 정상처럼 보이는
    함정에 빠지므로, 값에 날짜와 시각이 모두 있는 열을 우선한다.
    """
    best, best_score = None, -1
    for col in df.columns:
        if col == "_source_file":
            continue
        sample = df[col].dropna().astype(str).head(200)
        if sample.empty:
            continue
        has_date = sample.str.contains(_DATE_PAT, regex=True).mean()
        has_time = sample.str.contains(_TIME_PAT, regex=True).mean()
        name_bonus = 0.3 if any(k in str(col).lower() for k in TIMESTAMP_CANDIDATES) else 0.0
        # 날짜+시각을 모두 담은 열에 높은 점수
        score = has_date * 1.0 + has_time * 1.0 + name_bonus
        if has_date < 0.5:          # 날짜가 없으면 timestamp 로 쓰지 않음
            score -= 1.0
        if score > best_score:
            best, best_score = col, score
    # 이미 datetime dtype 인 열이 있으면 그것을 우선
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            return col
    return best if best_score > 0 else None


def prepare_timestamp(df: pd.DataFrame, ts_col: str | None = None) -> tuple[pd.DataFrame, dict]:
    """timestamp 를 파싱해 'timestamp' 열로 정규화하고 중복을 제거한다.

    세 가지 형식을 모두 받는다.
      (a) 날짜+시각이 한 열에 있는 경우      → 그 열 사용
      (b) 날짜 열과 시각 열이 분리된 경우     → 두 열을 합쳐 생성
      (c) 열 이름이 제각각인 경우            → 값의 내용(날짜/시각 패턴)으로 판별

    반환: (정렬된 DataFrame, 리포트 dict)
    """
    out = df.copy()
    used_cols: list[str] = []

    if ts_col is None:
        ts_col = detect_timestamp_column(df)
        # 자동 선택한 열에 '시각'이 없으면(날짜만 있는 열) 시간 열과 합쳐야 한다.
        # 그러지 않으면 하루가 한 행으로 뭉개지고 나머지가 전부 중복으로 버려진다.
        if ts_col is not None:
            has_date, has_time = _classify_datetime_content(df[ts_col])
            if has_time < 0.5:
                pair = detect_date_time_pair(df)
                if pair is not None:
                    ts_col = None

    if ts_col is not None:
        out["timestamp"] = parse_datetime_series(out[ts_col])
        used_cols = [ts_col]
        source_desc = str(ts_col)
    else:
        pair = detect_date_time_pair(df)
        if pair is None:
            raise ValueError(
                "날짜/시간 열을 찾지 못했습니다. 파일 상단에 'Timestamp'·'일시'·'날짜'+'시간' "
                "같은 열이 있는지 확인하거나, 대시보드에서 시간 열을 직접 지정하세요.")
        d_col, t_col = pair
        out["timestamp"] = parse_datetime_series(
            out[d_col].astype(str).str.strip() + " " + out[t_col].astype(str).str.strip())
        used_cols = [d_col, t_col]
        source_desc = f"{d_col} + {t_col}"

    n_bad = int(out["timestamp"].isna().sum())
    out = out.dropna(subset=["timestamp"])
    out = out.drop(columns=[c for c in used_cols if c != "timestamp"])

    out = out.sort_values("timestamp", kind="stable")
    dup_mask = out.duplicated(subset=["timestamp"], keep="first")
    n_dup = int(dup_mask.sum())
    dup_rows = out.loc[dup_mask, ["timestamp"]].copy()
    out = out.loc[~dup_mask].reset_index(drop=True)

    report = {
        "timestamp_column": source_desc,
        "unparsed_rows": n_bad,
        "duplicate_rows": n_dup,          # 재내려받기 겹침 → 대량 중복은 정상
        "duplicates": dup_rows,
        "start": out["timestamp"].min(),
        "end": out["timestamp"].max(),
        "n_rows": len(out),
        "interval_minutes": detect_interval_minutes(out["timestamp"]),
    }
    return out, report


# ---------------------------------------------------------------------
# 3. 열 이름 표준화
# ---------------------------------------------------------------------
def _normalize_key(text: str) -> str:
    """열 이름 비교용 정규화: 소문자 + 공백·기호·단위 제거.

    'SoilTemp' / 'soil_temp' / 'Soil Temperature' / '배지 온도' 를 모두 같은
    형태로 만들어, 로거 제조사마다 다른 표기를 동일하게 인식한다.
    """
    return re.sub(r"[^0-9a-z가-힣]+", "", str(text).lower())


def _match_variable(colname: str) -> str | None:
    low = str(colname).lower()
    norm = _normalize_key(colname)
    if any(_normalize_key(k) in norm for k in EXCLUDE_KEYWORDS):
        return None
    for key in STANDARD_ORDER:
        rule = VARIABLE_CANDIDATES[key]
        if any(_normalize_key(x) and _normalize_key(x) in norm for x in rule["exclude"]):
            continue
        for inc in rule["include"]:
            inc_n = _normalize_key(inc)
            if not inc_n:
                continue
            # 짧은 영문 키워드(rh, ec, par)는 부분일치 오인이 잦아 단어 단위로만 인정한다
            # (예: 'par' 가 'parameter' 에, 'ec' 가 '급액ec' 에 잘못 붙는 것을 막는다).
            # 한글은 '외부온도'처럼 붙여 쓰므로 이 규칙을 적용하지 않는다.
            if len(inc_n) <= 3 and inc_n.isascii():
                if re.search(rf"(^|[^0-9a-z가-힣]){re.escape(inc_n)}([^0-9a-z가-힣]|$)", low) or norm == inc_n:
                    return key
            elif inc_n in norm:
                return key
    return None


META_COLS = {"timestamp", "_source_file", "_serial", "_sheet", "logger", "serial"}


def map_columns(df: pd.DataFrame, overrides: dict[str, str] | None = None) -> dict[str, list[str]]:
    """원본 열 → 표준 변수키 매핑. 같은 변수의 중복 센서는 리스트로 모은다.

    overrides 로 자동 인식을 덮어쓸 수 있다({"내부온도": "temp", "무슨열": "제외"}).
    """
    overrides = {str(k): str(v) for k, v in (overrides or {}).items()}
    mapping: dict[str, list[str]] = {}
    for col in df.columns:
        if col in META_COLS:
            continue
        if str(col) in overrides:
            key = overrides[str(col)]
            if key in ("제외", "exclude", "-", ""):
                continue
        else:
            key = _match_variable(col)
        if key:
            mapping.setdefault(key, []).append(col)
    return mapping


def to_numeric_clean(series: pd.Series) -> tuple[pd.Series, int]:
    """문자열 섞인 열을 숫자로 변환하고 '진성 오류값' 개수를 센다.

    NaN(빈칸)은 오류가 아니라 결측이므로 세지 않는다. 설치 시점이 늦은 센서의
    앞부분 결측을 오류로 오인해 열을 통째로 버리는 사고를 막기 위함이다.
    """
    s = series.astype(object)
    text = s.astype(str).str.strip().str.lower()
    is_nan_token = text.isin(NAN_TOKENS) | s.isna()
    is_hard_err = text.isin(HARD_ERROR_TOKENS)
    num = pd.to_numeric(s, errors="coerce")
    # 값이 있는데 숫자로 변환 실패 + NaN토큰도 아님 → 진성 오류
    junk = num.isna() & (~is_nan_token)
    n_error = int((is_hard_err | junk).sum())
    return num, n_error


def _is_informative(series: pd.Series) -> bool:
    """센서가 실제로 값을 내고 있는지 판정.

    미연결 포트는 NaN 이 아니라 '전 구간 0' 또는 '전 구간 동일값'으로 나온다
    (실측 사례: TEROS 2조가 처음부터 끝까지 0.00). 이런 열을 대표 센서로 뽑으면
    변수 전체가 0 이 되므로, 반드시 정보가 있는 열을 우선한다.
    """
    s = series.dropna()
    if s.empty:
        return False
    if (s == 0).all():
        return False
    return s.nunique() > 1


def sanitize_name(name: str) -> str:
    """임의의 열 이름을 안전한 변수명으로 정리한다(단위 접두·기호 제거)."""
    s = str(name).strip()
    s = re.sub(r"^[\s\[\(]*[-+0-9.]*\s*(°C|℃|%|W/m²|W/m2|kPa|mS/cm|ppm|µmol[^\s]*|umol[^\s]*|mm|m/s|hPa)\s*",
               "", s, flags=re.IGNORECASE)          # 단위 접두 제거
    s = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]\s*$", "", s).strip()   # 뒤쪽 (단위) 제거
    s = re.sub(r"[^\w가-힣]+", "_", s).strip("_")
    return s.lower() or "col"


def standardize(df: pd.DataFrame, replicate: str = "first",
                keep_unmapped: bool = True,
                overrides: dict[str, str] | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """표준 변수키 열을 가진 깔끔한 DataFrame 을 만든다.

    replicate: 같은 변수의 중복 센서 처리 방식
        "first" (기본) | "mean"(공간반복 평균) | "keep"(temp_1, temp_2 로 모두 보존)
    keep_unmapped: 표준 변수로 인식되지 않은 수치 열도 **버리지 않고** 보존한다
        (CO2·풍속·수온·pH 등 로거마다 다른 변수. 이름만 정리해 그대로 집계된다)
    overrides: {원본열: 표준변수키 또는 "제외"} — 자동 인식 결과를 사람이 고칠 때 사용

    대표 센서는 '값이 살아 있는 첫 번째 열'을 고른다(전 구간 0·상수인 죽은 포트는
    후순위로 밀고 리포트에 사유를 남긴다). 죽은 열도 열 자체는 보존해 QC 규칙이
    고착(flatline)으로 잡아낼 수 있게 한다.

    반환: (표준화 DataFrame, 매핑 리포트)
    """
    mapping = map_columns(df, overrides)
    out = pd.DataFrame({"timestamp": df["timestamp"].to_numpy()})
    rows: dict[str, dict] = {}      # 원본열 → 리포트 행

    for key, cols in mapping.items():
        numeric_cols = {}
        for c in cols:
            num, n_err = to_numeric_clean(df[c])
            numeric_cols[c] = num
            rows[c] = {
                "표준변수": key,
                "라벨": LABELS.get(key, key),
                "원본열": c,
                "유효값수": int(num.notna().sum()),
                "결측수": int(num.isna().sum()),
                "오류값수": n_err,
                "채택": "미사용(빈 포트)",
            }
        block = pd.DataFrame(numeric_cols)
        # 유효값이 하나도 없는 열(빈 포트)은 후보에서 제외
        usable = [c for c in block.columns if block[c].notna().any()]
        if not usable:
            continue

        alive = [c for c in usable if _is_informative(block[c])]
        dead_note = " ※죽은 포트(전 구간 0·상수)"

        def _note(col: str, text: str) -> str:
            return text + (dead_note if col not in alive else "")

        if len(usable) == 1:
            out[key] = block[usable[0]].to_numpy()
            rows[usable[0]]["채택"] = _note(usable[0], key)
            continue

        # 센서가 여러 개면 **파일에 실린 포트 순서 그대로** var__rep1..N 으로 보존한다.
        # (순서를 바꾸면 처리구 매핑(config/sensor_map.yaml)이 어긋난다)
        for i, c in enumerate(usable, start=1):
            out[f"{key}__rep{i}"] = block[c].to_numpy()
            rows[c]["채택"] = _note(c, f"{key}__rep{i}")

        # 대표 열(key)은 별도로 고른다: 살아 있는 첫 센서, 없으면 첫 센서.
        if replicate == "mean" and len(alive) > 1:
            # 평균은 살아 있는 센서끼리만. 죽은 0값을 섞으면 평균이 절반으로 꺼진다.
            out[key] = block[alive].mean(axis=1).to_numpy()
            for c in alive:
                rows[c]["채택"] += f" → {key}(평균 대상)"
        elif replicate != "keep":
            rep = alive[0] if alive else usable[0]
            out[key] = block[rep].to_numpy()
            rows[rep]["채택"] += f" → {key}(대표)"

    # --- 표준 변수로 인식되지 않은 수치 열도 살린다 ----------------------
    # 로거마다 CO2·풍속·수온·pH·관수량 등 제각각인 변수가 있다. 인식 못 했다고
    # 버리면 그 자료는 통째로 사라지므로, 이름만 정리해 그대로 집계 대상에 넣는다.
    mapped_sources = {c for cols in mapping.values() for c in cols}
    if keep_unmapped:
        used_names = set(out.columns)
        for col in df.columns:
            if col in META_COLS or col in mapped_sources:
                continue
            low = str(col).lower()
            if any(k in low for k in EXCLUDE_KEYWORDS):      # 배터리·로거내부온도 등
                continue
            if str(col).startswith("Unnamed_") or low.strip() in INDEX_COL_NAMES:
                continue
            num, n_err = to_numeric_clean(df[col])
            if num.notna().sum() == 0:                       # 숫자가 아예 없는 열(메모 등)
                continue
            name = sanitize_name(col)
            base_name = name
            i = 2
            while name in used_names:
                name = f"{base_name}_{i}"
                i += 1
            used_names.add(name)
            out[name] = num.to_numpy()
            rows[col] = {
                "표준변수": name, "라벨": str(col), "원본열": col,
                "유효값수": int(num.notna().sum()), "결측수": int(num.isna().sum()),
                "오류값수": n_err, "채택": f"{name}(기타 변수 — 자동 보존)",
            }

    report = pd.DataFrame(list(rows.values()))
    ordered = ["timestamp"] + [c for c in STANDARD_ORDER if c in out.columns] + \
              [c for c in out.columns if c not in STANDARD_ORDER and c != "timestamp"]
    return out[ordered], report


# ---------------------------------------------------------------------
# 4. 기록 간격 격자 정합
# ---------------------------------------------------------------------
def reindex_full_grid(df: pd.DataFrame,
                      interval_minutes: float | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """min~max 구간의 완전한 격자를 만들고 빠진 시각을 빈 행으로 채운다.

    interval_minutes 를 주지 않으면 **자료에서 기록 간격을 자동 추정**한다
    (10분 로거뿐 아니라 1·5·15·30·60분 자료도 그대로 처리).

    반환: (격자 정합 DataFrame(qc_status 포함), 결측구간 요약)
    """
    if df.empty:
        return df.assign(qc_status="original"), pd.DataFrame()

    if interval_minutes is None:
        interval_minutes = detect_interval_minutes(df["timestamp"])
    freq = f"{int(round(interval_minutes * 60))}s"
    full = pd.date_range(df["timestamp"].min(), df["timestamp"].max(), freq=freq)
    base = df.set_index("timestamp").reindex(full)
    base.index.name = "timestamp"
    present = df.set_index("timestamp").index
    inserted = ~full.isin(present)
    base["qc_status"] = np.where(inserted, "missing_timestamp_inserted", "original")
    out = base.reset_index()

    # 연속 결측 구간(run) 요약
    runs, start = [], None
    for i, flag in enumerate(inserted):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            runs.append((full[start], full[i - 1]))
            start = None
    if start is not None:
        runs.append((full[start], full[-1]))

    gap = pd.DataFrame(runs, columns=["start", "end"])
    if not gap.empty:
        gap["결측개수"] = ((gap["end"] - gap["start"]).dt.total_seconds() / (interval_minutes * 60) + 1).astype(int)
        gap["결측시간_분"] = gap["결측개수"] * interval_minutes
        gap = gap.sort_values("결측개수", ascending=False).reset_index(drop=True)
    return out, gap
