"""전처리 결과·설정을 GitHub 레포지토리에 자동 저장(커밋)한다.

왜 필요한가
-----------
현장에서 만든 결과 파일은 대개 담당자 PC 에만 남는다. 담당자가 바뀌거나 PC 가
바뀌면 "지난 시즌 자료가 어디 있더라" 가 반복된다. 매주 만든 결과를 레포에
같은 경로로 덮어쓰며 커밋해 두면, 파일 이력이 그대로 남아
**언제 어떤 값이 바뀌었는지**를 나중에 되짚을 수 있다.

준비물
------
1) GitHub Personal Access Token (fine-grained 권장, 해당 레포 Contents: Read/Write)
2) 환경변수 등록 — 토큰은 절대 코드·설정파일에 쓰지 않는다.
       export GITHUB_TOKEN=ghp_xxxxxxxx
       export GITHUB_REPO=gangster270/data_qc      # config 에 적어도 됨
3) `pip install PyGithub`

사용
----
    from src import github_sync
    res = github_sync.push_files(
        {"outputs/merged_env_growth.csv": csv_bytes},
        cfg, message="2026-07-29 주간 결과")
    print(res["log"])
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pandas as pd

TOKEN_ENVS = ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT")


# ---------------------------------------------------------------------
# 설정 확인
# ---------------------------------------------------------------------
def get_token() -> str:
    """환경변수에서 토큰을 찾는다(여러 이름을 순서대로 시도)."""
    for name in TOKEN_ENVS:
        v = os.environ.get(name, "").strip()
        if v:
            return v
    return ""


def get_repo_name(cfg: dict | None = None) -> str:
    """`owner/repo` 를 환경변수 → 설정파일 순으로 찾는다."""
    v = os.environ.get("GITHUB_REPO", "").strip()
    if v:
        return v
    return str(((cfg or {}).get("github") or {}).get("repo", "") or "").strip()


def status(cfg: dict | None = None) -> dict:
    """지금 GitHub 저장이 가능한 상태인지 점검한다(대시보드 표시용)."""
    gcfg = (cfg or {}).get("github") or {}
    try:
        import github  # noqa: F401  (PyGithub)
        has_lib = True
    except Exception:
        has_lib = False
    repo = get_repo_name(cfg)
    token = get_token()
    ready = bool(has_lib and repo and token)
    if not has_lib:
        reason = "PyGithub 미설치 — `pip install PyGithub`"
    elif not token:
        reason = "토큰 없음 — 환경변수 GITHUB_TOKEN 을 설정하세요"
    elif not repo:
        reason = "레포 미지정 — config/qc_config.yaml 의 github.repo 또는 GITHUB_REPO"
    else:
        reason = "저장 준비 완료"
    return {
        "ready": ready, "reason": reason, "repo": repo,
        "branch": str(gcfg.get("branch", "main") or "main"),
        "base_dir": str(gcfg.get("base_dir", "outputs") or "outputs"),
        "has_library": has_lib, "has_token": bool(token),
        "enabled": bool(gcfg.get("enabled", False)),
    }


def test_connection(cfg: dict | None = None) -> tuple[bool, str]:
    """토큰·레포·권한을 실제로 한 번 확인한다."""
    st = status(cfg)
    if not st["ready"]:
        return False, st["reason"]
    try:
        from github import Github
        gh = Github(get_token())
        repo = gh.get_repo(st["repo"])
        perms = getattr(repo, "permissions", None)
        can_write = bool(getattr(perms, "push", False)) if perms else True
        who = gh.get_user().login
        msg = f"{who} → {repo.full_name} ({st['branch']} 브랜치) 연결 성공"
        if not can_write:
            return False, msg + " — 다만 쓰기 권한이 없습니다(토큰 권한 확인)"
        return True, msg
    except Exception as e:
        return False, f"연결 실패: {type(e).__name__} — {e}"


# ---------------------------------------------------------------------
# 저장(커밋)
# ---------------------------------------------------------------------
def _to_bytes(value) -> bytes:
    """DataFrame / str / bytes / 파일경로를 모두 bytes 로 맞춘다."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, pd.DataFrame):
        return value.to_csv(index=False).encode("utf-8-sig")   # 엑셀에서 한글 안 깨지게
    if isinstance(value, Path):
        return value.read_bytes()
    if isinstance(value, str):
        p = Path(value)
        if p.exists() and p.is_file():
            return p.read_bytes()
        return value.encode("utf-8")
    raise TypeError(f"지원하지 않는 형식: {type(value)}")


def push_files(files: dict, cfg: dict | None = None, message: str | None = None,
               base_dir: str | None = None, branch: str | None = None,
               dry_run: bool = False) -> dict:
    """파일 여러 개를 한 번에 커밋한다.

    files : {"레포_안_경로": DataFrame | bytes | str | Path}
            경로가 상대경로면 base_dir 아래로 들어간다.
    반환  : {"ok", "n_pushed", "commits":[{path, action, url}], "log":[...]}

    같은 경로에 파일이 이미 있으면 **덮어쓰되 이력은 남는다**(update_file).
    커밋 하나가 실패해도 나머지는 계속 진행하고, 실패 내역을 log 에 남긴다.
    """
    st = status(cfg)
    result = {"ok": False, "n_pushed": 0, "commits": [], "log": []}
    if not st["ready"]:
        result["log"].append(st["reason"])
        return result

    base = (base_dir if base_dir is not None else st["base_dir"]).strip("/")
    branch = branch or st["branch"]
    prefix = str(((cfg or {}).get("github") or {}).get("commit_prefix", "[자동] 환경데이터"))
    msg = message or f"{datetime.now():%Y-%m-%d %H:%M} 결과 저장"
    commit_msg = f"{prefix} {msg}".strip()

    if dry_run:
        result["log"] = [f"(모의) {st['repo']}@{branch} 에 "
                         f"{'/'.join(x for x in (base, p) if x)} 저장 예정" for p in files]
        result["ok"] = True
        return result

    try:
        from github import Github
        from github.GithubException import GithubException
    except Exception as e:      # pragma: no cover - status() 에서 이미 걸러짐
        result["log"].append(f"PyGithub 로드 실패: {e}")
        return result

    try:
        repo = Github(get_token()).get_repo(st["repo"])
    except Exception as e:
        result["log"].append(f"레포 접근 실패: {type(e).__name__} — {e}")
        return result

    for rel_path, value in files.items():
        path = rel_path if rel_path.startswith("/") else "/".join(x for x in (base, rel_path) if x)
        path = path.lstrip("/")
        try:
            content = _to_bytes(value)
        except TypeError as e:
            result["log"].append(f"❌ {path}: {e}")
            continue
        try:
            try:
                # 이미 있으면 SHA 를 받아 update — 이력이 이어진다.
                existing = repo.get_contents(path, ref=branch)
                sha = existing.sha if not isinstance(existing, list) else None
            except GithubException:
                sha = None

            if sha:
                out = repo.update_file(path, commit_msg, content, sha, branch=branch)
                action = "수정"
            else:
                out = repo.create_file(path, commit_msg, content, branch=branch)
                action = "신규"
            commit = out.get("commit") if isinstance(out, dict) else None
            result["commits"].append({
                "path": path, "action": action,
                "sha": getattr(commit, "sha", "")[:7] if commit else "",
                "url": getattr(commit, "html_url", ""),
            })
            result["n_pushed"] += 1
            result["log"].append(f"✅ {action} — {path}")
        except Exception as e:
            result["log"].append(f"❌ {path}: {type(e).__name__} — {e}")

    result["ok"] = result["n_pushed"] > 0
    if result["ok"]:
        result["log"].append(
            f"{st['repo']} @{branch} 에 {result['n_pushed']}개 저장했습니다.")
    return result


def push_result_bundle(tables: dict, cfg: dict, folder: str | None = None,
                       message: str | None = None, extra: dict | None = None) -> dict:
    """전처리 결과 묶음(일별·구간별·병합표)을 회차 폴더에 한 번에 저장한다.

    folder 를 주지 않으면 오늘 날짜로 폴더를 만든다 → 회차별 기록이 쌓인다.
        outputs/2026-07-29/merged_env_growth.csv
        outputs/2026-07-29/daily_env_summary.csv
    설정 스냅샷(config/qc_config.yaml)도 함께 넣어야 **그때 어떤 기준으로
    계산했는지**를 나중에 알 수 있다.
    """
    folder = folder or f"{datetime.now():%Y-%m-%d}"
    payload = {}
    for name, df in (tables or {}).items():
        if isinstance(df, pd.DataFrame) and df.empty:
            continue
        fname = name if name.endswith((".csv", ".xlsx", ".json", ".md", ".yaml")) else f"{name}.csv"
        payload[f"{folder}/{fname}"] = df
    for name, value in (extra or {}).items():
        payload[f"{folder}/{name}"] = value

    # 설정 스냅샷 — 재현성의 핵심
    cfg_path = Path(cfg.get("_path", "")) if cfg else None
    if cfg_path and cfg_path.exists():
        payload[f"{folder}/qc_config_snapshot.yaml"] = cfg_path.read_bytes()

    if not payload:
        return {"ok": False, "n_pushed": 0, "commits": [], "log": ["저장할 결과가 없습니다."]}
    return push_files(payload, cfg, message=message or f"{folder} 결과 저장")
