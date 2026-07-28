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

## 참고: 갈래(브랜치)란

저장소 안에는 여러 갈래가 있고, 기본 갈래는 `main` 입니다.
작업 중인 내용은 따로 갈래를 만들어 두었다가 확인이 끝나면 `main` 으로 합칩니다.
지금까지의 작업은 이미 `main` 에 합쳐졌으므로 갈래를 고를 필요가 없습니다.
