# 확인된 사실과 남은 확인 사항

## 2026-10-27 확인
- 사용자가 원하는 것은 새 공공데이터를 Central SCP에 올리고 독립 DB로 관리하는 것.
  로컬 영상/메타데이터 검토를 한 뒤 명시적으로 publish하며 추후 타 부서 이관을 고려한다.
- 기존 운영 DBMS는 MariaDB 10.5.8. `scpdb` 7테이블, `oncolabdb` 26테이블.
  테이블+인덱스 통계 합계 약 9.45GB, 실제 DICOM 파일 용량은 제외.
- 원천 `scpdb`는 직접 수정하지 않는 운영 원칙. LAB 부가 정보는 `oncolabdb`에 저장.
- `scpdb.dicom_instances`는 PatientID, Study/Series/SOP UID, Modality,
  InstancePath 등을 가진다. 영상 픽셀을 저장하는 DB가 아니다.
- `SOPInstanceUID`는 non-unique index. DB만으로 중복 방지가 보장되지 않는다.
- `lab_dataset_items`는 `(dataset_id, series_instance_uid)` unique 및 dataset FK.
- TCIA_FULL_PETCT 접두어는 PACS 요약상 378명/597검사/1,791시리즈/374,149인스턴스.
  LAB에서는 데이터셋 155, PSMA_PET_CT_Lesions_2026, source_type=pacs.
  이는 과거 확인 사례이며 신규 데이터셋의 ID/메타데이터로 재사용하지 않는다.
- 기존 로컬 sender 설정상 OS_LJM, 127.0.0.1:4001. 코드/테스트를 이 저장소로 이식.

## 2026-10-28 소유자 결정
- 새 관리 DB 위치: 이 서버(10.101.1.183). sudo가 없어 Docker(사용자 권한) MariaDB 11.4로 운영.
- Nightly backup: `/mnt/ai-pipeline/backups/ai-platform-dataops/` (CONTRACT §65, 00:30).
- Central SCP에는 아직 올리지 않음. Local SCP까지만 전송하고 메타데이터를 새 DB에 등록.
- 모든 결과를 보는 로컬 HTML 페이지(183에서 호스팅, loopback).
- 183에는 4001 포트 SCP가 없음(OS_LJM은 워크스테이션의 OncoStudio). 183에는 시스템 서비스
  Orthanc(`ORTHANC`, 4242/8042)가 있으나 대상으로 정하지 않음 — 실제 Local SCP 대상은 확인 필요.

## 구현 전에 구별할 것
- Central SCP의 실제 접속 설정과 수신 소프트웨어/조회 API는 아직 검증하지 않았다.
  DB 포트 3306을 DICOM 포트로 사용하지 않는다. 기존 문서의 후보를 운영 기본값으로 넣지 않는다.
- 새 DB 서버/계정은 미정. 소유자가 지정한 개발 DB에서 먼저 마이그레이션 검증한다.
- C-STORE success는 대상이 수신 성공 응답을 했다는 뜻이다. 실제 조회 가능성과 파일 영속성의
  독립 확인 방법은 수신기 기능에 달려 있다. 확인 미지원 상태를 verified로 표시하지 않는다.
- Local SCP 조회/웹뷰어 API 미확인. 초기 웹 미리보기는 준비된 파일에서 만들고
  OncoStudio를 통한 OS_LJM 확인과 구별한다.
- NIfTI -> DICOM 기존 코드는 검토용이다. LOCALREVIEW ID, modality 추정, UID 생성 방식은
  운영 배포 전 재검토 필요. 마스크는 기존 코드에서 skip, 자동 SEG/RTSTRUCT 변환 없음.

## 이식 출처
로컬 작업 공간의 `.claude/skills/send-local-scp/`에서 scripts, tests,
requirements, 문서와 wrapper를 가져왔다. 기존 설정/실데이터/압축 인수인계/backup은 복사하지 않았다.
파일별 초기 구현은 이 저장소 첫 커밋으로 보존한다.
