<!-- rumdl-disable MD013 -->

# 03. 검증·성능·보안·배포

## 1. “실행됨”과 “학습됨”을 분리하기

검증은 다음 층으로 나눈다.

| 층 | 질문 | 최소 검증 |
|---|---|---|
| 정적 구성 | 차원과 파일 경로가 맞는가 | config validator, CLI manifest |
| 데이터 | schema/template/mask가 맞는가 | JSONL 전수 검사, token/mask 샘플 출력 |
| 단위 연산 | loss와 sampling이 기대대로인가 | 작은 고정 tensor의 수치 assertion |
| 한 batch | forward/backward/save/load가 되는가 | tiny model + batch 1 overfit |
| 단계 연결 | 이전 weight가 다음 단계에서 열리는가 | pretrain→SFT→eval artifact test |
| 품질 | 성능이 실제로 나아졌는가 | holdout benchmark, baseline 비교 |
| 운영 | 부하·오류·보안 요구를 만족하는가 | load, timeout, auth, abuse test |

loss 감소만으로 품질을 판단하지 않는다. data leakage, mask 오류, answer truncation, reward hacking이 있어도 loss/reward 곡선은 좋아 보일 수 있다.

## 2. 재현성 manifest

각 실험마다 다음을 함께 저장한다.

```yaml
code_revision: a3c7b01cc004d5de86aea961f20bf1e638e7c09e
model_config:
  hidden_size: 768
  num_hidden_layers: 8
  use_moe: false
tokenizer_revision: <sha-or-checksum>
dataset_sha256: <sha256>
seed: 42
world_size: 1
effective_batch: <batch * accumulation * world_size>
command: <full command without secrets>
runtime: <python/torch/cuda/driver/os>
parent_checkpoint_sha256: <sha256>
```

`setup_seed`는 Python, NumPy, Torch, CUDA seed와 cuDNN 설정을 건드린다 (`trainer/trainer_utils.py:54-61`). 그러나 multi-process data order, 일부 GPU kernel, 외부 rollout server까지 완전 결정적으로 만들지는 않는다. 결과 보고에는 평균·분산과 반복 횟수를 포함한다.

## 3. 데이터와 label mask 디버깅

학습 전에 임의 10개가 아니라 다음 경계 사례를 고른다.

- 가장 짧은/긴 row
- Unicode, 줄바꿈, code block, 수식 포함 row
- system/tool/tool_calls가 있는 row
- assistant가 여러 번 등장하는 multi-turn row
- max length 바로 아래/위 row
- DPO chosen/rejected의 공통 prompt가 다른 row
- Agent `gt`가 숫자, 문자열, 여러 값인 row

각 row에 대해 `token`, `decoded token`, `label`, `loss mask`를 나란히 출력한다. SFT에서 assistant 응답이 truncation 밖으로 밀리면 모든 label이 `-100`일 수 있다. DPO에서는 chosen/rejected의 mask 합과 prompt 조건을 비교한다. Agent에서는 tool observation token의 policy mask가 0인지 확인한다.

전처리 함수가 확률적으로 system prompt와 think tag를 바꾸므로, dataset 검사와 회귀 시험에서는 seed를 고정하거나 확률 변환을 주입 가능한 함수로 분리하는 개선을 고려한다.

## 4. 학습 안정성 관찰 지표

### 공통

- raw loss와 accumulation 보정 후 loss
- learning rate, gradient norm, skipped/overflow step
- token/s, samples/s, peak allocated/reserved memory
- padding ratio, 유효 label token 수, truncation 비율
- checkpoint 저장 시간과 resume 후 첫 batch 일치 여부
- Dense/MoE의 `aux_loss`와 expert load distribution

### DPO

- chosen/rejected margin
- policy-reference KL
- pair별 mask token 수
- 동일 응답·빈 응답·길이 편향 비율

### PPO/GRPO

- reward 평균뿐 아니라 분산과 quantile
- KL, ratio, clip fraction, response length
- PPO critic loss와 explained variance
- GRPO group reward std 및 degenerate group 비율
- reward model drift와 별도의 task 성공률

### Agent

- tool parse 성공률, 유효 tool name/arguments 비율
- tool 실행 성공·timeout 비율
- GT 적중률, unfinished episode 비율, 평균 turn 수
- reward 구성 요소별 값
- tool result가 실제 환경 outcome과 일치하는지

현재 logger가 모든 항목을 제공하지는 않는다. 새 metric을 추가할 때 raw prompt나 개인정보가 외부 추적 서비스로 전송되지 않도록 redaction을 먼저 설계한다.

## 5. 성능 최적화 순서

1. **측정 기준 고정**: 같은 prompt 길이, 생성 길이, batch, dtype으로 baseline을 잰다.
2. **padding 감소**: length bucketing과 동적 padding을 검토한다.
3. **batch/accumulation 조정**: 메모리 한계 안에서 유효 batch를 유지한다.
4. **attention 경로 확인**: mask/cache 조건 때문에 SDPA fast path에서 빠지는지 profiler로 확인한다.
5. **dtype 선택**: GPU 지원에 맞춰 bfloat16/float16을 선택하고 overflow를 본다.
6. **compile 검증**: `--use_compile 1` 전후 정확도와 compile warm-up 비용을 함께 잰다.
7. **DDP 확장**: single GPU 기준이 안정된 후 `torchrun`으로 늘린다.
8. **rollout 분리**: RL 생성이 병목일 때 SGLang을 검토하고 weight sync 시간을 포함해 측정한다.

KV cache는 generation 때 이전 K/V 재계산을 줄이지만 sequence 길이에 따라 메모리가 증가한다. `max_new_tokens=8192` 같은 기본 상한을 그대로 외부 요청에 허용하면 latency와 메모리 위험이 크다. 서비스에서는 입력 token과 출력 token을 각각 제한한다.

MoE는 활성 expert 수가 적어도 현재 native Python loop와 token dispatch 비용이 있다. 총 파라미터, 활성 파라미터, wall-clock, peak memory를 따로 보고 Dense 대비 이점을 실측한다.

## 6. 보안 검토

### 모델·checkpoint 공급망

- `torch.load`는 신뢰하지 않는 pickle 기반 checkpoint를 열면 위험할 수 있다. 출처·checksum을 검증하고 격리된 환경에서만 연다.
- 일부 경로는 `trust_remote_code=True`로 Transformers 모델을 로드한다. revision을 pin하고 code review 없는 원격 모델을 production process에서 실행하지 않는다.
- tokenizer/chat template도 실행 의미를 바꿀 수 있는 artifact로 보고 버전과 hash를 기록한다.

### API 서버

`scripts/serve_openai_api.py`는 인증·권한·rate limit·TLS 없이 `0.0.0.0:8998`에 bind한다 (`237-252`). 학습용 예제 그대로 인터넷에 노출하지 않는다.

배포 전 최소 보완:

- 내부 network 또는 loopback bind
- TLS termination과 API 인증
- request body, message 수, input/output token 상한
- concurrency queue, timeout, cancellation, backpressure
- structured audit log와 개인정보 redaction
- health/readiness, graceful shutdown, model warm-up
- abuse/content 정책과 비용 quota

`reasoning_content`를 client에 노출하는 것은 내부 추론·민감 데이터 유출 문제를 낳을 수 있다. 제품 요구가 명확하지 않으면 저장·전송하지 않는다.

### Tool execution

Agent trainer의 `calculate_math` mock은 builtins를 제거한 `eval`을 쓰지만 이것을 범용 sandbox로 간주하면 안 된다 (`trainer/train_agent.py:56-95`). 더구나 timeout 구현은 Unix의 `signal.SIGALRM`에 의존한다. Windows에는 `SIGALRM`이 없으므로 `signal.signal(signal.SIGALRM, ...)`에서 예외가 나고, 현재의 넓은 `except`가 이를 삼켜 mock tool 결과를 `None`으로 만든다. 즉 Windows에서는 이 경로가 계산을 실행하기도 전에 조용히 실패할 수 있다.

더 직접적인 위험도 있다.

- `scripts/eval_toolcall.py:29-37`의 `calculate_math`는 model이 만든 식을 제한 없는 process-level `eval`에 전달한다.
- `scripts/web_demo.py:124-146`도 model argument를 제한 없는 `eval`로 실행한다.
- `scripts/web_demo.py:149-195`, `217-225`, `323-411`은 사용자·모델·tool 문자열을 HTML fragment에 넣고 여러 곳에서 `unsafe_allow_html=True`로 렌더링한다. escape되지 않은 입력이 HTML/content injection 표면이 된다.

이 두 script의 tool 기능은 신뢰할 수 없는 prompt나 외부 사용자에게 그대로 노출하지 않는다. 산술식은 AST allowlist parser로 이름·연산자·숫자만 허용한다. timeout은 Windows와 Unix 모두에서 동작하도록 spawn한 별도 worker process에서 실행하고, 부모가 명시적 timeout 후 worker를 terminate하는 방식을 사용한다. thread timeout은 실행 중인 Python 코드를 강제로 멈추지 못하므로 격리 경계가 아니다. Streamlit 출력은 기본 text/Markdown component를 우선하고, HTML이 꼭 필요하면 모든 동적 값을 `html.escape` 또는 검증된 sanitizer로 처리한다.

production tool은 이와 함께 JSON Schema validation, 최소권한 process/container, CPU/메모리 제한, egress 제어와 결과 크기 제한을 적용한다.

모델이 만든 tool name과 arguments는 신뢰할 수 없는 입력이다. prompt injection이나 tool result injection을 가정하고, 읽기와 쓰기 tool을 분리하며 destructive operation에는 사용자 승인과 idempotency key를 둔다.

### 데이터

- 학습 전 개인정보·비밀·저작권·license provenance를 검사한다.
- train/eval 중복과 benchmark contamination을 탐지한다.
- 외부 telemetry에 raw prompt/answer/tool output을 보내지 않는다.
- 모델 출력과 tool trace가 log injection을 만들 수 있으므로 구조화하고 escape한다.

## 7. 배포 경로

### 경로 A: Transformers 디렉터리

native `.pth`를 Transformers 형식으로 바꾸는 함수는 `scripts/convert_model.py`에 있지만, 현재 script에는 변환 CLI가 없다. `__main__`은 `lm_config`, 입력 `../out/full_sft_768.pth`, 출력 `../minimind-3`을 하드코딩하고 변환 함수 안의 tokenizer도 `../model/`에서 읽는다 (`scripts/convert_model.py:16-28`, `40-87`, `128-134`). 이 상대 경로는 `scripts/`를 현재 작업 디렉터리로 삼을 때만 repository 내부의 해당 경로를 가리킨다. 저장소 루트에서 `python scripts/convert_model.py`를 그대로 실행하면 `../out`과 `../model`이 저장소 밖을 가리킨다.

따라서 이 script를 범용 명령처럼 그대로 실행하지 않는다. 먼저 `cd scripts`한 뒤에도 `lm_config`, 입력 weight, 출력 `minimind-3` 경로를 목적에 맞게 검토·편집하거나, 변환 함수를 import하는 작은 wrapper에서 절대 경로를 명시한다. 변환 전후에는 동일 prompt의 logits/generation 차이를 허용 오차로 비교한다. tokenizer와 generation config도 같은 release 묶음으로 배포한다.

### 경로 B: 내장 OpenAI 호환 API

학습·개발 검증에는 간단하지만 production에는 앞 절의 gateway 보완이 필요하다.

```bash
# 터미널 1, 저장소 루트
python scripts/serve_openai_api.py --load_from ./minimind-3 --device cuda

# 터미널 2, 저장소 루트: script가 실제 제공하는 API 옵션 사용
python scripts/eval_toolcall.py --backend api \
  --api_base_url http://localhost:8998/v1 \
  --api_key sk-local-placeholder --api_model minimind --stream 1
```

내장 server는 `0.0.0.0:8998`에 bind한다. 반면 `scripts/chat_api.py`는 CLI option 없이 `http://localhost:11434/v1`과 model `minimind-local:latest`를 하드코딩한다 (`scripts/chat_api.py:3-16`). 이는 Ollama 계열 endpoint를 겨냥한 별도 client 예제이므로 내장 server용 명령으로 그대로 실행하면 안 된다. 내장 server 검증에는 위처럼 실제 `eval_toolcall.py`의 `--api_base_url`, `--api_key`, `--api_model` 옵션으로 8998을 명시한다. server 자체에는 API-key 검증이 없어 예시 key는 client 초기화를 위한 placeholder일 뿐이다.

단, `eval_toolcall.py`에도 앞서 설명한 제한 없는 산술 `eval`이 있다. 위 명령은 해당 함수를 안전한 parser로 교체했거나 네트워크·권한이 제한된 폐기 가능한 로컬 환경에서만 사용한다. 실행하면 `[0] 자동 테스트`와 `[1] 수동 입력`을 묻는다. stream과 non-stream 응답, EOS, tool calls, malformed JSON, client disconnect를 각각 시험하되 외부에 공개하지 않는다.

### 경로 C: vLLM/SGLang/llama.cpp/Ollama/MNN

각 runtime이 요구하는 format으로 변환하고, 원본 PyTorch와 parity test를 먼저 수행한다. runtime 이름만 같다고 custom architecture와 chat template가 자동 호환되는 것은 아니다. quantization 시에는 크기·속도뿐 아니라 perplexity, task accuracy, tool format 준수율을 비교한다.

### Release artifact checklist

- immutable model/tokenizer revision과 checksum
- base/LoRA/merged 여부
- config와 context 제한
- 학습 data provenance 요약
- 평가 결과와 알려진 한계
- license와 third-party notice
- SBOM/의존성 lock
- rollback 가능한 이전 artifact

현재 `.gitignore`는 `out`만 제외하며 `dataset/`, `checkpoints/`, 변환된 `minimind-3/` 같은 모델 디렉터리를 자동으로 제외하지 않는다. release artifact를 만들기 전에 ignore/대용량 저장 정책을 명시하고, secret·민감 dataset·weight가 일반 Git commit에 포함되지 않았는지 `git status --short`와 staged file 목록으로 확인한다.

## 8. 테스트 전략

새 변경에는 가능한 한 다음을 추가한다.

```text
tests/
  test_config_shapes.py
  test_attention_cache.py
  test_dataset_masks.py
  test_loss_numerics.py
  test_lora_merge.py
  test_checkpoint_resume.py
  test_api_contract.py
  test_tool_sandbox.py
```

특히 가치가 큰 회귀 시험:

- cache 사용/미사용 다음-token logits 일치
- explicit attention/SDPA 결과 허용 오차 일치
- LoRA B=0에서 base logits 일치, merge 전후 일치
- checkpoint 직전/재개 직후 optimizer step 연속성
- DPO에서 policy=reference이면 예상 baseline loss
- GRPO reward가 모두 같을 때 NaN 없이 0에 가까운 advantage
- 빈 completion과 EOS 없는 completion mask
- malformed tool call이 실행되지 않음

가이드 예제의 smoke test는 이 중 정적 계약만 다룬다. neural numerical test는 PyTorch test suite로 별도 구성해야 한다.

## 9. 기여 경로

1. issue/discussion과 `CODE_OF_CONDUCT.md`를 읽고 변경 범위를 정한다.
2. 현재 기본 branch와 최신 upstream을 확인한다.
3. 모델 구조 변경은 config serialization, conversion, old checkpoint 호환성을 함께 설계한다.
4. 데이터 변경은 schema 예제와 label-mask test를 추가한다.
5. trainer 변경은 single-device, resume, DDP 경로를 분리해 검증한다.
6. benchmark에는 환경, seed, data revision, baseline, variance를 포함한다.
7. 문서 명령을 깨뜨리지 않았는지 clean environment에서 재실행한다.

좋은 첫 기여 후보는 requirements 누락 명시화, API request bounds/auth 가이드, dataset schema validation, LoRA parity test, cache/attention numerical tests, Windows tool-timeout portability다.

## 10. 다음 학습 경로

- 모델: GQA, RoPE/YaRN, RMSNorm, SwiGLU, KV cache
- 데이터: chat template, assistant-only loss mask, preference pair 품질
- 최적화: gradient accumulation, AMP, DDP, checkpoint consistency
- 정렬: DPO의 log-ratio, PPO의 GAE/critic, GRPO의 group baseline
- Agent: trajectory credit assignment, tool sandbox, delayed/verifiable reward
- 서빙: continuous batching, quantization, backpressure, observability

돌아가기: [가이드 홈](README.md) · [예제](examples/README.md)
