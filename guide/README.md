<!-- rumdl-disable MD013 -->

# MiniMind 한국어 실전 학습 가이드

작성일: 2026-09-11
분석 기준 revision: `a3c7b01cc004d5de86aea961f20bf1e638e7c09e`

이 가이드는 MiniMind를 단순히 실행하는 데서 그치지 않고, 모델 구조와 데이터 계약을 읽고 사전학습부터 Agentic RL까지 학습 파이프라인을 추적할 수 있도록 구성했다. 설명은 위 revision의 실제 코드에 근거한다. 이후 upstream 변경으로 기본값이나 CLI가 달라질 수 있으므로 실행 직전에는 각 스크립트의 `--help`도 확인한다.

## 목차

- [학습 목표](#학습-목표)
- [추천 학습 순서](#추천-학습-순서)
- [프로젝트 지도](#프로젝트-지도)
- [10분 CPU 실습](#10분-cpu-실습)
- [실제 모델 실행으로 넘어가기](#실제-모델-실행으로-넘어가기)
- [환경 변수와 외부 자원](#환경-변수와-외부-자원)
- [검증 범위](#검증-범위)

## 학습 목표

가이드를 끝내면 다음을 할 수 있다.

1. `MiniMindConfig`의 차원 제약과 Dense/MoE 파라미터 규모를 설명한다.
2. Pretrain, SFT, DPO, RLAIF, Agent RL JSONL의 차이를 검사한다.
3. Pretrain → SFT → LoRA/증류/DPO → PPO/GRPO/Agent 경로 중 목적에 맞는 경로를 고른다.
4. `temperature`, repetition penalty, top-k, top-p가 후보 토큰을 어떻게 바꾸는지 재현한다.
5. checkpoint, DDP, rollout engine, API 서버의 운영상 위험을 점검한다.

## 추천 학습 순서

| 단계 | 문서 | 완료 기준 |
|---|---|---|
| 1 | [시작하기](01_getting_started.md) | 격리 환경을 만들고 CPU smoke test를 통과한다. |
| 2 | [핵심 개념](02_core_concepts.md) | 모델·데이터·학습 단계의 입력과 출력을 연결한다. |
| 3 | [고급 운영](03_advanced.md) | 재현성, 성능, 보안, 배포 체크리스트를 작성한다. |
| 4 | [예제 모음](examples/README.md) | 구성, 데이터, 샘플링 예제를 직접 변형한다. |

실제 코드 component와 실행 경로를 한 화면에서 보고 싶다면 [Archify 아키텍처 문서](../docs/archify/README.md)를 함께 본다.

처음부터 대규모 학습을 시작하지 않는 것이 좋다. 먼저 JSONL 스키마와 tensor shape를 확인하고, 매우 작은 데이터와 모델 설정으로 한 batch를 통과시킨 뒤 자원을 늘린다.

## 프로젝트 지도

| 경로 | 역할 | 먼저 볼 지점 |
|---|---|---|
| [`model/model_minimind.py`](../model/model_minimind.py) | Config, RoPE/YaRN, GQA, Dense/MoE, 생성 루프 | `MiniMindConfig`, `Attention`, `MOEFeedForward`, `MiniMindForCausalLM.generate` |
| [`model/model_lora.py`](../model/model_lora.py) | 수작업 LoRA 주입·저장·병합 | `apply_lora`, `merge_lora` |
| [`dataset/lm_dataset.py`](../dataset/lm_dataset.py) | 단계별 JSONL 로딩과 label mask | `PretrainDataset`, `SFTDataset`, `DPODataset`, `RLAIFDataset`, `AgentRLDataset` |
| [`trainer/trainer_utils.py`](../trainer/trainer_utils.py) | seed, LR, checkpoint, 모델 초기화 | `lm_checkpoint`, `init_model`, `SkipBatchSampler` |
| [`trainer/train_tokenizer.py`](../trainer/train_tokenizer.py) | 학습용 ByteLevel BPE tokenizer 생성 | 고정 입력·출력 경로, special token, chat template |
| [`trainer/train_pretrain.py`](../trainer/train_pretrain.py) | next-token 사전학습 | `train_epoch` |
| [`trainer/train_full_sft.py`](../trainer/train_full_sft.py) | assistant 구간 중심 SFT | `SFTDataset` 사용부 |
| [`trainer/train_distillation.py`](../trainer/train_distillation.py) | CE와 teacher KL 결합 | `distillation_loss` |
| [`trainer/train_dpo.py`](../trainer/train_dpo.py) | chosen/rejected 선호 최적화 | `dpo_loss` |
| [`trainer/train_ppo.py`](../trainer/train_ppo.py) | Actor/Critic, GAE, PPO | `ppo_train_epoch` |
| [`trainer/train_grpo.py`](../trainer/train_grpo.py) | 그룹 상대 advantage, GRPO/CISPO | `grpo_train_epoch` |
| [`trainer/train_agent.py`](../trainer/train_agent.py) | 다중 turn tool-use rollout과 지연 reward | `rollout_single`, `calculate_rewards` |
| [`trainer/rollout_engine.py`](../trainer/rollout_engine.py) | 로컬 Torch/SGLang rollout 추상화 | `RolloutEngine`, `create_rollout_engine` |
| [`eval_llm.py`](../eval_llm.py) | CLI 추론 | `init_model`, `main` |
| [`scripts/serve_openai_api.py`](../scripts/serve_openai_api.py) | OpenAI 호환 FastAPI endpoint | `/v1/chat/completions` |
| [`scripts/convert_model.py`](../scripts/convert_model.py) | native PyTorch/Transformers/LoRA 변환 | 변환 함수와 실행 block |

전체 흐름은 다음처럼 읽으면 된다.

```text
JSONL → Dataset/Chat Template → token ids + mask
      → MiniMindForCausalLM → logits / KV cache / MoE aux loss
      → CE, KL, preference 또는 policy loss → checkpoint
      → eval_llm / OpenAI-compatible API / 외부 inference runtime
```

## 10분 CPU 실습

아래 예제는 GPU, 모델 weight, 대규모 dataset, 서드파티 패키지가 필요 없다. Python 3.10 이상에서 저장소 루트를 현재 디렉터리로 두고 실행한다.

```bash
python guide/examples/model_config_check.py
python guide/examples/validate_data.py --kind all
python guide/examples/sampling_lab.py --seed 42 --top-k 4 --top-p 0.8
python guide/examples/smoke_test.py
```

정상이라면 마지막 명령은 `11 checks passed`를 출력한다. 이 실습은 실제 학습 결과를 재현하는 것이 아니라, 실제 코드의 구성 규칙·데이터 계약·생성 필터 순서를 작은 입력으로 검증하는 toy lab이다. 추적되는 초소형 [Pretrain fixture](examples/data/pretrain_tiny.jsonl)와 [SFT fixture](examples/data/sft_tiny.jsonl)는 CPU 학습 경로 점검에도 쓸 수 있다.

## 실제 모델 실행으로 넘어가기

실제 실행에는 PyTorch, Transformers 계열 의존성, tokenizer, weight가 필요하다. 권장 순서는 다음과 같다.

1. [시작하기](01_getting_started.md)의 환경 점검을 수행한다.
2. 공개된 Transformers 형식 모델을 별도 디렉터리에 내려받는다.
3. 짧은 `max_new_tokens`로 CLI 추론을 확인한다.
4. 샘플 JSONL을 [데이터 검사기](examples/validate_data.py)로 먼저 검증한다.
5. 작은 `hidden_size`, 짧은 sequence, batch 1로 dry run 후 본 학습 설정을 정한다.

공식 README의 대표 흐름은 다음과 같다.

```bash
# 저장소 루트에서 Transformers 형식 모델 추론
python eval_llm.py --load_from ./minimind-3 --max_new_tokens 128

# 학습 스크립트의 기본 상대 경로를 유지하려면 trainer에서 실행
cd trainer
python train_pretrain.py
python train_full_sft.py
```

기본 명령은 상당한 계산량을 요구한다. CPU smoke test와 전체 학습은 같은 의미가 아니다.

## 환경 변수와 외부 자원

로컬 CPU 예제에는 환경 변수가 필요 없다. MiniMind의 기본 로컬 추론도 API key를 요구하지 않지만, 다음 기능에는 별도 자격 증명이나 서비스가 생길 수 있다.

| 기능 | 필요한 것 | 주의점 |
|---|---|---|
| Hugging Face/ModelScope 비공개·제한 모델 | 각 서비스 token | token을 코드·JSONL·shell history에 넣지 않는다. |
| SwanLab 기록 (`--use_wandb`) | 계정 및 서비스 설정 | 민감한 prompt나 원문 dataset이 log로 전송되지 않는지 확인한다. |
| SGLang rollout | 별도 inference server와 공유 checkpoint 경로 | endpoint를 신뢰 경계 밖에 공개하지 않는다. |
| OpenAI 호환 API | 이 저장소 자체는 인증을 구현하지 않음 | reverse proxy에서 인증, TLS, rate limit를 추가한다. |

또한 학습에는 `dataset/*.jsonl`, native weight 추론에는 `out/*.pth`, resume에는 `checkpoints/*_resume.pth`가 필요하다. 현재 `.gitignore`가 자동으로 제외하는 것은 이 가운데 `out`뿐이다. `dataset/`, `checkpoints/`, 내려받은 모델 디렉터리는 자동 보호되지 않으므로 다운로드·학습 전에 팀 정책에 맞는 ignore 규칙을 정하고, commit 전 `git status --short`로 실제 추적 여부를 확인한다. 가이드의 작은 fixture 두 개는 재현 가능한 예제로 의도적으로 추적한다.

## 검증 범위

- 문서 근거: 저장소의 Python 소스, `README.md`, `README_en.md`, `requirements.txt`
- CPU 예제: Python 표준 라이브러리만 사용하며 네트워크와 파일 쓰기 없이 기본 실행 가능
- 미검증 범위: 전체 모델 weight 다운로드, GPU 학습 수렴, SGLang 연동, 공개 API 부하 시험
- 중요한 경계: 파라미터 계산 예제는 현재 구조를 반영한 정적 추정치이며, 실제 checkpoint 호환성과 학습 품질을 보증하지 않는다.
