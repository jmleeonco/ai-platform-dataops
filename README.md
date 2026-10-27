# AI Platform DataOps

공공 의료영상 데이터의 **디렉토리 해석 → Local SCP 검토 → Central SCP 및 독립 관리 DB 등록**을 위한 프로젝트입니다.

## 에이전트에 바로 전달하기

Claude Code 또는 Codex를 이 저장소 루트에서 실행하고 다음을 전달하세요.

> AGENTS.md와 docs/IMPLEMENTATION.md를 읽고 공공데이터 등록 워크플로우를 구현해줘.
> 기존 로컬 전송 코드를 활용하고, HTML에서 영상과 DB 등록 예정 값을 검토한 뒤
> 사용자가 선택한 실행 건만 Central SCP와 새 관리 DB에 등록하도록 해줘.
> 합성 데이터로 검증하고, 실제 운영 전송은 하지 마.
> 현재 구현 상태를 확인해서 완료된 부분 다음부터 이어가고, 운영 설정이 없어도
> 테스트 가능한 작업부터 끝내줘.

## 들어 있는 것과 남은 것

| 항목 | 상태 |
|---|---|
| DICOM / 2D·3D CT·MR NIfTI 로컬 전송 | 기존 코드 이식, 테스트 포함 |
| JSON 결과 → DB 등록 후보 JSON + 정적 HTML | 실행 가능, 영상 뷰어·승인 기능 없음 |
| 새 관리 DB 설계 | MariaDB DDL 초안, 미적용 |
| LLM 입력 해석 계약·HTML/API 설계·실패 복구 | 구현 지침 작성 |
| 대화형 HTML 검토 앱, Central 전송, 새 DB 등록 | 후속 구현 필요 |

HTML 검토 앱까지 완성된 제품이 아니라 **동작하는 로컬 기반 코드와 구현 인수인계 저장소**입니다.

## 실행

Python 3.10+ (현재 검증 환경은 Windows/Python). 저장소 루트에서:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
python tools/review.py --summary examples/synthetic-summary.json --dataset examples/dataset.json --out runtime/demo
```

`runtime/demo/review.html`을 브라우저로 열면 합성 데이터의 등록 예정 값을 확인할 수 있습니다.
`candidate-records.json`은 **계획 값**이며 DB에 저장되었다는 의미가 아닙니다.

로컬 전송 설정:

```powershell
Copy-Item config/local-scp.example.json .claude/skills/send-local-scp/local_scp.json
python .claude/skills/send-local-scp/scripts/send_local_scp.py send "D:\data\sample" --dry-run
# OncoStudio가 실행 중이고 사용자 요청으로 전송할 때:
python .claude/skills/send-local-scp/scripts/send_local_scp.py send "D:\data\sample"
```

로컬 기본 대상은 `127.0.0.1:4001`, called AE `OS_LJM`입니다. 이는 기존 작업 설정이며
다른 PC에서는 확인해야 합니다. 실제 연결은 이번 저장소 구성 작업에서 시험하지 않았습니다.
`--dry-run`도 변환 파일을 생성할 수 있지만 네트워크 전송은 하지 않습니다.

실행 결과로 preview 생성:

```powershell
python tools/review.py --summary "send_local_scp_runs/<run>/summary.json" --dataset examples/dataset.json --out runtime/review
```

실데이터에서는 예제 대신 본인 데이터셋 메타데이터 JSON을 사용하세요. 생성된 HTML/JSON에
환자 메타데이터가 포함될 수 있으므로 외부 게시하지 않습니다.

## 설계 문서

- [확정 요구사항·화면·실행 흐름](docs/WORKFLOW.md)
- [다음 에이전트 구현 순서·검증 기준](docs/IMPLEMENTATION.md)
- [새 DB·기존 DB와의 통합 규칙](docs/DATABASE.md)
- [2026-10-27 기준 확인 사실과 미확인 사항](docs/CONTEXT.md)
- [새 DB DDL 초안](sql/001_public_dataops.sql)

신규 DB 접속 정보와 Central 수신 대상은 `.env.example`을 참고해 별도 설정합니다.
운영 DB 로그인 정보와 원천 파일은 저장소에 포함하지 않습니다.
