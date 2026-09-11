<!-- rumdl-disable MD013 -->

# 02. 모델·데이터·학습 파이프라인

## 1. 모델 한눈에 보기

MiniMind의 중심은 Hugging Face `PreTrainedModel`/`GenerationMixin`과 연결되는 작은 Decoder-only language model이다 (`model/model_minimind.py:234-288`). 기본 Dense 구성은 다음 값으로 선언된다.

| 항목 | 기본값 | 코드 의미 |
|---|---:|---|
| vocabulary | 6,400 | embedding과 LM head가 같은 weight를 공유한다. |
| hidden size | 768 | residual stream 차원 |
| layers | 8 | `MiniMindBlock` 수 |
| query heads / KV heads | 8 / 4 | GQA로 KV를 query head 수에 맞게 반복한다. |
| head dimension | 96 | 기본적으로 `hidden_size / query heads` |
| intermediate | 2,432 | `ceil(hidden × π / 64) × 64` |
| max positions | 32,768 | 미리 계산하는 RoPE buffer 범위 |
| RoPE theta | 1,000,000 | rotary frequency base |
| Dense/MoE | Dense | MoE 선택 시 4 experts, token당 top-1 |

기본 정적 파라미터 추정은 Dense 약 63.91M, MoE 약 198.42M이다. [구성 검사 예제](examples/model_config_check.py)로 계산할 수 있다.

## 2. 한 block의 실행 흐름

```text
token ids
  → embedding + dropout
  → [RMSNorm → Q/K/V projection → QK-Norm → RoPE
     → causal GQA attention → output projection → residual]
  → [RMSNorm → SwiGLU Dense FFN 또는 routed MoE FFN → residual]
  → final RMSNorm → tied LM head → logits
```

핵심 구현 근거:

- RMSNorm은 float32로 norm을 계산한 뒤 입력 dtype으로 되돌린다 (`model/model_minimind.py:50-60`).
- Q/K에 RMSNorm과 rotary position embedding을 적용한다 (`91-124`).
- 가능한 조건에서는 PyTorch scaled-dot-product attention을 사용하고, cache·mask 조건에 따라 명시적 causal attention으로 fallback한다 (`125-133`).
- FFN은 `down(SiLU(gate(x)) * up(x))`인 SwiGLU 형태다 (`136-146`).
- block은 attention과 FFN 모두 pre-norm residual 구조다 (`178-194`).
- label이 주어지면 logits와 label을 한 token shift하여 cross entropy를 계산하며 `-100`은 무시한다 (`245-253`).

### Tensor shape 추적

`B=batch`, `S=sequence`, `H=hidden`, `Nq=query heads`, `Nkv=KV heads`, `D=head dim`이라 두면:

```text
input_ids                [B, S]
embedding                [B, S, H]
Q                        [B, S, Nq, D]
K, V                     [B, S, Nkv, D]
repeat_kv 후 K, V         [B, S, Nq, D]
attention output         [B, S, H]
logits                   [B, S, vocab]
```

반드시 지켜야 할 것은 `Nq % Nkv == 0`과 RoPE를 위한 짝수 `D`다. `repeat_kv`는 정수 반복 횟수를 사용하고, rotary 연산은 head dimension을 두 절반으로 나누기 때문이다. 반면 `H % Nq == 0`은 이 구현의 필수 runtime 제약이 아니다. Q projection은 `H → Nq×D`, output projection은 다시 `Nq×D → H`로 매핑하므로, 예를 들어 `H=770`, `Nq=8`, 기본 `D=96`도 실행할 수 있다 (`model/model_minimind.py:24`, `100-103`).

다만 `H=Nq×D`는 일반적인 canonical 구성이고 converter, 외부 runtime, checkpoint가 이를 암묵적으로 기대할 수 있다. [구성 검사기](examples/model_config_check.py)는 이런 차이를 기본적으로 경고만 하며, artifact 호환성을 강제하려면 `--strict-canonical`을 쓴다. 원본 Config가 KV 배수·짝수 차원·expert top-k를 조기에 검증하지 않으므로 실험 전에 검사한다.

## 3. Dense와 MoE

`use_moe=False`면 모든 token이 하나의 FFN을 통과한다. `True`면 gate softmax가 expert 점수를 만들고 top-k expert를 골라 token별 결과를 합산한다 (`model/model_minimind.py:148-176`). 기본은 4 experts/top-1이다.

학습 시에는 expert 사용 균형을 위한 auxiliary loss가 추가된다. 각 trainer가 language/policy loss에 `aux_loss`를 합치는지 확인해야 한다. MoE의 총 파라미터와 활성 파라미터는 다르며, 순수 PyTorch token dispatch는 작은 규모에서도 Dense보다 빠르다고 보장되지 않는다.

## 4. Tokenizer 경로와 호환성

정상적인 학습·추론은 저장소의 `model/tokenizer.json`과 `model/tokenizer_config.json`을 그대로 재사용하는 경로다. tokenizer는 단순 전처리 도구가 아니라 vocabulary token ID, embedding/LM-head 행, BOS/EOS/pad ID, special token과 chat template를 하나로 묶는 모델 artifact다.

`trainer/train_tokenizer.py`는 학습·참고 목적으로 별도 ByteLevel BPE tokenizer를 만드는 경로도 제공한다. 현재 코드는 다음을 하드코딩한다 (`trainer/train_tokenizer.py:1-10`, `12-55`, `166-168`).

- `../dataset/sft_t2t_mini.jsonl`의 `conversations[*].content`를 최대 10,000행 읽는다.
- vocabulary 6,400과 special-token slot 36개로 학습한다.
- 결과를 `../model_learn_tokenizer/`에 쓰고 chat template를 포함한 config를 만든다.
- CLI argument가 없으므로 의도적으로 실험할 때는 상수나 import wrapper에서 입력·출력을 명시해야 한다.

이 스크립트를 기본 설치 단계처럼 실행하지 않는다. 새 tokenizer는 같은 문자열을 기존 tokenizer와 다른 token ID로 매핑하므로 기존 MiniMind checkpoint의 embedding/LM head와 의미가 맞지 않는다. 재훈련이 연구 목적상 꼭 필요하다면 다음을 한 release 단위로 관리한다.

1. vocabulary 크기, 모든 special token과 chat template 계약을 먼저 고정한다.
2. 기존 checkpoint를 재사용하지 말고 새 tokenizer에 맞춰 모델을 처음부터 학습한다.
3. tokenizer 파일, model config, weight와 dataset revision/checksum을 함께 versioning한다.
4. encode/decode 왕복, assistant loss mask, tool-call template, 변환·서빙 runtime 호환성을 회귀 시험한다.

## 5. 데이터 계약

### Pretrain

한 줄에 `text`가 필요하다.

```json
{"text": "언어 모델은 다음 토큰을 예측한다."}
```

`PretrainDataset`은 special token을 직접 끈 tokenizer 결과에 BOS/EOS를 붙이고 고정 길이로 padding한다. label은 input 복사본이며 pad token만 `-100`이 된다 (`dataset/lm_dataset.py:37-55`). 즉 내용 전체가 next-token loss 대상이다.

### SFT와 LoRA/증류

```json
{"conversations":[{"role":"user","content":"RMSNorm을 설명해줘."},{"role":"assistant","content":"RMS 크기로 정규화하는 층입니다."}]}
```

SFT는 chat template를 문자열로 만든 뒤 assistant 시작/종료 token 사이만 label로 남긴다 (`dataset/lm_dataset.py:58-119`). system prompt가 없는 일반 샘플에는 확률적으로 system message가 추가되고, 빈 think tag도 확률적으로 제거될 수 있다. seed를 고정하지 않으면 같은 row도 전처리 결과가 달라질 수 있다.

LoRA와 white-box distillation도 같은 `SFTDataset`을 사용한다. tool-use 샘플에서는 system message의 `tools`, assistant message의 `tool_calls`가 JSON 문자열일 수 있으며 template에 전달된다.

### DPO

```json
{
  "chosen":[{"role":"user","content":"2+2?"},{"role":"assistant","content":"4"}],
  "rejected":[{"role":"user","content":"2+2?"},{"role":"assistant","content":"5"}]
}
```

`DPODataset`은 두 대화를 각각 template화하고 assistant 구간 mask를 만든다 (`122-192`). 학습은 policy/reference의 chosen-rejected log-ratio 차이를 `logsigmoid`로 최적화한다 (`trainer/train_dpo.py:25-50`). preference pair의 prompt 조건과 형식이 동일해야 비교가 의미 있다.

### RLAIF (PPO/GRPO)

`RLAIFDataset`은 마지막 assistant 답을 떼고 나머지를 generation prompt로 만든다. `thinking_ratio` 확률로 명시적 thinking template를 켠다 (`dataset/lm_dataset.py:195-224`). 실제 reward는 rollout 응답의 길이·형식·반복 패널티와 외부 reward model 점수를 결합한다.

### Agent RL

```json
{
  "conversations": [
    {"role":"system","content":"도구를 사용하라.","tools":"[{\"type\":\"function\",\"function\":{\"name\":\"calculate_math\",\"parameters\":{\"type\":\"object\"}}}]"},
    {"role":"user","content":"7의 제곱은?"},
    {"role":"assistant","content":"49"}
  ],
  "gt": ["49"]
}
```

`AgentRLDataset`은 마지막 message를 제외한 대화, tools, 검증 목표 `gt`를 반환한다 (`dataset/lm_dataset.py:226-252`). rollout은 `<tool_call>`을 파싱해 관찰을 context에 다시 넣고 최대 3 turn을 진행한다. prompt token에는 policy gradient를 주지 않고, tool observation도 response mask 0으로 둔다 (`trainer/train_agent.py:98-186`, `248-299`).

[JSONL 검사기](examples/validate_data.py)는 이 계약을 모델을 불러오지 않고 검사한다. 실제 tokenizer의 chat template 적합성은 별도 integration test가 필요하다.

## 6. 학습 단계 선택표

| 단계 | 시작 weight | Dataset | 핵심 목적/손실 | 주요 출력 기본 prefix |
|---|---|---|---|---|
| Pretrain | 없음 | `PretrainDataset` | 전체 text next-token CE | `pretrain` |
| Full SFT | `pretrain` | `SFTDataset` | assistant token CE | `full_sft` |
| LoRA | `full_sft` | `SFTDataset` | low-rank branch만 CE 학습 | `lora_medical` 등 |
| Distillation | student/teacher `full_sft` | `SFTDataset` | `α·CE + (1-α)·T²KL` | `full_dist` |
| DPO | `full_sft` + 고정 ref | `DPODataset` | chosen 선호 log-ratio | `dpo` |
| PPO | `full_sft` + ref + critic + reward model | `RLAIFDataset` | clipped actor + value + KL | `ppo_actor` |
| GRPO/CISPO | `full_sft` + ref + reward model | `RLAIFDataset` | 그룹 상대 advantage + KL | `grpo` |
| Agent RL | `full_sft` + ref + reward model | `AgentRLDataset` | trajectory/tool/GT reward + GRPO/CISPO | `agent` |

모든 단계를 순서대로 실행해야 하는 것은 아니다. 가장 작은 합리적 경로는 보통 `Pretrain → SFT → 목적별 한 단계`다.

### 실행 명령 지도

다음 명령은 CLI 연결 관계를 보여주는 출발점이다. `trainer/`에서 실행하며 해당 data와 parent weight가 이미 준비돼 있어야 한다. PPO, GRPO, Agent는 `--reward_model_path`의 reward model도 필수다.

```bash
cd trainer

# 1. 기반 모델
python train_pretrain.py --data_path ../dataset/pretrain_t2t_mini.jsonl
python train_full_sft.py --data_path ../dataset/sft_t2t_mini.jsonl --from_weight pretrain

# 2. 목적별 후속 단계
python train_lora.py --data_path ../dataset/lora_medical.jsonl --from_weight full_sft
python train_distillation.py --data_path ../dataset/sft_t2t_mini.jsonl \
  --from_student_weight full_sft --from_teacher_weight full_sft
python train_dpo.py --data_path ../dataset/dpo.jsonl --from_weight full_sft

# 3. online rollout 기반 단계
python train_ppo.py --data_path ../dataset/rlaif.jsonl \
  --from_weight full_sft --reward_model_path /trusted/reward-model
python train_grpo.py --data_path ../dataset/rlaif.jsonl --loss_type grpo \
  --from_weight full_sft --reward_model_path /trusted/reward-model
python train_agent.py --data_path ../dataset/agent_rl.jsonl \
  --from_weight full_sft --reward_model_path /trusted/reward-model
```

증류 기본 설정은 Dense student와 MoE teacher를 가정하므로 각각 `full_sft_768.pth`, `full_sft_768_moe.pth`가 필요하다. 다른 구조를 쓰면 student/teacher hidden, layer, MoE flag를 weight와 일치시킨다. 각 명령을 실행하기 전에 `python <script> --help`로 현재 revision의 옵션을 확인한다.

## 7. 각 학습 단계의 내부 동작

### 7.1 Pretrain과 SFT

두 script의 optimizer loop는 거의 같다. cosine 형태의 수동 LR, gradient accumulation, clipping, AMP scaler, periodic weight/checkpoint 저장, resume를 사용한다. 차이는 Dataset과 시작 weight다.

- Pretrain 기본: batch 32, accumulation 8, length 340, LR `5e-4`, 2 epochs
- Full SFT 기본: batch 16, accumulation 1, length 768, LR `1e-5`, 2 epochs

기본값은 현재 source의 출발점일 뿐 hardware/data에 대한 보편적 권장값이 아니다. 유효 batch는 대략 `per-device batch × accumulation × world size`로 기록한다.

### 7.2 LoRA

수작업 구현은 입력·출력 차원이 같은 `nn.Linear`에 rank 16의 `A`와 `B`를 붙인다. 원래 forward에 `B(A(x))`를 더하며 B를 0으로 초기화하므로 시작 시 원래 함수를 보존한다 (`model/model_lora.py:6-32`). trainer는 이름에 `lora`가 든 파라미터만 optimizer에 넘긴다.

주의할 점:

- 모든 linear가 아니라 정사각형 projection에만 주입된다.
- 별도 scaling `alpha/rank`와 LoRA dropout이 없다.
- 저장 파일은 LoRA state만 담고, 추론 때 동일 base weight가 필요하다.
- 병합은 `B.weight @ A.weight`를 base weight에 더한다 (`model/model_lora.py:56-65`). 병합 전후 logits 허용 오차를 검사한다.

### 7.3 지식 증류

teacher는 eval/frozen 상태다. assistant label mask 위치에서 student와 teacher의 token distribution KL을 계산하고 `T²`를 곱한다. 총 손실은 `alpha*CE + (1-alpha)*distill`이다 (`trainer/train_distillation.py:25-93`). student와 teacher vocabulary가 다르면 코드는 teacher logits를 student vocabulary 길이로 자른다. 단순 길이 절단만으로 token ID 의미가 일치하는 것은 아니므로 tokenizer/vocabulary alignment를 반드시 검증한다.

### 7.4 DPO

고정 reference와 학습 policy가 chosen/rejected를 모두 forward한다. assistant mask 위치의 log probability 합을 비교한다. `beta`는 reference로부터의 이탈과 선호 강도의 trade-off다. DPO는 정적 preference dataset을 반복 이용하기 쉽지만 online 탐색이나 정답 검증을 자체 제공하지 않는다.

### 7.5 PPO

현재 구현은 Actor, Critic, reference model, reward model을 사용한다. 현재 policy로 rollout한 뒤 response token의 old log probability와 value를 보관하고, GAE로 advantage/return을 계산해 minibatch를 여러 번 업데이트한다. actor ratio와 value 모두 clip하며 KL과 early-stop 신호를 본다. 모델을 여러 개 메모리에 두므로 가장 무거운 경로다.

### 7.6 GRPO와 CISPO

같은 prompt에서 `num_generations`개 답을 만들고 그룹 평균·표준편차로 reward를 정규화한다 (`trainer/train_grpo.py:122-125`). Critic이 필요 없지만 모든 답의 reward가 같으면 advantage가 거의 0이다. `--loss_type grpo`는 ratio clip, 기본 `cispo`는 상한을 둔 ratio를 detached weight로 사용한다 (`136-145`).

### 7.7 Agentic RL

Agent 경로는 행동 한 번이 아니라 `action → tool observation → action` trajectory를 최적화한다. reward에는 tool 형식·허용 이름/인자·GT 적중·미완료·반복·선택적 reward model이 반영된다 (`trainer/train_agent.py:194-245`). 이는 범용 agent runtime이 아니라 제한된 mock tool set에서 tool-use 학습을 보여주는 교육용 구현에 가깝다.

reward 계산 함수 자체는 reward model이 없는 호출도 처리하지만, 현재 `train_agent.py`의 CLI main은 `LMForRewardModel`을 무조건 생성한다 (`trainer/train_agent.py:441-447`). 따라서 그대로 실행할 때는 유효한 `--reward_model_path`가 필요하다.

`RolloutEngine`은 로컬 Torch와 HTTP SGLang을 같은 interface로 감싼다. 현재 학습은 rollout batch 후 update하는 동기식 구조다. SGLang 모드는 disk에 policy를 export하고 server의 weight update endpoint를 호출한다 (`trainer/rollout_engine.py:98-194`).

## 8. 생성 알고리즘

`MiniMindForCausalLM.generate`의 한 step은 다음 순서다 (`model/model_minimind.py:256-288`).

1. KV cache 이후의 새 token만 forward한다.
2. logits를 temperature로 나눈다.
3. 이미 나온 token에 repetition penalty를 적용한다.
4. top-k 미만 후보를 제거한다.
5. top-p 누적 확률 밖 후보를 제거한다.
6. multinomial sampling 또는 argmax로 다음 token을 고른다.
7. EOS가 나온 sequence를 완료 처리한다.

[샘플링 실습](examples/sampling_lab.py)은 같은 개념 순서를 작은 숫자 벡터로 보여준다. 실제 tokenizer나 neural logits를 재현하는 예제는 아니다.

## 9. 이해도 점검 실습

1. `model_config_check.py --hidden-size 512 --layers 12`를 실행해 파라미터 수가 어떻게 바뀌는지 설명한다.
2. `--hidden-size 770`이 경고와 함께 성공하고, 여기에 `--strict-canonical`을 붙이면 실패하는 이유를 projection shape로 설명한다.
3. `--kv-heads 3`이 GQA 반복 제약 때문에 항상 실패하는지 확인한다.
4. SFT 예제에서 assistant message를 제거하고 데이터 validator 오류를 읽는다.
5. sampling의 `top-p`를 1.0에서 0.5로 바꾸고 살아남는 후보 수를 비교한다.
6. GRPO에서 한 그룹 reward가 모두 같을 때 정규화 advantage를 손으로 계산한다.

다음: [고급 운영과 배포](03_advanced.md)
