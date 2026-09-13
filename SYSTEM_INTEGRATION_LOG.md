# SYSTEM_INTEGRATION_LOG

이 문서는 TAKURO-Collector와 WordPress 플러그인 등 시스템 구성요소 사이의 인터페이스 및 공유 데이터 변경을 누적 기록한다.

실제 코드와 이 문서가 다를 경우 실제 코드를 우선하며, 기존 기록은 덮어쓰지 않고 새 항목을 문서 아래에 추가한다.

기록 대상은 구성요소 간 계약에 영향을 줄 수 있는 변경으로 한정한다. 내부 리팩터링, 단순 UI·문구·스타일 변경 및 외부 인터페이스에 영향을 주지 않는 버그 수정은 원칙적으로 기록하지 않는다.

<!--
새 기록 기본 형식:

## YYYY-MM-DD - 변경 제목

Component:\
변경한 프로그램 또는 플러그인

Change:\
무엇을 변경했는지

Reason:\
왜 변경했는지

Interface/Data Changes:\
변경하거나 추가한 ID, key, API, function, hook, JSON, DB 구조 등

Affected Components:\
영향을 받을 가능성이 있는 다른 프로그램/플러그인

Compatibility:\
기존 버전 및 기존 데이터와 호환되는지

Required Follow-up:\
다른 구성요소에서 확인하거나 수정해야 하는 사항

Status:\
확인 필요 / 호환성 확인 완료 / 수정 완료 등
-->

## 2026-09-13 - Property Search 고객 공개 판정 통합 및 검색 인덱스 방어 보정

Component:\
TAKURO Property Search 3.7.3

Change:\
`Takuro_Property_Search_Visibility`를 고객 공개 판정의 단일 구현으로 추가하고 일반 검색, AJAX count, 고객 맞춤검색, 알림, 검색 결과 카드 및 고객용 단일 post 직접 접근이 이 service를 사용하도록 변경했다. 고객 맞춤검색의 해외심사 `category_90` 하드코딩을 제거하고 `overseas-screening` slug의 현재 term ID에서 `category_{term_id}` code를 해석한다. 검색 인덱스는 `_takuro_total_yen`이 누락되거나 `_takuro_rent_yen + _takuro_management_yen`과 다르면 계산값을 사용하며 post meta는 수정하지 않는다.

Reason:\
고객 노출 경로마다 workflow gate가 달라지는 문제, 운영 taxonomy term ID 의존성, stale total meta로 인한 검색·정렬 오류를 방지하기 위해서다.

Interface/Data Changes:\
새 public PHP interface `Takuro_Property_Search_Visibility::customer_visible_ids()`, `is_customer_visible($post_id)`, `constrain_query_args(array $args)`, `clear_cache()`를 추가했다. 기존 `_takuro_*`, taxonomy 이름/slug, index table schema와 `Takuro_Property_Search_Transition::sync_post($post_id)` signature는 변경하지 않았다. `_takuro_total_yen`의 authoritative writer는 Import V2로 유지하며 Search는 index row에만 defensive 계산값을 저장한다.

Affected Components:\
Import V2가 기록하는 workflow/meta/taxonomy와 Property Search가 소비하는 검색 인덱스. Registration V2와 Collector interface는 변경하지 않았다. `Takuro_Outgoing_Status`는 전체 공개 gate에 사용하지 않으며 inventory lifecycle 책임만 유지한다.

Compatibility:\
기존 meta와 taxonomy 형식에는 호환된다. `overseas-screening` term이 없으면 해외심사 필수 고객 맞춤검색은 숫자 ID fallback 없이 fail closed한다. 기존 index row에 defensive total을 반영하려면 전체 재색인이 필요하다.

Regression Results:\
소스 정적 회귀 12개 항목 통과: 단일 workflow predicate, 일반 검색, AJAX count의 공통 query 경유, 고객 맞춤검색, 알림, 카드, 직접 상세 접근, `Takuro_Outgoing_Status` 비의존, 해외심사 slug 동적 해석, 숫자 ID fallback 부재, total fallback, post meta 비변경. `search.js`, `navigation.js`, `listing-cards.js`의 `node --check`가 모두 통과했다. 로컬에 PHP 실행 파일이 없어 PHP lint와 동봉 PHP 테스트는 실행하지 못했다.

Total Mismatch Dry-run:\
실제 WordPress DB dry-run은 실행 환경 부재로 미실행이다. fixture dry-run 결과: `70000+5000/저장 75000 → 75000`, `저장 누락 → 75000`, `저장 70000 불일치 → 75000`, `관리비 누락 → 70000`, `rent 누락/저장 80000 → 80000`, `모두 누락 → NULL`. 이 dry-run은 운영 데이터 건수 결과가 아니다.

Full Reindex Result:\
실제 전체 재색인은 WordPress/PHP/운영 DB가 이 작업공간에 없어 미실행이다. 배포 후 관리자 도구 `TAKURO 검색 데이터 전환`의 100건 batch 재색인을 끝까지 실행하고, 실행 전후 published/indexed coverage와 total mismatch 건수를 확인해야 한다.

Required Follow-up:\
운영 또는 staging에서 PHP lint와 동봉 테스트를 실행한다. 배포 전 운영 DB에서 total mismatch dry-run을 수행하고 결과 건수를 이 항목에 후속 누적 기록한다. 배포 후 전체 재색인을 완료하고 coverage 및 taxonomy/meta 검색 비교 결과를 후속 기록한다.

Status:\
코드 구현 및 정적 회귀 완료 / 운영 DB dry-run·전체 재색인 확인 필요

## 2026-09-13 - Import V2 공유 파생 meta authoritative sync 통합

Component:\
TAKURO Import V2 2.0.0-rc30, TAKURO Registration V2 0.7.4

Change:\
Import V2의 기존 public `Takuro\ImportV2\Storage::sync($post_id)` 경로가 `_takuro_total_yen`, `_takuro_orientation_code`, `_takuro_structure_code`의 authoritative writer가 되도록 강화했다. Registration V2는 검수 저장, 레거시 test/retest 및 품질 테스트 값 적용 뒤 이 공통 sync를 명시적으로 호출한다.

Reason:\
원본 금액·방향·구조를 수정한 뒤 여러 저장 경로에 stale 파생값이 남고 Property Search 인덱스가 이를 읽을 수 있는 문제를 막기 위해서다.

Interface/Data Changes:\
meta key와 public method signature는 변경하지 않았다. `_takuro_total_yen`은 유효한 `_takuro_rent_yen`과 `_takuro_management_yen`의 합이며 관리비 meta가 없으면 계산에서 0으로 취급한다. rent 또는 존재하는 management 값이 유효하지 않으면 stale total을 삭제한다. `_takuro_orientation_code`와 `_takuro_structure_code`는 기존 Import Parser normalization/dictionary를 재사용해 label에서 만들고, label로 결정할 수 없을 때 정확히 하나의 canonical taxonomy term만 있으면 이를 사용한다. 결정할 수 없으면 stale code를 삭제한다. 처리 순서는 파생 meta → `_takuro_search_*` → `_takuro_search_meta_version` → `Takuro_Property_Search_Transition::sync_post($post_id)`이다.

Affected Components:\
TAKURO Property Search는 변경하지 않았지만 위 meta와 transition 결과를 소비한다. TAKURO-Collector request/interface에는 영향이 없다.

Compatibility:\
기존 key, taxonomy, API signature와 호환된다. 기존 post는 다음 `Storage::sync` 또는 백필 전까지 기존 파생값을 유지할 수 있다. 알 수 없는 label 또는 모호한 taxonomy는 추측하지 않고 stale code를 제거하므로 기존 잘못된 값이 사라질 수 있다. 중복 호출은 같은 결과를 만든다.

Required Follow-up:\
운영 DB 변경 없는 mismatch dry-run → 백업 → V2 post batch `Storage::sync` → Property Search 전체 재색인 → total/code mismatch와 검색 coverage 검증 순으로 수행한다. 이번 변경에서는 운영 backfill과 재색인을 실행하지 않았다.

Status:\
코드 구현 및 로컬 회귀 완료 / 운영 dry-run·backfill·재색인 대기

## 2026-09-13 - Goodcom 관리회사 식별값 추가

Component:\
TAKURO-Collector

Change:\
Goodcom 전용 수집 adapter를 추가하고 Collector 내부 site code를 `GOO`, WordPress로 전달하는 `management_company` 값을 `goo`로 고정했다.

Reason:\
Goodcom 전체 모집 inventory와 상세·사진을 기존 Collector/WordPress 후보 등록 흐름으로 처리하기 위해서다.

Interface/Data Changes:\
기존 후보 JSON 구조와 endpoint는 변경하지 않았다. 기존 `management_company` 필드에 새 허용값 `goo`가 추가되며, 로컬 site 상태/설정 식별값으로 `GOO`가 추가된다. 매물 identity는 `source_site=goodcomasset-gc.co.jp`와 숫자형 `source_property_id=room_id` 조합이다.

Affected Components:\
TAKURO Registration V2 및 관리회사 taxonomy/registry. WordPress에는 사전에 관리회사 slug `goo`가 생성됐으며 플러그인 코드 변경은 하지 않았다.

Compatibility:\
기존 JSON key, DB schema 및 KIN/AMB/AMM/SKY 데이터와 하위 호환된다. 과거 범용 stub의 `GOODCOM`/`GoodCom` 값은 실제 수집 데이터나 활성화 설정 충돌 징후가 없어 전용 `GOO`/`goo`로 교체했다.

Required Follow-up:\
새 Collector 배포 후 Goodcom 후보가 WordPress의 `goo` 관리회사 항목에 연결되는지 단건 운영 검증한다.

Status:\
Collector 구현 및 로컬 계약 회귀 완료 / 배포 후 WordPress 단건 연결 확인 필요

## 2026-09-14 - Collector inventory delivery outbox 및 Registration endpoint 전환 준비

Component:\
TAKURO Collector

Change:\
complete inventory discovery마다 안정적인 run을 생성하고 미전송 run을 덮어쓰지 않는 append-only delivery outbox를 추가했다. 신규 outbox delivery는 Registration V2 canonical inventory endpoint를 사용하며 기존 Connect inventory endpoint 코드는 호환 검증용으로 유지했다.

Reason:\
네트워크 실패·앱 종료·새 discovery 발생 시에도 동일 inventory run을 유실하지 않고 동일 run_id로 멱등 재전송하며, lifecycle 단일 writer를 Registration V2로 이전하기 위해서다.

Interface/Data Changes:\
DB schema는 v6이며 `inventory_delivery_outbox` table을 추가했다. `run_id`는 32자리 lowercase UUIDv4 hex, `completed_at`은 complete discovery 확정 시각의 UTC ISO-8601 Z다. 신규 request endpoint는 `POST /wp-json/takuro-registration/v1/inventory-snapshots`이며 Registration schema 1의 `schema_version=1`, `discovery_status=complete`, `run_id`, `completed_at`과 기존 inventory 의미 필드를 함께 전달한다. 성공에는 boolean `accepted=true`, 동일 `run_id`, 제출 고유 ID 수와 동일한 integer `accepted_count`, 올바른 `duplicate_run` 타입이 필요하다. 상태는 `pending`, `sending`, `accepted`, `retryable_error`, `rejected_terminal`이며 same-site FIFO를 적용한다. 기존 `POST /wp-json/takuro/v1/collection/inventory-snapshot` 코드는 자동 fallback 없이 유지한다.

Affected Components:\
TAKURO Registration V2 0.7.10의 canonical inventory REST와 배포 순서를 맞춰야 한다. TAKURO Connect legacy inventory writer는 아직 제거하지 않았으며 dual writer가 되지 않도록 전환 시점을 관리해야 한다. Import V2와 Property Search 코드는 변경하지 않았다.

Compatibility:\
기존 current `inventory_snapshots`와 모든 property/photo/settings 데이터는 유지한다. 유효한 legacy pending/error snapshot은 기존 snapshot_token으로 outbox에 1회 이관하고 synced snapshot은 재전송하지 않는다. 신규 endpoint 실패 시 legacy endpoint로 fallback하지 않는다.

Required Follow-up:\
Collector와 Registration V2 0.7.10을 함께 운영 배포하기 전에 동일 request/response 계약 로컬 E2E 결과를 확인하고, Connect legacy writer 중단 및 rollback 순서를 총괄 설계에서 승인해야 한다.

Status:\
Collector 코드·로컬 endpoint E2E·전체 회귀 완료 / Registration V2 0.7.10과 동시 운영 배포 대기

## 2026-09-14 - Collector INT-002 legacy Connect 안전 롤아웃

Component:\
TAKURO Collector 0.4.20

Change:\
append-only inventory outbox와 FIFO/retry 구조는 활성화하되 v0.4.20의 기본 inventory delivery destination을 `legacy_connect`로 고정했다. Registration V2 inventory client는 향후 전환용 dormant 코드로 유지한다.

Reason:\
Registration V2 0.7.10의 운영 검증은 완료됐지만 authoritative lifecycle writer 전환은 별도 총괄 승인이 필요하므로, 최초 outbox 배포에서 Connect와 Registration의 dual-write를 방지하기 위해서다.

Interface/Data Changes:\
기본 운영 request는 기존 `POST /wp-json/takuro/v1/collection/inventory-snapshot`과 기존 필드 `source_site`, `complete`, `errors`, `parse_errors`, `blockers`, `source_property_ids`만 사용한다. `run_id`, `completed_at`, checksum은 Collector outbox 내부에 보존하지만 legacy request에는 추가하지 않는다. 신규 `POST /wp-json/takuro-registration/v1/inventory-snapshots` client는 삭제하지 않았으며 기본 일반 동기화에서는 호출하지 않는다. 한 run에는 선택된 endpoint 하나만 사용하고 자동 fallback하지 않는다.

Affected Components:\
TAKURO Connect가 v0.4.20 운영 inventory의 유일한 destination이다. Registration V2 lifecycle/current/events는 일반 Collector 동기화로 변경되지 않는다. Import V2와 Property Search는 변경하지 않았다.

Compatibility:\
legacy Connect request/response shape를 유지하므로 기존 운영 endpoint와 호환된다. DB v6 outbox, same-site FIFO, restart recovery, candidate 실패 안전성은 그대로 활성화된다.

Required Follow-up:\
Registration V2를 authoritative inventory destination으로 전환하려면 총괄 승인 후 `registration_v2` mode로 단일 전환하고 Connect writer 중단 및 rollback 순서를 함께 검증해야 한다. dual-write는 허용하지 않는다.

Status:\
legacy Connect only 로컬 E2E 및 전체 회귀 완료 / v0.4.20 운영 배포 준비
