"""매주 들어오는 자료를 쌓아 두는 보관함.

매주 새 파일을 올릴 때마다 처음부터 다시 하지 않아도 되도록,
두 가지를 함께 남긴다.

  1) **원본 보관** `outputs/archive/uploads/` — 올린 파일을 그대로 복사해 둔다.
     내용이 같은 파일은 다시 넣지 않는다(같은 주에 두 번 올려도 중복되지 않음).
     원본이 남아 있으므로 나중에 규칙이 바뀌면 전부 다시 계산할 수 있다.
  2) **누적 마스터** `outputs/archive/env_master.csv` — 새 자료가 기존 자료
     **뒤에 이어붙는다**. 같은 구역·같은 시각이면 한 행으로 합쳐지므로
     겹치는 기간을 올려도 늘어나지 않는다(archive.build_archive 가 처리).

전처리 결과는 회차별로 `outputs/results/<날짜>/` 에 통째로 남겨 두어
지난주 결과를 그대로 다시 내려받을 수 있게 한다.
"""

from __future__ import annotations

import hashlib
import io
import re
import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import archive

UPLOAD_DIR = "uploads"
UPLOAD_LOG = "upload_log.csv"
RESULT_INDEX = "index.csv"

UPLOAD_LOG_COLS = ["올린날짜", "파일명", "저장이름", "크기(KB)", "내용지문"]


# ---------------------------------------------------------------------
# 원본 파일 보관
# ---------------------------------------------------------------------
def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _read_source(src) -> tuple[str, bytes]:
    """(버퍼, 파일명) 또는 경로를 (파일명, 내용) 으로 통일한다."""
    if isinstance(src, tuple):
        buf, name = src
        data = buf.getvalue() if hasattr(buf, "getvalue") else bytes(buf)
        return str(name), data
    p = Path(src)
    return p.name, p.read_bytes()


def load_upload_log(store_dir: str | Path) -> pd.DataFrame:
    path = Path(store_dir) / UPLOAD_LOG
    if not path.exists():
        return pd.DataFrame(columns=UPLOAD_LOG_COLS)
    df = pd.read_csv(path, dtype={"내용지문": str})
    for c in UPLOAD_LOG_COLS:
        if c not in df.columns:
            df[c] = ""
    return df[UPLOAD_LOG_COLS]


def save_uploads(sources: list, store_dir: str | Path, when: datetime | None = None) -> pd.DataFrame:
    """올린 파일을 보관함에 복사한다. 이미 같은 내용이 있으면 건너뛴다.

    반환: 파일별 처리 결과(파일명·상태·저장경로). 상태는 '새로 보관'/'이미 있음'.
    """
    store = Path(store_dir)
    updir = store / UPLOAD_DIR
    updir.mkdir(parents=True, exist_ok=True)
    log = load_upload_log(store)
    known = set(log["내용지문"].astype(str))
    stamp = (when or datetime.now())

    rows, added = [], []
    for src in sources:
        name, data = _read_source(src)
        fp = _digest(data)
        if fp in known:
            rows.append({"파일명": name, "상태": "이미 있음", "저장경로": "",
                         "크기(KB)": round(len(data) / 1024, 1)})
            continue
        safe = re.sub(r"[^\w가-힣.\-]+", "_", name)
        target = updir / f"{stamp:%Y%m%d}_{fp[:6]}_{safe}"
        target.write_bytes(data)
        known.add(fp)
        added.append(str(target))
        rows.append({"파일명": name, "상태": "새로 보관", "저장경로": str(target),
                     "크기(KB)": round(len(data) / 1024, 1)})
        log = pd.concat([log, pd.DataFrame([{
            "올린날짜": f"{stamp:%Y-%m-%d %H:%M}", "파일명": name,
            "저장이름": target.name, "크기(KB)": round(len(data) / 1024, 1),
            "내용지문": fp}])], ignore_index=True)

    log.to_csv(store / UPLOAD_LOG, index=False, encoding="utf-8-sig")
    result = pd.DataFrame(rows)
    result.attrs["added_paths"] = added
    return result


def stored_files(store_dir: str | Path) -> list[str]:
    """보관함에 들어 있는 원본 파일 경로 전부(오래된 것부터)."""
    updir = Path(store_dir) / UPLOAD_DIR
    if not updir.exists():
        return []
    return [str(p) for p in sorted(updir.iterdir()) if p.is_file()]


# ---------------------------------------------------------------------
# 보관 + 누적 통합을 한 번에
# ---------------------------------------------------------------------
def add_files(sources: list, cfg: dict, store_dir: str | Path,
              replicate: str = "first", registry_path: str | Path | None = None,
              rebuild: bool = False) -> dict:
    """올린 파일을 보관하고 누적 마스터에 이어붙인다.

    rebuild=True 면 보관된 원본 전부를 처음부터 다시 통합한다
    (규칙이 바뀌었거나 구역 이름을 새로 지정했을 때).

    반환 dict: uploads(파일별 처리) + archive.build_archive 결과 + before/after 행수
    """
    store = Path(store_dir)
    store.mkdir(parents=True, exist_ok=True)

    before = 0
    master_path = store / archive.MASTER_NAME
    if master_path.exists():
        before = sum(1 for _ in open(master_path, encoding="utf-8-sig")) - 1

    uploads = save_uploads(sources, store)
    new_paths = list(uploads.attrs.get("added_paths", []))

    if rebuild:
        targets, update = stored_files(store), False
    else:
        targets, update = new_paths, True

    if not targets:
        # 새로 들어온 것이 없다 — 기존 마스터를 그대로 돌려준다
        master = archive.load_master(store) if master_path.exists() else pd.DataFrame()
        return {"uploads": uploads, "master": master, "clean": pd.DataFrame(),
                "summary": pd.read_csv(store / archive.SUMMARY_NAME) if (store / archive.SUMMARY_NAME).exists() else pd.DataFrame(),
                "files": pd.DataFrame(), "log": ["새로 들어온 파일이 없습니다(이미 보관된 자료)."],
                "registry": {}, "range_report": pd.DataFrame(),
                "collisions": pd.DataFrame(),
                "before": before, "after": before, "added": 0, "out_dir": store}

    res = archive.build_archive(targets, cfg, store, replicate=replicate,
                                update=update, registry_path=registry_path)
    res["uploads"] = uploads
    res["before"] = before
    res["after"] = len(res["master"])
    res["added"] = res["after"] - before
    return res


# ---------------------------------------------------------------------
# 회차별 결과 보관
# ---------------------------------------------------------------------
def _index_path(results_dir: str | Path) -> Path:
    return Path(results_dir) / RESULT_INDEX


def list_results(results_dir: str | Path) -> pd.DataFrame:
    """보관된 결과 회차 목록(최근 것부터)."""
    path = _index_path(results_dir)
    if not path.exists():
        return pd.DataFrame(columns=["저장시각", "이름", "폴더", "구간수", "메모"])
    df = pd.read_csv(path)
    return df.iloc[::-1].reset_index(drop=True)


def save_result(tables: dict[str, pd.DataFrame], results_dir: str | Path,
                label: str = "", memo: str = "", when: datetime | None = None) -> Path:
    """이번에 만든 표들을 회차 폴더에 통째로 저장한다.

    tables: {"하루별요약": df, "구간정의": df, "구간환경": df, "생육_환경": df}
    """
    results = Path(results_dir)
    results.mkdir(parents=True, exist_ok=True)
    stamp = when or datetime.now()
    tag = re.sub(r"[^\w가-힣.\-]+", "_", str(label)).strip("_")
    base = f"{stamp:%Y-%m-%d}" + (f"_{tag}" if tag else "")
    folder = results / base
    n = 2
    while folder.exists():
        folder = results / f"{base}({n})"
        n += 1
    folder.mkdir(parents=True)

    for name, df in tables.items():
        if df is None or getattr(df, "empty", True):
            continue
        df.to_csv(folder / f"{name}.csv", index=False, encoding="utf-8-sig")

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        for name, df in tables.items():
            if df is None or getattr(df, "empty", True):
                continue
            df.to_excel(w, sheet_name=str(name)[:31], index=False)
    (folder / "전체결과.xlsx").write_bytes(buf.getvalue())

    iv = tables.get("구간환경")
    row = {"저장시각": f"{stamp:%Y-%m-%d %H:%M}", "이름": folder.name,
           "폴더": str(folder), "구간수": (len(iv) if iv is not None and not iv.empty else 0),
           "메모": memo}
    idx = _index_path(results)
    prev = pd.read_csv(idx) if idx.exists() else pd.DataFrame()
    pd.concat([prev, pd.DataFrame([row])], ignore_index=True).to_csv(
        idx, index=False, encoding="utf-8-sig")
    return folder


def register_result(folder: str | Path, results_dir: str | Path, memo: str = "",
                    n_intervals: int = 0, when: datetime | None = None) -> None:
    """이미 만들어진 결과 폴더를 회차 목록에 등록한다(CLI 에서 사용)."""
    stamp = when or datetime.now()
    idx = _index_path(results_dir)
    prev = pd.read_csv(idx) if idx.exists() else pd.DataFrame()
    row = {"저장시각": f"{stamp:%Y-%m-%d %H:%M}", "이름": Path(folder).name,
           "폴더": str(folder), "구간수": int(n_intervals), "메모": memo}
    if not prev.empty and "폴더" in prev.columns:
        prev = prev[prev["폴더"].astype(str) != str(folder)]
    pd.concat([prev, pd.DataFrame([row])], ignore_index=True).to_csv(
        idx, index=False, encoding="utf-8-sig")


def result_files(folder: str | Path) -> list[Path]:
    p = Path(folder)
    return sorted(p.iterdir()) if p.exists() else []


def zip_result(folder: str | Path) -> bytes:
    """회차 폴더를 통째로 압축해 내려받을 수 있게 만든다."""
    folder = Path(folder)
    tmp = folder.parent / f".{folder.name}"
    shutil.make_archive(str(tmp), "zip", root_dir=folder)
    data = Path(f"{tmp}.zip").read_bytes()
    Path(f"{tmp}.zip").unlink(missing_ok=True)
    return data
