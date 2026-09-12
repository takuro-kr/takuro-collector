# 관리회사 추가 절차

관리회사는 한 번에 하나만 추가하고 아래 단계를 모두 통과한 뒤 다음 회사로 진행합니다.

## 1. 입력 확정

- 관리회사명과 공식 임대 매물 사이트 도메인
- 공개 목록 URL 1개 이상
- 공개 상세 URL 2개 이상(서로 다른 건물/호실 권장)
- 로그인이 필요한지, 이용약관/robots 정책상 자동 접근이 허용되는지

실제 계정, 비밀번호, 쿠키는 문서·fixture·커밋에 기록하지 않습니다.

## 2. 어댑터 작성

`takuro_collector/sites/<code>.py`에 `BaseAdapter` 하위 클래스를 만들고 다음 값을 정의합니다.

- 고유한 `code`, 표시용 `label`, `management_company`
- 허용 `domains`, 시작점 `seed_urls`, 상세 URL `detail_patterns`
- 필요할 때만 `force_browser` 또는 `login_expected`

그다음 `takuro_collector/sites/__init__.py`의 `ADAPTER_CLASSES` 마지막에 등록합니다. 기존 어댑터 순서는 바꾸지 않습니다.

## 3. 오프라인 계약 테스트

최소 HTML fixture에는 개인정보나 로그인 상태가 없어야 하며 다음을 검증합니다.

- URL 일치/불일치와 안정적인 `source_property_id`
- 건물명, 호실, 1도3현 주소, 임대료, 관리비
- 교통, 설비, 사진 URL의 순서와 중복 제거
- 지원 지역 밖 주소 및 불완전 페이지의 안전한 거부
- WordPress payload의 기존 필드가 변하지 않음

## 4. 실제 사이트 단건 테스트

먼저 URL 수집으로 상세 URL **1개만** 실행합니다. 별도 임시 `TAKURO_COLLECTOR_HOME`을 사용하고 WordPress 자동 동기화는 끕니다. 결과 필드와 사진을 사람이 확인한 뒤 두 번째 URL을 테스트합니다.

실제 테스트 중 생성된 DB, HTML, 사진, 로그는 Git에 추가하지 않습니다.

## 5. 회귀 및 WordPress 테스트

```powershell
.\.venv\Scripts\python.exe -m pytest -q
powershell -ExecutionPolicy Bypass -File scripts/check-repository.ps1
```

WordPress 테스트는 가짜 로컬 응답/클라이언트를 사용합니다. 운영 WordPress에 쓰는 테스트는 사용자가 명시적으로 승인한 후보 1건으로만 수행하며 자동 등록/공개 전환 여부를 별도로 확인합니다.

## 6. 커밋 단위

한 회사당 어댑터, 최소 fixture, 테스트, 문서를 한 커밋으로 묶습니다. 권장 메시지는 `feat(site): add <company> adapter`입니다.

