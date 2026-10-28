# AI Platform DataOps

공공 의료영상 데이터셋 폴더를 웹 브라우저에서 처리하는 프로젝트입니다. 운영 위치는 10.101.1.183입니다.
흐름은 **폴더 지정 → 에이전트 분석·HTML 보고서 → 2케이스 Local SCP 시험 전송 → 최종 승인 → Central SCP 전송과 관리 DB 등록**입니다.
Central 대상은 아직 정하지 않았습니다(2026-10-28). 승인까지는 진행되고, 전송은 대상을 설정할 때까지 대기합니다.

## 상태

| 항목 | 상태 |
|---|---|
| 입력 폴더 inventory (DICOM 확장자 유무, NIfTI, 사이드카, 기타; backup/숨김 폴더 제외) | 구현, 테스트 |
| 버전 있는 plan(schema 1) + 결정적 검증기, 미해결 항목/추정 modality 구분 | 구현, 테스트 |
| 고정 artifact(prepare): DICOM 바이트 그대로 복사, NIfTI→DICOM 결정적 변환, sha256 manifest | 구현, 테스트 |
| Local SCP 전송: 인스턴스별 journal, 재시도, 변조 artifact 차단, 원격 host 차단 | 구현, 테스트 (loopback SCP) |
| 관리 DB: MariaDB 11.4 (Docker, 183), migration 0001–0002, 멱등 등록·충돌 시 롤백 | 구현, 테스트 (일회용 schema) |
| 로컬 HTML 뷰어 (run·메타데이터·DB 예정/등록 비교·전송 결과·슬라이스 뷰어·DB 테이블·백업) | 구현, 테스트 (읽기 전용) |
| Nightly backup → `/mnt/ai-pipeline/backups/ai-platform-dataops` (00:30, §65 규격) | 구현, 1회 실행, 복원 검증 |
| 웹 흐름: 폴더 → 분석 에이전트(Claude, 읽기 전용) → 결정적 검증 → 보고서·수정 → 파일럿 → 승인 | 구현, 테스트, AMOS로 실제 실행 |
| 라벨 맵 → RTSTRUCT (영상 격자 일치 확인, 왕복 Dice, 뷰어 윤곽 표시) | 구현, 테스트, AMOS 2케이스 OS_LJM 수신 |
| Central 전송 어댑터 (승인 plan과 해시가 맞을 때만, journal·재시도) → DB 등록 | 구현, 테스트 (loopback). 실제 대상 미설정 |
| oncolabdb export, Central 수신본 검증(scpdb 조회) | 미구현 |

## 사용 (183, 저장소 루트)

```bash
PY=~/.venvs/ai-platform-dataops/bin/python
$PY -m pytest -q                                           # 61 tests (DB 테스트는 관리 DB가 있어야 실행)
$PY -m dataops plan /path/to/dataset --dataset ds.json     # inventory + plan → runtime/runs/<run>/
$PY -m dataops validate RUN                                # plan.json 수정 후 재검증
$PY -m dataops prepare RUN                                 # artifact 고정 + manifest
$PY -m dataops send-local RUN [--dry-run]                  # Local SCP로 C-STORE
$PY -m dataops register RUN                                # 관리 DB 등록
$PY -m dataops ingest DIR --dataset ds.json --send-local --register   # 한 번에
deploy/web.sh start                                        # 웹 + worker, http://127.0.0.1:8096/
$PY -m dataops analyze DIR | pilot RUN | approve RUN --by 이름 --confirm 데이터셋이름 | publish RUN
```

`ds.json` 예시는 `examples/dataset.json`입니다. NIfTI 대상자가 있으면 `patient_id_prefix`가 필요합니다.
설치·DB·백업·복원은 [docs/OPERATIONS.md](docs/OPERATIONS.md)를 보세요.

기존 단독 sender(`/send-local-scp`)와 오프라인 미리보기(`tools/review.py`)도 그대로 동작합니다.

## 설계 문서

- [운영(DB·웹·백업·복원)](docs/OPERATIONS.md)
- [확정 요구사항·화면·실행 흐름](docs/WORKFLOW.md)
- [구현 순서·검증 기준](docs/IMPLEMENTATION.md)
- [새 DB·기존 DB와의 통합 규칙](docs/DATABASE.md)
- [확인 사실과 미확인 사항](docs/CONTEXT.md)
- [검증 기록](docs/VALIDATION.md)
- DB migration: `sql/migrations/`

비밀값(DB 비밀번호)은 `~/.config/ai-platform-dataops/env`에만 있고 저장소·백업에 포함하지 않습니다.
생성된 HTML/JSON에 데이터셋 메타데이터가 있으므로 외부에 게시하지 않습니다.
