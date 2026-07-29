# 코드를 내 컴퓨터로 가져오기 (GitHub 처음이신 분용)

화면이 예전 그대로라면 **코드가 아직 내 컴퓨터에 안 온 것**입니다. 고장이 아닙니다.

지금은 모든 작업이 기본 갈래(`main`)에 들어가 있으므로,
**아무것도 고르지 않고 그냥 받으면 최신입니다.**

**제대로 받았는지 확인하는 법** — 대시보드를 열었을 때 위쪽 탭이
`2️⃣ 상태 점검 · 3️⃣ 결과 만들기 · 📦 쌓인 자료 · 🔬 센서 점검 · ⚙️ 설정`
이면 최신입니다. `📊 모니터링 · 🔁 전처리` 로 보이면 예전 것입니다.

---

## 🍎 맥(macOS)에서 설치·실행 — 이대로 한 줄씩

맥에서 가장 많이 막히는 세 곳을 미리 피하는 순서입니다.

1. **`pip` 라는 명령은 맥에 없다** → `python3 -m pip` 또는 아래처럼 전용 공간을 만들어 쓴다
2. **`Library/Mobile Documents` 처럼 경로에 띄어쓰기가 있으면 명령이 끊긴다**
3. **iCloud 폴더(`com~apple~CloudDocs`)에서는 돌리면 안 된다** — 안 쓰는 파일을 자동으로
   내려버려서 실행 중에 파일이 사라진 것처럼 된다. **홈 폴더로 옮긴다.**

```bash
# 1) 파이썬이 있는지 확인 — 'Python 3.x.x' 가 나오면 통과
python3 --version
#    없다고 나오면 https://www.python.org/downloads/ 에서 설치 후 터미널을 껐다 켠다

# 2) 받은 폴더를 iCloud 밖(홈)으로 옮긴다
mv ~/Library/Mobile\ Documents/com~apple~CloudDocs/data_qc-main ~/data_qc
cd ~/data_qc

# 3) 이 프로그램 전용 공간을 만들고 켠다 (처음 한 번만)
python3 -m venv .venv
source .venv/bin/activate
#    줄 맨 앞에 (.venv) 가 붙으면 성공 — 이때부터 pip·streamlit 이 그냥 된다

# 4) 설치 (처음 한 번만, 몇 분 걸린다)
pip install -r requirements.txt

# 5) 실행
streamlit run app/streamlit_app.py
```

**다음부터 켤 때는 이 세 줄만:**

```bash
cd ~/data_qc
source .venv/bin/activate
streamlit run app/streamlit_app.py
```

끄려면 터미널에서 **Control + C**.

| 맥에서 나는 오류 | 뜻·해결 |
|---|---|
| `zsh: command not found: pip` | 3번(전용 공간 만들기)을 먼저 한다. 또는 `python3 -m pip` 로 부른다 |
| `zsh: command not found: streamlit` | 아직 설치 전이다. 4번을 실행한다 |
| `no such file or directory: /Users/.../Mobile` | 경로의 띄어쓰기에서 끊긴 것. 위 2번처럼 `Mobile\ Documents` 로 쓰거나 경로를 `"..."` 로 감싼다 |
| `externally-managed-environment` | 전용 공간(`.venv`) 없이 설치하려 해서 나는 것. 3번을 먼저 한다 |
| `zsh: command not found: python3` | 파이썬 미설치. python.org 에서 설치 후 터미널 재시작 |

---

## 방법 A. 압축파일로 받기 (가장 쉬움 · git 몰라도 됨)

1. 브라우저에서 <https://github.com/gangster270/data_qc> 접속
2. 오른쪽 초록색 **`< > Code`** 단추 → **`Download ZIP`**
3. 받은 zip 을 풀고, 그 폴더에서 실행

맥이라면 위 **🍎 맥에서 설치·실행** 순서를 그대로 따르시면 됩니다.
윈도우는 압축을 푼 폴더에서:

```cmd
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
streamlit run app/streamlit_app.py
```

> 단점: 다음에 또 바뀌면 처음부터 다시 받아야 하고, `outputs/` 에 쌓아둔
> 자료가 새 폴더에는 없습니다. **자료를 계속 쌓아 쓰시려면 방법 B 를 권합니다.**

---

## 방법 B. git 으로 받기 (한 번만 해두면 다음부터 명령 한 줄)

git 이 없으면 먼저 설치합니다 — <https://git-scm.com/downloads>
(Windows 는 설치 후 **Git Bash** 를 열어 아래를 입력)

### 처음 한 번

```bash
git clone https://github.com/gangster270/data_qc.git
cd data_qc
python3 -m venv .venv && source .venv/bin/activate    # 맥·리눅스
pip install -r requirements.txt
streamlit run app/streamlit_app.py
```

> 홈 폴더처럼 **iCloud 밖**, 그리고 **띄어쓰기 없는 경로**에 받으세요.

### 이미 `data_qc` 폴더가 있다면

```bash
cd data_qc
git checkout main        # 다른 갈래를 보고 있었다면
git pull
```

### 다음부터 새 작업이 올라올 때마다

```bash
cd data_qc
git pull
source .venv/bin/activate        # 맥·리눅스
streamlit run app/streamlit_app.py
```

`git pull` 은 "바뀐 것만 받아오기"입니다. `outputs/` 에 쌓아둔 자료와
`config/` 에 적어둔 구역 이름은 지워지지 않고 그대로 남습니다.

---

## 자주 막히는 곳

| 증상 | 해결 |
|---|---|
| `git: command not found` | git 미설치 → <https://git-scm.com/downloads> |
| `python: command not found` | 파이썬 미설치 → <https://www.python.org/downloads/> (설치 때 *Add to PATH* 체크) |
| `streamlit: command not found` | `.venv` 를 켰는지(`source .venv/bin/activate`) 확인하고 `pip install -r requirements.txt` 실행 |
| 화면이 그대로 | 브라우저 **F5**. 그래도 그대로면 터미널에서 **Ctrl+C** 후 다시 `streamlit run ...` |
| 지금 어느 갈래인지 모르겠음 | `git branch --show-current` → `main` 이면 최신 |
| `Your local changes would be overwritten` | 내 컴퓨터에서 코드를 고친 상태입니다. `git stash` 입력 후 다시 `git pull` |
| 포트가 이미 쓰이는 중 | `streamlit run app/streamlit_app.py --server.port 8502` |

---

## 받은 다음 할 일

1. `streamlit run app/streamlit_app.py` 로 화면 열기
2. **⚙️ 설정** 탭에서 로거 번호마다 **구역 이름** 지정 (한 번만 하면 계속 기억)
3. **📦 쌓인 자료** 탭에서 지금까지 받은 파일을 전부 넣어 두기
4. 다음 주부터는 새로 받은 파일만 올리고 **📦 보관함에 쌓기**
   — 또는 터미널에서 `python scripts/weekly_update.py --env "data/신규/*"`

---

## 결과를 GitHub 에 자동으로 올려두기 (선택)

여기까지는 **코드를 내려받는** 방법이었고, 이번에는 반대로
**내가 만든 결과를 GitHub 에 올려서 쌓아 두는** 기능입니다.

매주 만든 결과가 담당자 컴퓨터에만 있으면, 담당자나 컴퓨터가 바뀔 때 사라집니다.
같은 저장소에 계속 올려 두면 **언제 어떤 값이 바뀌었는지**가 기록으로 남습니다.

### 1) 열쇠(토큰) 만들기 — 한 번만

1. GitHub 로그인 → 오른쪽 위 프로필 → **Settings**
2. 왼쪽 맨 아래 **Developer settings**
3. **Personal access tokens** → **Fine-grained tokens** → **Generate new token**
4. 아래 두 가지를 꼭 맞춥니다.
   - **Repository access** : `Only select repositories` → `data_qc` 선택
   - **Permissions** → **Repository permissions** → **Contents** 를 `Read and write` 로
5. 맨 아래 **Generate token** → 나온 문자열(`github_pat_...`)을 복사합니다.

> **이 문자열은 비밀번호와 같습니다.** 창을 닫으면 다시 볼 수 없으니 그 자리에서 등록하세요.
> 메모장이나 코드 파일에 적어 두면 안 됩니다 — 그대로 GitHub 에 올라가면 남이 씁니다.

### 2) 컴퓨터에 등록하기

맥·리눅스 (터미널):

```bash
export GITHUB_TOKEN="복사한_github_pat_..."
export GITHUB_REPO="gangster270/data_qc"
streamlit run app/streamlit_app.py
```

윈도우 (PowerShell):

```powershell
$env:GITHUB_TOKEN="복사한_github_pat_..."
$env:GITHUB_REPO="gangster270/data_qc"
streamlit run app/streamlit_app.py
```

터미널을 껐다 켜면 다시 넣어야 합니다. 매번 넣기 번거로우면 맥에서는
`~/.zshrc` 파일 맨 아래에 위 `export` 두 줄을 적어 두면 됩니다.

`pip install -r requirements.txt` 를 이미 했다면 필요한 `PyGithub` 도 함께 깔려 있습니다.

### 3) 확인하고 올리기

1. 대시보드 **⚙️ 설정** 탭 맨 아래 **☁️ GitHub 자동 저장** 에서 **🔌 연결 확인**
   → *"○○ → gangster270/data_qc (main 브랜치) 연결 성공"* 이 나오면 준비 완료
2. **3️⃣ 결과 만들기** 탭 → **☁️ GitHub 에도 올려두기** → **올리기**

올라가는 모습:

```
outputs/2026-07-29/daily_env_summary.csv       하루별 요약
outputs/2026-07-29/env_interval_summary.csv    구간별 환경
outputs/2026-07-29/merged_env_growth.csv       생육 + 환경 최종 표
outputs/2026-07-29/qc_config_snapshot.yaml     그때 쓴 기준값
```

마지막 `qc_config_snapshot.yaml` 은 **그 결과를 어떤 기준으로 계산했는지**를 남기는 파일입니다.
나중에 값이 달라 보일 때 기준이 바뀐 것인지 자료가 바뀐 것인지 구분할 수 있습니다.

| 이런 말이 나오면 | 뜻·해결 |
|---|---|
| `PyGithub 미설치` | `pip install PyGithub` |
| `토큰 없음` | 위 2) 를 하지 않았거나 터미널을 새로 켰습니다 |
| `연결 실패: BadCredentials` | 토큰을 잘못 붙여넣었거나 만료됐습니다 — 새로 만드세요 |
| `쓰기 권한이 없습니다` | 토큰 만들 때 **Contents: Read and write** 를 안 준 경우 |
| `레포 미지정` | `GITHUB_REPO` 또는 `config/qc_config.yaml` 의 `github.repo` 확인 |

---

## 참고: 갈래(브랜치)란

저장소 안에는 여러 갈래가 있고, 기본 갈래는 `main` 입니다.
작업 중인 내용은 따로 갈래를 만들어 두었다가 확인이 끝나면 `main` 으로 합칩니다.
지금까지의 작업은 이미 `main` 에 합쳐졌으므로 갈래를 고를 필요가 없습니다.
