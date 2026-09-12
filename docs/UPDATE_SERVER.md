# TAKURO 정적 업데이트 서버

운영 endpoint는 `https://updates.takuro.tech/latest.json`이며 ZIP은
`https://updates.takuro.tech/releases/{version}/TAKURO-Collector-{version}.zip`에 둡니다.

서버는 PHP나 WordPress를 거치지 않고 두 파일을 정적으로 제공합니다.

- HTTP에서 파일을 제공하지 말고 HTTPS로만 접근시킵니다.
- directory listing을 비활성화합니다.
- JSON은 `application/json`, ZIP은 `application/zip`으로 제공합니다.
- `latest.json`은 `Cache-Control: no-cache` 또는 짧은 TTL을 사용합니다.
- 버전별 ZIP은 파일을 교체하지 않고 immutable 장기 cache를 사용할 수 있습니다.
- private signing key, 소스, credential은 서버에 업로드하지 않습니다.

서명 대상은 최상위 `signature` 필드를 제외한 manifest 전체입니다. UTF-8 JSON을
키 이름 오름차순, 공백 없음, `ensure_ascii=False`로 직렬화합니다. Python 표현은
`json.dumps(payload_without_signature, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")`입니다.

키 생성 예시(private key 경로는 반드시 저장소 밖):

```powershell
.\.venv\Scripts\python.exe scripts\release_update.py generate-key `
  --private-key "$env:USERPROFILE\.takuro-signing\update-ed25519-private.pem"
```

빌드 후 release 파일 준비:

```powershell
BUILD_EXE.bat
.\.venv\Scripts\python.exe scripts\release_update.py prepare `
  --version 0.4.7 `
  --zip .\TAKURO-Collector-Portable-Windows.zip `
  --private-key "$env:USERPROFILE\.takuro-signing\update-ed25519-private.pem" `
  --output .\release-output `
  --note "AMM 수집 지원" `
  --note "수집 매물 목록 정렬 지원" `
  --note "AMB 4개 현 목록 discovery 및 stale 매물 처리"
```

서버에는 `release-output/latest.json`과 `release-output/releases/0.4.7/TAKURO-Collector-0.4.7.zip`만 업로드합니다.
