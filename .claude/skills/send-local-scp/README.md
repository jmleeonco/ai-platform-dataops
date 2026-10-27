# send-local-scp

디렉토리의 DICOM은 그대로, NIfTI는 DICOM으로 변환해 **이 PC의 로컬 DICOM Storage SCP**로 C-STORE 전송하고,
성공/실패 건수와 환자/시리즈 식별 정보를 요약합니다. 로컬에서 변환 결과를 직접 열어보기 위한 도구이며
Central SCP 전송 기능은 없습니다.

## 파일

| 경로 | 내용 |
|---|---|
| `SKILL.md` | Claude Code `/send-local-scp <디렉토리>` 정의 |
| `<repository-root>\.agents\skills\send-local-scp\` | Codex `$send-local-scp` 정의 (`SKILL.md`). 아래 스크립트·설정을 그대로 사용 |
| `scripts/send_local_scp.py` | CLI: `send` / `echo` / `inspect` (설정 검증, 스캔, C-STORE, 요약) |
| `scripts/nifti_to_dicom.py` | NIfTI 분류(영상/mask/모호/미지원)와 CT/MR DICOM 변환 |
| `config.example.json` | 설정 예시 → `local_scp.json` 으로 복사해 사용 |
| `send-local-scp.cmd` | cmd/PowerShell용 래퍼 |
| `tests/test_send_local_scp.py` | 합성 데이터 + 테스트 SCP 검증 |

필요 패키지: `pydicom>=3`, `pynetdicom`, `nibabel`, `numpy` (저장소 requirements-dev.txt로 설치).

## 설정 (`local_scp.json`)

기존 작업 환경의 값(2026-10-26): OncoStudio 로컬 SCP `OS_LJM` @ `127.0.0.1:4001`, calling AE `SENDLOCALSCP`. 이 저장소에는 설정 예시만 포함합니다.

```json
{
  "destination": {"host": "127.0.0.1", "port": 4001, "called_ae": "OS_LJM", "calling_ae": "SENDLOCALSCP"},
  "nifti": {"modality": null, "patient_id_prefix": "LOCALREVIEW", "group_by": "dir", "layout": "auto",
            "max_series_per_study": 16},
  "output_dir": null
}
```

- `destination` 4개 값은 반드시 직접 채웁니다. host는 loopback 또는 이 PC의 IP만 허용됩니다(그 외는 실행 거부).
- 이 PC의 OncoStudio 로컬 SCP 설정: `C:\ProgramData\Oncosoft\OncoStudio\settings\dicom_local_scp.json`
  (AE/port). 같은 폴더의 `dicom_nodes.json` 에 있는 `ONCOPACS`(centralSCP)는 사용하지 않습니다.
- `output_dir` 기본값: `<repository-root>\send_local_scp_runs`. 입력 디렉토리 안은 허용하지 않습니다.
- `--config <경로>` 로 다른 설정 파일을 쓸 수 있습니다.

## 사용법

Claude Code와 Codex 모두 같은 스크립트(`scripts/send_local_scp.py`)와 같은 설정(`local_scp.json`)을 씁니다.
어느 쪽에서 보내도 결과는 같고, 실행 기록도 같은 `send_local_scp_runs\` 에 쌓입니다. 사용자가 요청한 로컬 전송 범위에서 실행합니다.

시작 전 공통: OncoStudio를 실행해 로컬 SCP(`OS_LJM`, 127.0.0.1:4001)가 열려 있어야 합니다.

### Claude Code

`<repository-root>` 에서 `claude` 를 실행한 뒤 프롬프트에 입력합니다.

```
/send-local-scp D:\data\case01                              # DICOM/NIfTI (modality 자동 판정)
/send-local-scp D:\data\BraTS_sample --modality MR          # 자동 판정이 안 되는 NIfTI → 직접 지정
/send-local-scp D:\data\ct_nifti --dry-run                  # 전송 없이 변환·분류·판정만
/send-local-scp D:\data\imagesTr --modality CT --group-by file
```

- skill 위치: `<repository-root>\.claude\skills\send-local-scp\SKILL.md` (`/` 입력 시 목록에 보임)
- 명령 실행 권한을 물으면 승인합니다.

### Codex

`<repository-root>` 에서 `codex` 를 실행한 뒤 프롬프트에 `$send-local-scp` 로 호출합니다. 옵션은 문장 안에 그대로
적습니다.

```
$send-local-scp D:\data\case01
$send-local-scp D:\data\BraTS_sample --modality MR
$send-local-scp D:\data\ct_nifti --dry-run
$send-local-scp D:\data\imagesTr --modality CT --group-by file
```

- skill 위치: `<repository-root>\.agents\skills\send-local-scp\`. 실제 코드는 Claude 진입점과 공유합니다.
- 네트워크 실행 권한은 사용하는 에이전트 환경의 설정을 따릅니다.
- 다른 폴더에서 Codex를 열면 이 skill이 보이지 않습니다. 반드시 `<repository-root>` 에서 시작하세요.

### 명령행 직접 실행 (Claude/Codex 없이)

`<repository-root>` 에서:

```powershell
python .claude/skills/send-local-scp/scripts/send_local_scp.py echo                       # 연결 확인 (C-ECHO)
python .claude/skills/send-local-scp/scripts/send_local_scp.py send D:\data\case01
python .claude/skills/send-local-scp/scripts/send_local_scp.py send D:\data\case01 --dry-run   # 변환·판정만
python .claude/skills/send-local-scp/scripts/send_local_scp.py inspect <repository-root>\send_local_scp_runs\<run>\nifti_dicom
```

| 옵션 | 의미 |
|---|---|
| `--modality auto\|CT\|MR` | NIfTI modality. 기본 `auto`(파일별 자동 판정). CT/MR 을 주면 그 실행의 모든 NIfTI 영상에 적용 |
| `--group-by dir\|file` | NIfTI 환자/스터디 묶음: 같은 폴더 = 한 환자(기본) / 파일마다 한 환자 |
| `--nifti-layout auto\|standard\|onco` | NIfTI 규약. `onco` = nii2OncoNii 출력(`*_image.nii.gz`) |
| `--force-image GLOB` | mask/모호 판정 파일을 영상으로 보냄 (상대경로 또는 파일명 glob, 반복 가능) |
| `--dry-run` | 변환·요약만 하고 전송하지 않음 (설정의 destination 불필요) |
| `--output-dir` | 실행 기록 위치 |

종료 코드: 0 성공, 1 일부 전송 실패 또는 변환 오류, 2 설정/입력 오류(전송 전 중단).

### 출력

콘솔 요약(성공/실패/전체 인스턴스, 시리즈별 PatientID·PatientName·StudyUID·Series#·Modality·Description·
SeriesInstanceUID, 실패 사유, 건너뛴 NIfTI와 이유)과 실행 기록 폴더:

```
send_local_scp_runs\<날짜_시간>_<입력폴더>\
  summary.json        전체 결과 (시리즈, 실패/경고 파일별 사유, 건너뜀, NIfTI 변환 geometry)
  series.csv          시리즈 표 (Excel에서 열림, UTF-8 BOM)
  nifti_dicom\<PatientID>\S001_<이름>\IM0001.dcm …   NIfTI 변환본 (직접 열어볼 수 있음)
```

실데이터 DICOM의 환자 정보가 summary에 그대로 남으므로 필요 없어진 실행 기록은 지워주세요.

## DICOM 전송

- 하위 폴더까지 모든 파일을 읽어 DICOM(preamble 없는 raw 파일 포함)을 찾습니다. DICOMDIR, 알려진 비DICOM 확장자,
  읽히지 않는 파일은 개수만 보고합니다. 같은 SOPInstanceUID는 한 번만 보냅니다.
- 파일은 읽기만 하고 내용 변경 없이 보냅니다. SCP가 파일의 압축 전송구문을 받지 않으면 메모리에서 압축 해제해
  보냅니다(SOPInstanceUID 유지, RLE는 기본 지원, JPEG 계열은 `pylibjpeg` 필요).
- C-STORE 상태 0x0000 = 성공, 0xB000/0xB006/0xB007 = 경고(성공으로 집계), 그 외 = 실패(상태 코드 표시).

## NIfTI → DICOM 변환 규칙

**지원 (v1)**: `.nii`/`.nii.gz`, 2D 또는 3D 스칼라 볼륨(뒤쪽 크기 1 차원은 무시), 출력 CT Image Storage 또는
MR Image Storage(단일 프레임, Explicit VR Little Endian).

- **modality (기본 auto, 파일별)**: NIfTI 헤더에는 modality가 없으므로 아래 순서의 근거로 판정하고, 근거를
  요약(`MR ← 경로 단어 ['t1n']`)과 각 DICOM의 ImageComments에 남깁니다.
  1. 같은 이름의 사이드카 JSON(`<이름>.json`, dcm2niix/BIDS)의 `Modality` — CT/MR 외 값(PT 등)이면 건너뜀
  2. 경로 단어 — MR: `t1 t1w t1c t1ce t1gd t1n t2 t2w t2f flair dwi adc swi pd mprage bold dti dce mr mri` 등,
     CT: `ct cect ncct cta hrct ldct cbct`, PET 등(`pet suv spect`)이면 건너뜀
  3. 값 분포 — HU 공기 영역(≤ -900)이 2% 이상이면 CT. 이 근거로 MR이라고 판정하지는 않음
  - 건너뛰는 경우: 근거 없음, MR 단어인데 값이 HU처럼 보임, MR·CT 단어가 함께 있음. 이때는 `--modality` 로 지정.
  - CT 단어로 판정했는데 값에 공기 영역이 없으면 "HU 보정 확인"을 표시(예: 0~4095로 저장된 CT).
  - `--modality CT|MR` 은 판정 없이 모든 NIfTI 영상에 적용됩니다. 섞인 폴더는 하위 폴더별로 실행하세요.

- **방향/위치/spacing**: sform(없으면 qform)을 RAS→LPS로 바꿔 voxel 격자를 그대로 사용합니다. 리샘플링이 없으므로
  oblique 영상도 그대로 oblique DICOM(ImageOrientationPatient)이 됩니다. 단위가 meter/micron이면 mm로 환산합니다.
- **표시 방향 / slice order**: 축 교환·뒤집기(무손실 index 연산)만 적용해 axial `1\0\0\0\1\0`, coronal
  `1\0\0\0\0\-1`, sagittal `0\1\0\0\0\-1` 형태로 맞추고, InstanceNumber가 slice normal(row×column) 방향으로
  증가하도록 정렬합니다. 각 voxel의 실제 위치는 변하지 않습니다.
- **voxel 값**: 정수 데이터는 그대로(bit-exact) 저장하고 NIfTI `scl_slope/scl_inter`는 RescaleSlope/Intercept로
  옮깁니다. 비정수 float는 int16으로 선형 양자화하고 최대 오차를 요약에 표시합니다. NaN/Inf는 최소값으로 대체하고
  개수를 표시합니다. Window는 값 분포(0.5~99.5 백분위)에서 계산합니다.
- **환자/스터디/시리즈**: NIfTI에는 환자 정보가 없으므로 원본 정보처럼 만들지 않습니다.
  PatientID `LOCALREVIEW_<그룹명>`, PatientName `LOCALREVIEW^<그룹명>`, StudyDescription
  `LOCAL REVIEW (NIfTI to DICOM)`, 생년월일/성별/StudyDate 등은 빈 값, ImageType `DERIVED\SECONDARY`,
  ImageComments에 원본 NIfTI 상대경로, "local placeholder" 표시, modality 판정 근거. 그룹명은 `group_by=dir`이면 입력 기준 상대 폴더
  경로, `file`이면 파일 경로입니다. 한 그룹 = 한 환자·한 스터디, 파일 하나 = 시리즈 하나(파일명순 SeriesNumber).
  한 폴더에 영상이 `max_series_per_study`개를 넘으면(여러 환자 혼재 가능) 그 폴더는 건너뛰고 `--group-by file`을 안내합니다.
- **UID**: 이름 기반 UUID(`2.25.…`)로 결정적으로 만듭니다. 같은 파일을 다시 보내면 같은 UID가 되어 로컬 SCP에
  중복 스터디가 생기지 않습니다(내용이 바뀌면 새 시리즈). FrameOfReferenceUID는 시리즈마다 따로 둡니다(서로 같은
  좌표계라는 근거가 NIfTI에 없음).
- **MR 필수 속성**: ScanningSequence `RM`, SequenceVariant `NONE`은 IOD 필수값을 채우기 위한 자리표시 값이며
  실제 촬영 정보가 아닙니다. CT RescaleType은 `HU`입니다.

### 영상 / mask / 모호 판정

| 판정 | 조건 | 처리 |
|---|---|---|
| mask | 경로에 mask/seg/label/annot/roi/gt 계열 단어 **그리고** 값이 0 이상 정수 20개 이하 | 건너뜀 |
| ambiguous | 둘 중 하나만 해당 (예: `t1_brain_masked.nii.gz` 연속값, 이름은 평범한데 값이 라벨) | 건너뜀, `--force-image` 안내 |
| ambiguous | `auto`에서 Onco NIfTI처럼 보임(대각 양수 affine + `*_image.nii.gz` 또는 근처 `dataset.json`/`Debug_dataset_loader_info.json`) | 건너뜀, `--nifti-layout` 안내 |
| image | 그 외 | 변환·전송 |

`--force-image` 로 지정한 파일은 영상으로 변환됩니다(요약 notes에 원래 판정 기록).

### 미지원 (건너뛰고 이유 표시)

4D 이상(시간/채널; 예: Decathlon BRATS 4채널, DWI), RGB/complex voxel, shear affine, sform/qform 코드가 모두 0,
segmentation mask의 DICOM SEG/RTSTRUCT 변환, Onco NIfTI 4D (C,Z,Y,X), CT/MR 외 modality, modality 근거 없음/충돌.

### Onco NIfTI (`--nifti-layout onco`)

nii2OncoNii `save_onco_nifti` 규약: 배열 `(Z,Y,X)`, affine은 LPS 대각행렬. 배열을 `(X,Y,Z)`로 되돌리고 affine을
LPS로 해석해 변환합니다. 표준 NIfTI로 잘못 읽으면 축과 좌우/전후가 뒤바뀌므로 `auto`는 추측하지 않습니다.

## 테스트

```powershell
python -m pytest .claude/skills/send-local-scp/tests -q
# nii2OncoNii 코드로도 교차 검증 (handoff tar의 repo/nii2OncoNii 경로)
$env:NII2ONCO_REPO = "<...>\repo\nii2OncoNii"; python -m pytest .claude/skills/send-local-scp/tests -q
```

테스트는 프로세스 내 테스트 SCP(127.0.0.1 임의 포트)로 실제 C-STORE를 하고, 받은 DICOM을 세 가지 독립 방법으로
원본 NIfTI와 대조합니다: (1) IPP/IOP/PixelSpacing만으로 재구성한 좌표로 모든 voxel의 월드 위치·값 1:1 비교,
(2) SimpleITK(GDCM) 시리즈 리더, (3) `NII2ONCO_REPO` 지정 시 nii2OncoNii `adjust_affine_from_dicom`.
케이스: LAS axial CT, oblique MR(float), sagittal(scl_slope/inter), coronal, 왼손 좌표계, 2D 단일 slice, Onco layout,
mask/모호/4D/shear/xform 없음, modality 자동 판정(사이드카/경로/HU/충돌/근거 없음), 그룹/UID 재현성, DICOM 원본 무변경, 제한된 SCP(CT·비압축만, 0xA700 실패, RLE 압축 해제),
원격 host 거부, CLI(한글 경로).

수동 확인용 로컬 수신기 (pynetdicom 내장):

```powershell
python -m pynetdicom storescp 11112 --bind-address 127.0.0.1 -aet TESTSCP -od D:\tmp\scp_received
```
