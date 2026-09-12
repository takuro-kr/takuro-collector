TAKURO Collector 0.2.0 RC

TAKURO Collector 0.1.9
=======================

목적
----
Windows PC에서 관리회사 매물을 수집하고 사진을 준비한 뒤,
직원이 REINS PDF를 추가하면 TAKURO 등록용 ZIP을 만드는 프로그램입니다.
WordPress 연동을 하지 않아도 로컬 수집/사진/PDF/ZIP 기능을 사용할 수 있습니다.

가장 쉬운 설치
--------------
1) 이 ZIP을 집 데스크탑의 원하는 폴더에 압축 해제합니다.
2) START_HERE.bat 를 더블클릭합니다.
3) 처음 실행이면 Python 및 필요한 라이브러리를 설치하고 바로가기를 만듭니다.
4) 이후에는 바탕화면의 TAKURO Collector 또는 RUN_TAKURO_COLLECTOR.bat 로 실행합니다.

※ Windows 10/11의 Microsoft Edge를 브라우저 수집 엔진으로 사용합니다.
※ Python이 없으면 winget으로 Python 3.12 설치를 시도합니다.
※ 설치 스크립트는 별도의 Playwright Chromium을 설치하지 않습니다.

기본 사용 흐름
--------------
[수집 시작]
 → 활성화된 8개 관리회사 사이트의 sitemap/root에서 상세 매물 링크 검색
 → 상세 페이지 수집
 → 1도3현(東京都/千葉県/埼玉県/神奈川県)만 SQLite 저장
 → 신규 여부는 source_site + source_property_id로 판단

특정 페이지를 바로 수집하려면 [URL 수집]에 상세 URL을 붙여넣습니다.

매물을 선택한 뒤:
[사진 다운로드]
 → 실내/외관/공용부 사진 후보 다운로드
 → 周辺/지도/로고/아이콘/배너 등은 수집 후보 단계에서 제외
 → 間取り는 별도 파일명으로 저장

[PDF 추가]
 → 직원이 REINS PDF 선택
 → 20MiB 이하의 실제 PDF인지 검사

[ZIP 만들기]
 → 基本情報.txt 자동 생성
 → 다운로드된 사진 + REINS.pdf 포함
 → TAKURO 기존 ZIP/FAST 등록 흐름과 호환되는 단일 매물 ZIP 생성

저장 위치
---------
데이터베이스/로그:
  %LOCALAPPDATA%\TAKURO Collector\

매물 사진/PDF:
  %USERPROFILE%\Documents\TAKURO Collector\properties\

생성 ZIP:
  %USERPROFILE%\Documents\TAKURO Collector\exports\

WordPress 연동
--------------
설정 → TAKURO 연동에서 켜거나 끌 수 있습니다.

연동 OFF:
- 로컬 수집/사진/PDF/ZIP 전부 사용 가능

연동 ON:
- TAKURO 0.36.74의 collection REST API에 신규 후보 전송
- WordPress 장애 시 로컬 sync_queue에 남겨 재전송
- 직원이 WordPress 신규매물 화면에서 REINS PDF를 선택하고 등록 준비로 바꾸면
  Collector의 [TAKURO 동기화]가 ready 작업과 PDF를 가져올 수 있음

연동 키 발급:
WordPress → 타쿠로 매물관리 → 등록 준비 → 신규매물 → Collector 연동 설정
→ 연동 키 생성/재발급

Collector에는 WordPress 관리자 비밀번호를 저장하지 않습니다.
Collector 키는 Windows에서는 DPAPI로 암호화하여 현재 Windows 사용자에 묶어 저장합니다.

TAKURO ZIP 업로드
-----------------
현재 WordPress 0.36.74의 공식 경계에 맞춰 Collector가 임의의 새 ZIP 업로드 API를 만들지 않습니다.
Collector에서 ZIP을 만든 뒤 [TAKURO ZIP 업로드 열기] 버튼으로 기존 WordPress ZIP 업로드 화면을 열어
생성된 ZIP을 올리면 기존 FAST 등록 흐름을 그대로 사용합니다.

로그인 사이트
-------------
왼쪽 사이트를 선택하고 [브라우저 열기 / 로그인]을 누릅니다.
사이트별 전용 Edge 프로필이 열립니다.
사람이 직접 로그인/CAPTCHA/MFA를 완료하고 브라우저를 닫은 뒤 다시 수집합니다.
로그인 우회 기능은 없습니다.

EXE가 필요한 경우
-----------------
이 소스 설치 상태에서 BUILD_EXE.bat 를 한 번 더블클릭하면
현재 Windows PC에서 실제 Windows 실행 파일을 만듭니다.

생성 위치:
  dist\TAKURO Collector\TAKURO Collector.exe
  TAKURO-Collector-Portable-Windows.zip

왜 이 대화에서 exe 자체를 바로 제공하지 않나?
----------------------------------------------
현재 제작 환경은 Linux라 Windows PyInstaller 바이너리를 신뢰성 있게 교차 빌드할 수 없습니다.
그래서 Windows에서 더블클릭 한 번으로 동일 소스를 빌드하는 BUILD_EXE.bat를 함께 제공합니다.

현재 구현된 사이트 adapter
---------------------------
AMM       otoku-chintai.com
SKY       skyc-chintai.jp
GOODCOM   goodcomasset-gc.co.jp
NAM       ref.namiki-grp.co.jp
AMB       pm.am-bition.jp
KIN       kinoshita-chintai.com
SYLA      rent.syla.jp
STAGEPLAN stageplan.es-ws.jp

사이트별 URL 식별 규칙과 adapter는 분리되어 있습니다.
수집은 requests + BeautifulSoup을 먼저 사용하고, 차단/JS 페이지 또는 지정 사이트는 Playwright + Edge로 재시도합니다.

주의
----
관리회사 사이트는 언제든 HTML 구조를 바꿀 수 있습니다.
이 패키지는 공통 일본 부동산 표/DT-DD/JSON-LD 구조와 사이트별 상세 URL 규칙을 기반으로 구현했습니다.
현재 제작 환경에서는 외부 사이트 DNS 접속이 차단되어 8개 운영 사이트를 실시간으로 직접 호출해 검증하지 못했습니다.
따라서 첫 실사용에서 특정 사이트의 건물명/호실/사진이 누락되면 작업 로그와 해당 URL을 전달하면 그 adapter만 보정하면 됩니다.
로컬 DB, ZIP 생성, WordPress 0.36.74 API 계약은 오프라인 자동 테스트로 검증했습니다.


[0.1.3 업데이트]
- KIN(木下の賃貸)의 家賃（管理費） 형식을 분리 파싱합니다.
- 전체 주소를 로컬 DB에 보존하고 상세보기에서 확인할 수 있습니다.
- 매물 더블클릭은 원본 사이트가 아니라 로컬 상세보기를 엽니다.
- 원본 사이트는 [원본 열기] 버튼으로 엽니다.
- 기존 SQLite DB는 그대로 사용됩니다. 같은 URL을 다시 [URL 수집]하면 해당 매물 정보가 갱신됩니다.

[0.1.4 업데이트]
- KIN(木下の賃貸) 전용 파서 보강
- 호실 오인식(예: ○) 방지, 제목/전용 필드에서 호실 추출
- 전체 주소 후보 중 잘리지 않은 주소를 우선 선택
- 家賃/賃料 + 管理費/共益費 분리형 표시 대응
- 방향 값에서 鍵交換費 등 뒤섞인 부가 텍스트 제거
- 입주일에서 物件ID 등 뒤섞인 텍스트 제거
- KIN 교통/층수 전용 정리


[0.1.7 업데이트]
- KIN 교통정보의 일본어 괄호/따옴표 표기(예: 京成本線「京成津田沼」駅 徒歩12分)를 인식하여 基本情報.txt의 交通 항목에 보존합니다.
- 수집 매물에서 [매물명 복사] 버튼으로 건물명을 클립보드에 복사할 수 있습니다. REINS 검색용입니다.
- 왼쪽 사이트 상태/작업 로그 영역의 최소 폭을 넓히고 작업 로그를 창 너비에 맞게 줄바꿈합니다.
- 사진 상태를 한국어로 표시합니다: 미다운로드/다운로드 완료/일부 완료/오류/사진 없음.
- PDF/ZIP 상태를 🟢 완료 / ⚪ 없음으로 표시합니다.
- [ZIP 폴더 열기] 버튼으로 Documents\TAKURO Collector\exports 폴더를 바로 엽니다.
- 잘못된 WordPress Collector 키는 HTTP 헤더 오류 대신 64자리 키 안내를 표시합니다.
- 기존 DB와 WordPress 0.36.74 후보 동기화 계약은 유지합니다. 자동 발행 기능은 없습니다.


[0.1.8 업데이트]
- KIN 현재 상세페이지의 분리형 교통 표 지원
  - 路線/バス会社
  - 駅名/停留所名
  - 徒歩時間
- 예: 京成本線 / + 京成大久保駅 / + 京成大久保駅：14分 / 停留所：
  -> 京成本線 京成大久保駅 徒歩14分
- 기존 한 줄형 교통 파서는 fallback으로 유지


[0.1.9 업데이트]
- KIN 4셀 표에서 礼金=賃料1ヵ月分 같은 값을 정확히 분리/정규화합니다.
- ZIP에 基本情報.txt 외에 収集情報.txt / 収集情報.json을 함께 저장합니다.
- KIN 이미지 resize/cache URL 변형의 중복 후보를 줄이고, 기존 SHA-256 중복 제거를 유지합니다.
- 상세보기에서 REINS 재입력용 텍스트를 마우스로 드래그해 복사할 수 있습니다.
