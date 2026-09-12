# TAKURO Collector

Windows용 임대 매물 수집기입니다. 가져온 배포 기준선은 **0.4.0**, 현재 개발 버전은 **0.4.1**입니다.

## 개발 환경

Python 3.12를 권장합니다.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe main.py
```

Playwright 브라우저가 필요한 사이트는 최초 1회 다음 명령도 실행합니다.

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

## 민감정보와 수집 자료

로그인 정보, WordPress 연동 키, 쿠키/브라우저 프로필, 로컬 DB, 로그, 사진, PDF, CSV, ZIP과 내보내기 결과는 Git에 넣지 않습니다. 앱은 기본적으로 이들을 저장소 밖의 사용자 데이터 폴더에 저장합니다. `TAKURO_COLLECTOR_HOME`을 사용할 때도 저장소 내부 경로를 지정하지 마세요.

테스트의 `tests/fixtures/*.html`은 KIN 파서 회귀를 재현하기 위한 최소 고정 입력이며 실제 운영 수집 결과가 아닙니다.

새 관리회사 추가 및 실제 검증 순서는 [docs/ADDING_MANAGEMENT_COMPANY.md](docs/ADDING_MANAGEMENT_COMPANY.md)를 따릅니다. 현재 기준선과 배포물 식별값은 [docs/BASELINE.md](docs/BASELINE.md)에 기록되어 있습니다.

## 비공개 원격 저장소 연결

GitHub/GitLab 등에서 **비공개 빈 저장소**를 만든 뒤 아래처럼 연결합니다.

```powershell
git remote add origin <PRIVATE_REPOSITORY_URL>
git push -u origin main
```

푸시 전에는 반드시 다음 검사를 통과시킵니다.

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check-repository.ps1
```
