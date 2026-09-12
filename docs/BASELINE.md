# 검증 기준선

- 가져온 기준 소스 버전: `takuro_collector.__version__ == 0.4.0`
- DB 스키마 버전: `5`
- 원본 소스 위치: `C:\Users\fdz89\Documents\Codex\2026-09-11\new-chat\work\collector\TAKURO-Collector-0.2.9-PDF-AUTO-FAST-HOTFIX6-source`
- 최신 배포 파일: `TAKURO-Collector-0.4.0-Portable-Windows.zip`
- 배포 ZIP 크기: `98,609,209 bytes`
- 배포 ZIP SHA-256: `9E1C9E5072E39B5726972C78B79A498BF416FA127B3FC5AEB8875EB56F9EA500`
- ZIP 내부 실행 파일: `TAKURO Collector.exe` (`6,269,875 bytes`)
- 실행 파일 SHA-256: `7210AEBB80186A4AC44858E389A10A4163EED57E88067F24E9214DCC252B1ADA`
- 확인 시각: 2026-09-12 (Asia/Tokyo)
- 기준선 테스트: `129 passed in 4.76s`

## AMB 실제 검증

- 사이트/회사: `AMB` / `アンビション`
- 가격 정책: 플랜명과 관계없이 표시된 가격 행 중 월세가 가장 낮은 플랜을 선택. 같은 월세면 사이트 표시 순서상 첫 행 사용
- `https://pm.am-bition.jp/rent/2585/34310`: `敷・礼プラン`, 임대료 `69,000円`, 관리비 `4,000円`, 보증금/사례금 각 `1ヶ月`
- `https://pm.am-bition.jp/rent/2650/36644`: 단일 가격 행, 임대료 `140,000円`, 관리비 `15,000円`, 보증금/사례금 없음
- 실제 검증은 별도 임시 사용자 데이터 폴더에서 WordPress 동기화를 끈 상태로 수행

원본 작업 폴더 이름은 과거 버전을 가리키지만 실제 `__version__`과 최신 배포물은 모두 0.4.0입니다. 이후 변경은 이 저장소의 커밋과 태그를 기준으로 추적합니다.

현재 개발 버전은 AMB 전용 파서 보강 후 `0.4.1`입니다. 위 ZIP/EXE 해시는 수정 전 0.4.0 배포 기준선의 식별값입니다.

## 보호해야 할 회귀 범위

1. KIN 목록/상세 HTML 파싱, 호실·주소·교통·설비·사진 순서
2. WordPress 후보 동기화, 중복 사전조회, 완전 재고 스냅샷, 사진/TXT ZIP 업로드
3. 기존 8개 어댑터의 URL 판별과 등록 순서
4. 로그인 실패 시 다른 사이트 수집이 중단되지 않는 동작
