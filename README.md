# TAKURO Collector

Windows용 임대 매물 수집기입니다. 가져온 배포 기준선은 **0.4.0**, 현재 개발 버전은 **0.4.5**입니다.

새 관리회사는 우선 `config/managed-sites.example.json` 형식으로 작성한 뒤 설정 → 수집 사이트 → `관리회사 설정 파일 가져오기`로 등록합니다. KIN과 AMB처럼 특수한 사이트만 전용 어댑터를 유지합니다. 모든 어댑터의 최종 결과는 홈페이지 필수 매물 정보 계약으로 검증됩니다.

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

## 업데이트

설정 → 일반의 `업데이트 정보 URL`에 HTTPS JSON 매니페스트 주소를 저장하면 상단의 `업데이트 확인` 버튼을 사용할 수 있습니다. 사용자가 승인한 경우에만 서명과 SHA-256을 검증하고, 별도 helper가 프로그램 폴더를 교체한 뒤 health marker를 확인합니다. 시작 확인에 실패하면 known-good 백업으로 복구합니다. 사용자 DB·설정·브라우저 프로필·수집 자료는 프로그램 폴더 밖에 유지됩니다.

매니페스트 형식은 `config/update-manifest.example.json`을 참고하세요. 운영 HTTPS 호스팅과 Ed25519 공개키를 확정하기 전에는 실제 release를 게시하지 않습니다. signing private key는 저장소나 배포 파일에 포함하지 않습니다.
