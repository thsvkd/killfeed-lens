# OCR 코드·모델 고지 출처

수집일: 2026-11-01. 대상은 현재 설치된 `rapidocr_onnxruntime 1.4.4`와 그 패키지에 포함된
ONNX 모델 세 개다. 이 폴더의 라이선스는 아래 upstream 원문을 수정 없이 복사했다.
모델카드는 내용은 그대로 두고 저장소 공백 검사에 맞춰 파일 끝의 추가 빈 줄만 정리했다.
실제 배포물에 이 폴더를 포함하는 작업과 포함 여부의 검증은 패키징 단계에서 별도로 수행한다.

## 복사한 원문

### `RapidOCR-LICENSE.txt`

- 대상: RapidOCR wrapper 코드의 upstream Apache-2.0 라이선스.
- 버전: `RapidAI/RapidOCR` 태그 `v1.4.4`.
- 커밋: `86ae3f5079df3422c1829cd84baf19bc8a7a9453`.
- [고정 커밋의 LICENSE 원문](https://raw.githubusercontent.com/RapidAI/RapidOCR/86ae3f5079df3422c1829cd84baf19bc8a7a9453/LICENSE).
- Git blob: `ab68e50ab2d0af41c9c8c30947a694a6285cde3f`.
- SHA-256: `3e0af25fdd06aa9586ae97adb00ea927ebe5a3805ac77d2d3a81ce5f55693333`.

### `PaddleOCR-LICENSE.txt`

- 대상: 변환 전 모델의 원 프로젝트인 PaddleOCR의 upstream Apache-2.0 라이선스.
- 수집 시 `PaddlePaddle/PaddleOCR`의 `main`이 가리킨 커밋:
  `dab3fe35379033fdcb2d0e9572fac0b36c9a9ebf`.
- [고정 커밋의 LICENSE 원문](https://raw.githubusercontent.com/PaddlePaddle/PaddleOCR/dab3fe35379033fdcb2d0e9572fac0b36c9a9ebf/LICENSE).
- Git blob: `430edfa0cfcb59f15b7aa5457427d0faf2e40dac`.
- SHA-256: `3840c5c0c61c294264d2dd77b8777be6ddd90121ef4e0e64abcd22edea581d6e`.

이 커밋을 세 모델의 학습·변환 시점이라고 주장하지 않는다. 원 프로젝트의 라이선스 원문을 수집한
시점과 모델 파일의 출처 확인을 구분한다.

### `RapidOCR-models-README.md`

- 대상: RapidOCR 공식 문서가 연결하는 `SWHL/RapidOCR` ONNX 모델 저장소의 모델카드 원문.
- 모델 저장소 커밋: `1cfba2e90fc938db55889873735088de210cc173`.
- [고정 커밋의 모델카드 원문](https://huggingface.co/SWHL/RapidOCR/resolve/1cfba2e90fc938db55889873735088de210cc173/README.md).
- Git blob: `6eadc148f2dbd47a0eea5c57df561edc295123dd`.
- SHA-256: `a2aa61e290af1ad9c143ba4212f87c976bc2d756cf676d20bda42d48b5b28b14`.

모델카드는 `license: apache-2.0`을 명시하고 PaddleOCR에서 변환한 ONNX 모델을 보관한다고
설명한다. 연결 근거는 [RapidOCR v1.4.4 공식 모델 목록](https://rapidai.github.io/RapidOCRDocs/v1.4.4/model_list/)이다.

## 실제 포함 모델과 바이트 대조

아래 파일은 `rapidocr_onnxruntime/models/`의 현재 설치 파일이며, 세 파일의 SHA-256 모두
위 모델 저장소의 고정 커밋에 기록된 Git LFS SHA-256과 정확히 일치했다.

| 파일 | 크기(bytes) | SHA-256 | 모델 저장소 경로 |
| --- | --- | --- | --- |
| `ch_PP-OCRv4_det_infer.onnx` | 4,745,517 | `d2a7720d45a54257208b1e13e36a8479894cb74155a5efe29462512d42f49da9` | [PP-OCRv4](https://huggingface.co/SWHL/RapidOCR/blob/1cfba2e90fc938db55889873735088de210cc173/PP-OCRv4/ch_PP-OCRv4_det_infer.onnx) |
| `ch_PP-OCRv4_rec_infer.onnx` | 10,857,958 | `48fc40f24f6d2a207a2b1091d3437eb3cc3eb6b676dc3ef9c37384005483683b` | [PP-OCRv4](https://huggingface.co/SWHL/RapidOCR/blob/1cfba2e90fc938db55889873735088de210cc173/PP-OCRv4/ch_PP-OCRv4_rec_infer.onnx) |
| `ch_ppocr_mobile_v2.0_cls_infer.onnx` | 585,532 | `e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c` | [PP-OCRv1](https://huggingface.co/SWHL/RapidOCR/blob/1cfba2e90fc938db55889873735088de210cc173/PP-OCRv1/ch_ppocr_mobile_v2.0_cls_infer.onnx) |

## NOTICE 및 수집 범위

확인한 RapidOCR·PaddleOCR 커밋과 모델 저장소에는 프로젝트 루트의 별도 `NOTICE`가 없었다.
PaddleOCR의 iOS 데모 등에 있는 별도 제3자 고지는 현재 ONNX 모델과 관련성이 확인되지 않아
이 폴더로 복사하지 않았다. 존재하지 않는 모델 전용 NOTICE를 새로 작성하지 않았다.

이 자료는 wrapper 라이선스, PaddleOCR 프로젝트 라이선스, 모델카드와 실제 모델 파일의 일치
확인을 제공한다. 학습 데이터·글꼴·상표·특허 등 모든 권리의 연쇄를 검증하거나 모델 권리 전반을
보장하는 자료는 아니다. 프로젝트 자체 코드의 라이선스를 선택하거나 PUBG 게임 자산에 대한
이용 허락을 부여하지도 않는다. 모델이나 wrapper 버전이 바뀌면 파일 해시·출처·고지를 다시 확인한다.

ONNX Runtime·OpenCV 등 설치된 wheel의 고지는 이 폴더와 별도로 패키징 시 수집한다.
Electron·Chromium·Pretendard의 기존 고지는 여기에서 중복 수집하지 않는다.
