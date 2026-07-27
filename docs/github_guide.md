# 바뀐 코드를 내 컴퓨터로 가져오기 (GitHub 처음이신 분용)

화면이 그대로라면 **코드가 아직 내 컴퓨터에 안 온 것**입니다. 고장이 아닙니다.

## 왜 이런 일이 생기나

GitHub 저장소 안에는 여러 갈래(**브랜치**)가 있습니다.

```
main                                  ← 기본 갈래. 아무것도 안 하면 여기를 봅니다
claude/environmental-data-...-pyr7ro  ← 새로 만든 것이 전부 여기 있습니다
```

새 작업은 **`claude/environmental-data-alignment-monitoring-pyr7ro`** 갈래에 있습니다.
`main` 만 보고 있으면 예전 화면이 그대로 보입니다.

**제대로 받았는지 확인하는 법** — 대시보드를 열었을 때 위쪽 탭이
`2️⃣ 상태 점검 · 3️⃣ 결과 만들기 · 📦 쌓인 자료 · 🔬 센서 점검 · ⚙️ 설정`
이면 최신입니다. `📊 모니터링 · 🔁 전처리` 로 보이면 예전 것입니다.

---

## 방법 A. 압축파일로 받기 (가장 쉬움 · git 몰라도 됨)

1. 브라우저에서 <https://github.com/gangster270/data_qc> 접속
2. 파일 목록 **왼쪽 위**의 갈래 선택 단추(`main` 이라고 쓰인 곳)를 누른다
3. 목록에서 **`claude/environmental-data-alignment-monitoring-pyr7ro`** 선택
   - 주소창이 `.../tree/claude/environmental-data-alignment-monitoring-pyr7ro` 로 바뀌면 맞습니다
4. 오른쪽 초록색 **`< > Code`** 단추 → **`Download ZIP`**
5. 받은 zip 을 풀고, 그 폴더에서 실행

```bash
cd 압축을푼폴더
pip install -r requirements.txt
streamlit run app/streamlit_app.py
```

> 단점: 다음에 또 바뀌면 다시 받아야 하고, `outputs/` 에 쌓아둔 자료가
> 새 폴더에는 없습니다. **쌓인 자료를 계속 쓰려면 방법 B 를 권합니다.**

---

## 방법 B. git 으로 받기 (한 번만 해두면 다음부터 명령 두 줄)

git 이 없으면 먼저 설치합니다 — <https://git-scm.com/downloads>
(Windows 는 설치 후 **Git Bash** 를 열어서 아래를 입력)

### 처음 한 번

```bash
git clone https://github.com/gangster270/data_qc.git
cd data_qc
git checkout claude/environmental-data-alignment-monitoring-pyr7ro
pip install -r requirements.txt
```

### 이미 폴더가 있다면

```bash
cd data_qc
git fetch origin
git checkout claude/environmental-data-alignment-monitoring-pyr7ro
git pull
```

### 다음부터 새 작업이 올라올 때마다

```bash
cd data_qc
git pull
streamlit run app/streamlit_app.py
```

`git pull` 은 "바뀐 것만 받아오기"입니다. `outputs/` 에 쌓아둔 자료와
`config/` 에 적어둔 구역 이름은 그대로 남습니다.

---

## 방법 C. `main` 으로 합치기 (앞으로 갈래를 신경 쓰기 싫다면)

<https://github.com/gangster270/data_qc/pull/1> 을 열고 초록색
**Merge pull request** → **Confirm merge** 를 누르면 지금까지의 작업이
`main` 으로 들어갑니다. 그러면 갈래 이름을 몰라도 됩니다.

```bash
git clone https://github.com/gangster270/data_qc.git   # 처음이면
cd data_qc
git pull                                                # 이미 있으면
```

---

## 자주 막히는 곳

| 증상 | 해결 |
|---|---|
| `git: command not found` | git 미설치 → <https://git-scm.com/downloads> |
| `python: command not found` | 파이썬 미설치 → <https://www.python.org/downloads/> (설치 때 *Add to PATH* 체크) |
| `streamlit: command not found` | `pip install -r requirements.txt` 를 먼저 실행 |
| 화면이 그대로 | 브라우저 **F5**. 그래도 그대로면 터미널에서 **Ctrl+C** 후 다시 `streamlit run ...` |
| 지금 어느 갈래인지 모르겠음 | `git branch --show-current` 을 입력하면 알려줍니다 |
| `Your local changes would be overwritten` | 내 컴퓨터에서 코드를 고친 상태입니다. `git stash` 입력 후 다시 `git pull` |

---

## 받은 다음 할 일

1. `streamlit run app/streamlit_app.py` 로 화면 열기
2. **⚙️ 설정** 탭에서 로거 번호마다 **구역 이름** 지정 (한 번만 하면 계속 기억)
3. **📦 쌓인 자료** 탭에서 지금까지 받은 파일을 전부 넣어 두기
4. 다음 주부터는 새로 받은 파일만 올리고 **📦 보관함에 쌓기**
   — 또는 터미널에서 `python scripts/weekly_update.py --env "data/신규/*"`
