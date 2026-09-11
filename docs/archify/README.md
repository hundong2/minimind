<!-- rumdl-disable MD013 -->

# MiniMind 코드 아키텍처 지도

분석일: 2026-09-11

- [대화형 아키텍처 Viewer](architecture.html)
- [Archify 원본 명세](architecture.json)
- [자동 브라우저 검증 receipt](architecture.visual-check.json)
- [light/dark 비교 contact sheet](architecture.visual-check.html)

## 분석 기준

| 항목 | 값 |
| --- | --- |
| 저장소 | <https://github.com/hundong2/minimind.git> |
| 분석 revision | `a3c7b01cc004d5de86aea961f20bf1e638e7c09e` |
| 다이어그램 유형 | `architecture` |
| 품질 profile | `showcase` |
| 중심 흐름 | JSONL → Dataset → Trainer → MiniMind model → checkpoint → 추론 진입점 |
| 보조 흐름 | tokenizer 공유, RL rollout의 Torch/SGLang backend 선택 |

revision은 번역·가이드·아키텍처 문서를 추가하기 전 분석 대상 코드 commit이다. 생성 문서 자체를 포함한 commit을 명세에 넣으면 commit hash가 다시 바뀌는 순환 참조가 생기므로, 코드 증거의 기준점을 별도로 고정했다.

이 그림은 하나의 프로세스가 모든 구성요소를 동시에 실행한다는 배포도 아니며, 클라우드 topology나 소유권을 나타내지 않는다. 여러 trainer와 CLI·FastAPI·Streamlit 진입점이 공통으로 재사용하는 코드 경계와 데이터 이동을 한 장에 요약한 **bounded code architecture map**이다.

## 대표 실행 흐름

### 1. 학습 경로

1. 각 trainer는 로컬 JSONL 경로와 이전 weight의 이름(prefix)을 CLI 인자로 받는다. tokenizer 경로는 trainer의 factory에서 `../model`로 고정되어 있으며, `--from_weight`도 임의 파일 경로가 아니라 `../out/<prefix>_<hidden>.pth`를 찾는 이름이다.
2. `dataset/lm_dataset.py`의 단계별 Dataset이 JSON 레코드를 token, mask, label tensor로 바꾼다.
3. `DataLoader`가 batch를 만들고 pretrain, SFT, LoRA, DPO, PPO, GRPO, distillation 또는 Agentic RL loop에 전달한다.
4. `MiniMindForCausalLM`은 공통 decoder-only model core에서 attention, Dense/MoE FFN과 language-model loss를 계산한다.
5. trainer는 추론에 쓰는 model-only state dict와, 재개에 쓰는 optimizer·scaler·step 포함 상태를 서로 다른 파일로 직렬화한다. 기본 추론 경로는 `out/*.pth`이고 resume 파일은 `checkpoints/*_resume.pth`이며 서로 교환할 수 없다.

다이어그램의 `Training Pipelines`는 모든 알고리즘이 한 실행에서 동시에 동작한다는 뜻이 아니다. 사용자가 선택한 trainer script 하나가 Dataset과 model core를 재사용한다.

### 2. 강화학습 생성 경로

PPO, GRPO와 Agentic RL trainer는 `RolloutEngine` 추상화를 통해 응답을 생성한다. 구현은 현재 process의 PyTorch model을 사용하는 `TorchRolloutEngine`과 별도 SGLang server를 호출하는 `SGLangRolloutEngine`으로 나뉜다. SGLang은 선택적 외부 runtime이며 pretrain·SFT의 필수 구성요소가 아니다.

### 3. 추론·서빙 경로

저장된 model-only 가중치와 tokenizer는 다음 진입점에서 다시 사용된다. optimizer 등을 포함한 `*_resume.pth`는 trainer 재개 전용이며 서빙 입력이 아니다.

- `eval_llm.py`: terminal 대화와 평가용 CLI
- `scripts/serve_openai_api.py`: `/v1/chat/completions`를 제공하는 FastAPI server
- `scripts/web_demo.py`: Streamlit chat UI

그림의 `사용자·API Client`는 소비자 역할을 뜻한다. repository의 sample client 기본 endpoint와 FastAPI server 기본 port는 서로 다르므로 별도 설정 없이 자동 연결된다고 해석하면 안 된다.

## 구성요소별 코드 근거

| 구성요소 | 코드 근거 | 확인한 역할 |
| --- | --- | --- |
| JSONL 학습 데이터 | `dataset/dataset.md:1`, `dataset/lm_dataset.py:37`, `:42` | 단계별 로컬 JSON/JSONL 입력과 `datasets.load_dataset` 호출 |
| MiniMind Tokenizer | `model/tokenizer_config.json:1`, `trainer/trainer_utils.py:119` | BPE 자산, chat template와 trainer 초기화 |
| Dataset Pipeline | `dataset/lm_dataset.py:37`, `:58`, `:122` | Pretrain·SFT·DPO sample을 token, mask, label로 변환 |
| Training Pipelines | `trainer/train_pretrain.py:24`, `trainer/train_full_sft.py:24`, `trainer/train_dpo.py:53` | 단계별 optimization loop와 batch 소비 |
| MiniMind Model Core | `model/model_minimind.py:10`, `:91`, `:234` | config, GQA/RoPE attention, Dense/MoE causal LM |
| Rollout Engine | `trainer/rollout_engine.py:51`, `:64`, `:99` | 생성 backend 계약과 Torch/SGLang 구현 |
| SGLang Server | `trainer/rollout_engine.py:99`, `:209` | 강화학습에서 선택 가능한 외부 생성 backend |
| 저장 아티팩트 | `trainer/train_pretrain.py:61`, `trainer/trainer_utils.py:63`, `:103` | model-only state dict와 resume state를 별도 파일에 저장 |
| 추론 진입점 | `eval_llm.py:12`, `scripts/serve_openai_api.py:105`, `scripts/web_demo.py:312` | model load, streaming generation, CLI/API/UI 시작점 |
| 사용자·API Client | `scripts/serve_openai_api.py:50`, `:179` | 요청 schema와 OpenAI 호환 chat endpoint |

파일이 같은 디렉터리에 있다는 이유로 관계를 만들지 않았다. import, 함수 호출, model load/save, Dataset 주입 또는 backend factory에서 확인된 관계만 포함했다.

## 상태·직렬화와 신뢰 경계

- **학습 데이터**: 실제 dataset 파일은 repository에 포함되지 않는다. trainer는 사용자가 준비한 로컬 경로를 신뢰 입력으로 읽으므로 schema, 출처, 개인정보와 prompt-injection 가능성을 별도로 검증해야 한다.
- **저장 아티팩트**: `out/*.pth`와 `checkpoints/*.pth`는 model-only state dict이고, `checkpoints/*_resume.pth`는 optimizer·step 등 재개 상태까지 담는다. 파일 역할을 혼동하지 말아야 하며, `torch.load`는 신뢰하지 않는 pickle 기반 파일에 사용하면 위험하므로 출처와 digest를 검증해야 한다.
- **SGLang**: 선택한 경우 별도 process·network 경계를 넘는다. endpoint 인증, TLS, timeout, retry와 response schema는 운영자가 보강해야 한다.
- **OpenAI 호환 API**: sample server는 `0.0.0.0:8998`로 bind할 수 있지만 코드에서 인증·rate limit을 확인하지 못했다. 인터넷에 그대로 노출하지 않는다.
- **Tool 실행**: Web/tool-call 예제 일부는 model이 만든 수식을 Python `eval`로 평가한다. 학습용 demo를 신뢰할 수 없는 사용자 입력에 그대로 사용하지 말고 parser와 allowlist로 교체한다.
- **Telemetry**: trainer의 `--use_wandb` 경로는 실제로 SwanLab을 import할 수 있다. 외부 전송을 켤 때 dataset·prompt·secret이 log에 포함되지 않는지 확인한다.

## 제외하거나 확정하지 않은 사항

- Hugging Face와 ModelScope 링크가 README에 있지만 대표 학습 경로의 자동 downloader 또는 `push_to_hub` 호출은 확인되지 않아 runtime upstream으로 그리지 않았다.
- `from_pretrained(..., trust_remote_code=True)`에 remote model ID를 전달하면 Hub 경계를 넘을 수 있으나, 로컬 경로도 허용하므로 고정 연결로 표현하지 않았다.
- cloud 배포 topology, container, queue, database, ownership, 인증 provider와 model registry는 코드 증거가 없어 만들지 않았다.
- repository에는 `tests/`, pytest/tox 설정과 CI workflow가 없다. `scripts/eval_toolcall.py`의 사례는 interactive harness이며 자동 pass/fail test suite로 간주하지 않았다.
- 여러 script가 `../model`, `../dataset`, `../out` 같은 현재 작업 디렉터리 기준 상대경로를 사용한다. README의 명령처럼 `trainer/` 또는 `scripts/`에서 실행하지 않으면 다른 파일을 가리킬 수 있다.
- 다이어그램은 공통 구조를 보여 주기 위해 training과 inference 경계를 함께 표시하지만, 하나의 end-to-end process topology라고 주장하지 않는다.

## Archify 검증 기록

### 결정론적 전달

```text
diagram_type: architecture
output: D:\workspace\laboratory\minimind\docs\archify\architecture.html
specification_sha256: 5f065ae29bf300b0ef99493a523a044b08f26151dd54fc8f4f67e6312ee7cbe5
artifact_sha256: 3b16ee42ad034b219bf57fec87921dceae78d99bfdd670efee2de8fb12c458dc
validation: 9/9 showcase, 0 errors, 0 warnings
browser_evidence: passed
manual_visual_review: passed
visual_correction_rounds: 1
```

`deliver` receipt는 exact specification bytes에서 self-contained HTML을 생성했고, 단일 SVG·유한 좌표·직교 화살표·레이블 여백·관계선 교차·공유 corridor·container border·route rhythm·legend의 9개 검사를 모두 통과했다.

### 자동 브라우저 증거

현재 artifact SHA-256에 묶인 `visual-check`는 Chrome에서 다음을 확인했다.

| viewport | theme | scrollWidth/innerWidth | scrollHeight/innerHeight | 결과 |
| --- | --- | --- | --- | --- |
| 1440×900 | light | 1440/1440 | 900/900 | 통과 |
| 1600×1000 | light | 1600/1600 | 1000/1000 | 통과 |
| 1920×1080 | light | 1920/1920 | 1080/1080 | 통과 |
| 2048×1320 | light | 2048/2048 | 1320/1320 | 통과 |

추가로 1440×900과 2048×1320의 light/dark screenshot 네 장과 contact sheet가 생성되었다. 이 결과는 자동 containment와 browser 동작의 증거이며 사람의 미적 판단을 대신하지 않는다.

### 이미지 기반 시각 검토

네 장의 현재 screenshot을 실제 이미지 도구로 확인했다.

- light와 dark 모두 node·card·legend·navigation dock이 잘리지 않았다.
- 관계선이 불투명 node를 통과하거나 의미가 다른 corridor로 겹치지 않았다.
- 레이블은 연결선과 구분되며 두 theme에서 읽을 수 있다.
- 2048×1320에서도 diagram과 결론 card가 균형 있게 공간을 사용하며 큰 빈 하단 띠가 없다.

첫 전달본은 `load weights` 레이블이 오른쪽 viewBox 경계에서 잘리는 시각 결함이 있었다. 레이블을 실제 연결 경로 내부로 옮긴 뒤 validate, deliver와 visual-check를 모두 다시 실행했고 잘림이 해소된 것을 확인했다. 이후 최종 코드 대조에서 model-only 가중치와 resume state가 서로 다른 용도임을 더 명확히 구분하도록 명세를 보정한 뒤, 현재 artifact에 대해 9/9 검증과 네 viewport의 browser evidence를 새로 수집하고 네 장의 light/dark screenshot을 다시 검토했다. 현재 artifact의 `manual_visual_review`는 `passed`, `visual_correction_rounds`는 `1`이다.

## 언어와 Viewer 제한

작성자가 제어하는 제목, node, 관계와 card는 한국어를 기본 언어로 작성했다. Archify Viewer가 한국어 locale을 지원하지 않으므로 `meta.locale`은 넣지 않았다. 따라서 고정 Viewer UI와 생성된 `<html lang>`은 영어 fallback이며, diagram 본문만 한국어로 현지화되어 있다.
