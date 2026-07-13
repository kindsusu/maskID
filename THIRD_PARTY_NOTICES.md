# 오픈소스 라이선스 고지 (Third-Party Notices)

MarkID(버전 1.3.0)는 아래의 오픈소스 구성요소를 사용합니다. 각 구성요소의
저작권과 라이선스는 해당 프로젝트에 귀속됩니다. 이 문서의 정보는 빌드에 사용된
가상환경(`.venv`)에 실제로 설치된 패키지 메타데이터(`*.dist-info/METADATA`,
`LICENSE`/`COPYING`)에서 확인한 것입니다.

- MarkID는 완전 오프라인으로 동작하며, 아래 런타임 구성요소를 실행 파일에 함께
  번들합니다.
- 본 프로젝트의 소스 코드는 공개 GitHub 저장소에서 공개하고 있습니다:
  **https://github.com/kindsusu/maskID**

---

## 1. 런타임 구성요소 (실행 파일에 번들됨)

| 패키지 | 버전 | 라이선스 | 홈페이지 / 소스 |
|---|---|---|---|
| PyMuPDF | 1.25.2 | **AGPL-3.0-only** (또는 상용 이중 라이선스) — 아래 2절 참조 | https://github.com/pymupdf/pymupdf |
| Pillow | 10.3.0 | HPND (Historical Permission Notice and Disclaimer) | https://python-pillow.org |
| numpy | 2.2.4 | BSD-3-Clause | https://numpy.org |
| rapidocr_onnxruntime | 1.4.4 | Apache-2.0 | https://github.com/RapidAI/RapidOCR |
| onnxruntime | 1.21.0 | MIT | https://onnxruntime.ai |
| opencv-python | 4.11.0.86 | Apache-2.0 (메타데이터 기준) | https://github.com/opencv/opencv-python |
| pyclipper | 1.3.0.post6 | MIT | https://github.com/greginvm/pyclipper |
| shapely | 2.0.7 | BSD-3-Clause | https://github.com/shapely/shapely |
| six | 1.17.0 | MIT | https://github.com/benjaminp/six |
| PyYAML | 6.0.3 | MIT | https://pyyaml.org |

> 위 라이선스 표기는 각 패키지의 `dist-info` 메타데이터(License 필드 및
> `Classifier: License ::` 항목)에서 읽은 값입니다. numpy의 경우 License 필드가
> 저작권 문구로 되어 있으나 분류자(`License :: OSI Approved :: BSD License`)에
> 따라 BSD-3-Clause로 표기했습니다. opencv-python은 메타데이터 License 필드가
> "Apache 2.0"으로 기재되어 있습니다.

---

## 2. PyMuPDF 라이선스에 관한 중요 고지 (AGPL-3.0)

PyMuPDF와 그 기반인 MuPDF는 **Artifex Software, Inc.** 가 유지·개발하며,
**오픈소스 AGPL(GNU Affero General Public License, 버전 3)** 과 **상용 라이선스**
중 하나를 선택할 수 있는 **이중 라이선스(dual license)** 로 배포됩니다.
(패키지 메타데이터: `License: GNU AFFERO GPL 3.0`, 저작자: Artifex,
`support@artifex.com`.)

AGPL-3.0은 강한 카피레프트 라이선스로, PyMuPDF를 포함해 배포·서비스하는
소프트웨어에 대해 **동일 조건의 소스 공개 의무**를 부과합니다. 상용
라이선스 조건을 충족할 수 없는 경우 Artifex의 AGPL 조건을 따라야 합니다.

**MarkID의 준수 방식:** MarkID는 별도의 상용 PyMuPDF 라이선스를 구매하지 않고
AGPL-3.0 경로를 따릅니다. 이에 따라 본 프로젝트의 **전체 소스 코드를 공개 GitHub
저장소(https://github.com/kindsusu/maskID)에서 공개**하고 있습니다. MarkID
실행 파일(바이너리)을 제3자에게 배포·제공하는 경우, 동일한 버전의 대응 소스 코드에
접근할 수 있도록 위 저장소를 함께 안내해야 합니다.

- AGPL-3.0 전문: https://www.gnu.org/licenses/agpl-3.0.html
- 상용 라이선스 문의: https://artifex.com/contact/pymupdf-inquiry.php

---

## 3. OCR 인식 모델 (models/)

MarkID에 포함된 한국어 OCR 모델 파일은 다음과 같습니다.

| 파일 | 용도 | 출처 |
|---|---|---|
| `models/korean_rec.onnx` | 한국어 텍스트 인식(recognition) 모델 | RapidOCR 프로젝트 배포 한국어 인식 모델 (PaddleOCR 계열) |
| `models/korean_dict.txt` | 위 인식 모델의 문자 사전(dictionary) | 위와 동일 |

이 모델은 RapidOCR(RapidAI) 프로젝트가 배포하는 한국어 인식 모델로, 계보상
PaddleOCR(PaddlePaddle) 계열의 인식 모델에 해당합니다. 다만 **모델 파일 자체에는
라이선스/저작권 파일이 동봉되어 있지 않으며**, 배포 패키지 메타데이터만으로는
정확한 라이선스 조항을 단정할 수 없습니다.

> **주의 — 출처·라이선스 재확인 필요:** OCR 모델(`korean_rec.onnx`,
> `korean_dict.txt`)의 정확한 원출처와 라이선스는 RapidOCR/PaddleOCR 배포처에서
> 별도로 재확인해야 합니다. 현재 파일에서 직접 확인 가능한 저작권/라이선스 문구는
> 없으며, 본 문서는 확인되지 않은 저작권 문구를 임의로 기재하지 않습니다.
> (참고 배포처: https://github.com/RapidAI/RapidOCR ,
> https://github.com/PaddlePaddle/PaddleOCR )

---

## 4. 빌드 도구 (실행 파일에 포함되지 않음)

아래 도구는 실행 파일을 **생성**하는 데에만 사용되며, 배포되는 MarkID.exe에는
그 소스가 포함되지 않습니다.

| 도구 | 버전 | 라이선스 | 홈페이지 |
|---|---|---|---|
| PyInstaller | 6.21.0 | GPL-2.0-or-later (부트로더 예외 조항 포함 — 비자유/상용 프로그램의 빌드·배포 허용) | https://pyinstaller.org |

> PyInstaller의 GPL 예외 조항은 PyInstaller로 **빌드된 결과물**을 자유롭게(상용
> 포함) 배포하도록 허용합니다. 즉, PyInstaller의 라이선스가 MarkID 자체의 라이선스
> 선택을 강제하지 않습니다. (MarkID의 소스 공개 의무는 위 2절의 PyMuPDF AGPL에서
> 비롯됩니다.)

---

*이 문서는 빌드 환경에 설치된 실제 패키지 버전을 기준으로 작성되었습니다.
의존성 버전이 바뀌면 `tools/`의 빌드 절차에 따라 재생성 후 본 문서를 갱신하세요.*
