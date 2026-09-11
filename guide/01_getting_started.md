<!-- rumdl-disable MD013 -->

# 01. 설치와 첫 실행

이 장의 목표는 “환경을 설치했다”가 아니라, 실행 모드에 필요한 자원과 경로를 구분하고 가장 작은 검증을 통과하는 것이다.

## 1. 실행 모드 먼저 선택하기

| 모드 | GPU | weight/data | 권장 대상 |
|---|---:|---|---|
| 가이드 smoke test | 불필요 | 불필요 | 모든 사용자 |
| 공개 weight CLI 추론 | 선택 | Transformers 모델 디렉터리 | 처음 결과를 확인할 때 |
| LoRA 소규모 적응 | 선택 | base weight + SFT JSONL | 제한된 도메인 실험 |
| Pretrain/SFT 전체 학습 | 사실상 권장 | 대규모 JSONL | 학습 파이프라인 재현 |
| PPO/GRPO/Agent RL | 강하게 권장 | SFT weight + reward model/data | 고급 연구 |
| API/외부 runtime | 배포 규모에 따름 | 변환된 모델 | 서비스 통합 |

## 2. 격리 환경 만들기

저장소 README가 제시한 참조 환경은 Python 3.10 계열이다. `requirements.txt`는 버전을 강하게 고정하지만 PyTorch는 주석 처리되어 있으므로, CPU/CUDA/운영체제에 맞는 PyTorch를 먼저 별도로 선택해야 한다.

PowerShell:

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

Bash:

```bash
python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

전체 개발 환경은 호환되는 PyTorch를 설치한 다음 아래를 실행한다.

```bash
python -m pip install -r requirements.txt
```

주의할 점:

- `requirements.txt`의 `torch`가 주석이므로 설치가 자동 완결되지 않는다.
- API 스크립트는 `fastapi`와 `uvicorn`을 직접 import하지만 두 패키지가 requirements에 명시적으로 고정되어 있지 않다. API를 쓸 환경에서는 별도로 고정하고 lockfile 또는 container image로 재현성을 확보한다.
- 최신 Python을 무조건 선택하기보다 PyTorch와 pinned dependency가 함께 지원하는 버전을 택한다.
- 프로젝트별 가상환경을 사용해 다른 CUDA/PyTorch 조합과 섞이지 않게 한다.

## 3. 의존성 없이 먼저 검증하기

저장소 루트에서 다음을 실행한다.

```bash
python guide/examples/smoke_test.py
```

이 검사는 다음을 확인한다.

- 실제 `MiniMindConfig` 선언에서 핵심 기본값을 읽을 수 있는가
- 기본 Dense/MoE 파라미터 정적 추정치가 각각 약 64M/198M인가
- `hidden_size=770`, query heads 8인 비정규 구성을 실행 가능 경고로 처리하는가
- KV head 배수, 짝수 head dimension, expert top-k의 잘못된 구성을 강하게 거부하는가
- 다섯 가지 내장 JSONL 샘플과 실제 초소형 fixture가 계약을 만족하는가
- 동일 seed의 샘플링 결과가 재현되는가

정상 결과는 `11 checks passed`다. 실제 PyTorch forward나 학습 수렴을 확인하는 검사는 아니다.

## 4. GPU·backend 점검

PyTorch 설치 후 다음을 실행한다.

```bash
python -c "import torch; print(torch.__version__); print(torch.cuda.is_available()); print(torch.cuda.device_count())"
```

판단 기준:

- CUDA를 쓸 계획인데 `False`라면 학습 명령보다 driver/PyTorch wheel 조합을 먼저 해결한다.
- CPU에서 trainer는 autocast를 비활성화하지만 전체 학습은 매우 느릴 수 있다.
- DDP 초기화는 `RANK`가 있을 때 NCCL을 사용하므로, 일반 CPU multi-process DDP 용도로 바로 쓸 수 있는 구현은 아니다 (`trainer/trainer_utils.py:44-51`).
- `eval_llm.py`와 API 서버는 모델에 `.half()`를 적용한다 (`eval_llm.py:30`, `scripts/serve_openai_api.py:47`). CPU float16 kernel 호환성과 속도를 별도로 시험해야 한다.

## 5. 모델과 데이터 배치

### Transformers 형식 weight

저장소 루트에 예를 들어 `minimind-3/`를 두고 다음 파일이 있는지 확인한다.

```text
minimind-3/
  config.json
  tokenizer.json
  tokenizer_config.json
  model.safetensors 또는 pytorch_model.bin
```

다운로드한 모델이 신뢰 가능한 출처와 revision인지 확인하고 가능하면 checksum을 기록한다. Transformers 경로에서는 `trust_remote_code=True`가 사용되는 지점이 있으므로 임의 저장소의 model code를 실행하지 않는다.

### Native PyTorch weight

`eval_llm.py --load_from ./model --weight full_sft`는 기본적으로 다음 이름을 찾는다.

```text
out/full_sft_768.pth
```

MoE라면 `_moe` suffix가 붙는다. `hidden_size`, layer 수, Dense/MoE 여부가 checkpoint를 만든 구성과 일치해야 한다.

### Dataset

가이드에는 dataset class의 실제 계약에 맞춘 아주 작은 파일이 포함돼 있다.

```text
guide/examples/data/pretrain_tiny.jsonl  # 각 줄: {"text": "..."}
guide/examples/data/sft_tiny.jsonl       # 각 줄: {"conversations": [...]}
```

먼저 저장소 루트에서 이 fixture를 검사한다.

```bash
python guide/examples/validate_data.py guide/examples/data/pretrain_tiny.jsonl --kind pretrain
python guide/examples/validate_data.py guide/examples/data/sft_tiny.jsonl --kind sft
```

정식 dataset의 기본 경로는 trainer 디렉터리를 기준으로 `../dataset/...`이다. 실제 학습으로 확장할 때는 다음 파일을 별도로 준비한다.

```text
dataset/pretrain_t2t_mini.jsonl
dataset/sft_t2t_mini.jsonl
```

다운로드 전에 license, 개인정보, 재배포 조건을 확인한다. 현재 `.gitignore`는 `dataset/`과 `checkpoints/`를 자동 제외하지 않는다. 민감·대용량 파일을 내려받기 전에 ignore 정책을 추가하고, 다운로드 후에는 전체 학습보다 먼저 검사한다.

```bash
python guide/examples/validate_data.py dataset/pretrain_t2t_mini.jsonl --kind pretrain
python guide/examples/validate_data.py dataset/sft_t2t_mini.jsonl --kind sft
```

## 6. 최소 추론

Transformers 모델이 준비됐다면 저장소 루트에서 다음처럼 출력 길이를 작게 제한한다.

```bash
python eval_llm.py \
  --load_from ./minimind-3 \
  --device cpu \
  --max_new_tokens 64 \
  --temperature 0.7 \
  --top_p 0.9
```

Windows PowerShell에서는 줄 연속 문법 대신 한 줄로 실행하거나 backtick을 사용한다. 첫 prompt가 느린 것은 모델 load와 초기 kernel 준비가 포함되기 때문이다. 출력 품질 검증에는 고정 prompt 집합, seed, decoding 설정을 함께 기록한다.

`--inference_rope_scaling`은 위치 표현을 확장하는 옵션일 뿐, 학습하지 않은 장문 능력을 자동으로 만들어 주지는 않는다. CLI help에도 `max_new_tokens`가 실제 장문 능력을 뜻하지 않는다고 명시돼 있다.

## 7. 최소 학습 흐름

training script의 기본 상대 경로가 `trainer/` 기준이므로 그 디렉터리에서 실행한다.

```bash
cd trainer

# 1) 사전학습: 4행짜리 fixture로 코드 경로만 확인
python train_pretrain.py --device cpu --batch_size 1 --num_workers 0 \
  --max_seq_len 64 --hidden_size 128 --num_hidden_layers 2 \
  --epochs 1 --accumulation_steps 1 --log_interval 1 --save_interval 9999 \
  --data_path ../guide/examples/data/pretrain_tiny.jsonl \
  --save_weight guide_pretrain

# 2) SFT: 바로 앞에서 저장한 같은 구조의 weight를 사용
python train_full_sft.py --device cpu --batch_size 1 --num_workers 0 \
  --max_seq_len 64 --hidden_size 128 --num_hidden_layers 2 \
  --epochs 1 --accumulation_steps 1 --log_interval 1 --save_interval 9999 \
  --data_path ../guide/examples/data/sft_tiny.jsonl \
  --from_weight guide_pretrain --save_weight guide_sft
```

위 명령도 실제 PyTorch와 repository tokenizer가 필요하다. 4행 fixture를 한 epoch만 처리하는 코드 경로 점검이며 공식 모델 품질 재현이나 유의미한 학습이 아니다. 첫 명령의 최종 저장으로 `../out/guide_pretrain_128.pth`가 생기고, 두 번째 명령이 이를 읽는다. trainer는 최종 step에도 weight를 저장하므로 큰 `--save_interval`은 최종 저장을 막지 않는다.

학습 중 `../checkpoints/guide_*_resume.pth`도 생성될 수 있다. 현재 `.gitignore`는 `out`만 제외하고 `checkpoints/`는 제외하지 않으므로 commit 전에 반드시 상태를 확인한다.

## 8. checkpoint와 resume

trainer는 두 종류를 저장한다.

- `out/<weight>_<hidden>[_moe].pth`: 추론용 half state dict
- `checkpoints/<weight>_<hidden>[_moe]_resume.pth`: model, optimizer, epoch, step 및 부가 state

`--from_resume 1`은 resume 파일을 찾아 이어간다. `SkipBatchSampler`가 이미 처리한 batch를 건너뛰며, 저장된 world size와 현재 world size가 다르면 step을 조정한다 (`trainer/trainer_utils.py:63-116`, `134-157`). 재현 가능한 resume를 위해 code revision, dataset checksum, tokenizer, 전체 CLI, world size도 별도 manifest에 남긴다.

## 9. 자주 만나는 오류

| 증상 | 확인할 것 |
|---|---|
| `ModuleNotFoundError: torch` | requirements가 torch를 설치하지 않는다. 플랫폼에 맞는 wheel을 먼저 설치한다. |
| weight file not found | 실행 디렉터리, `--save_dir`, `--weight`, `--hidden_size`, `_moe` suffix를 확인한다. |
| state dict size mismatch | checkpoint와 config의 hidden/layer/head/MoE 구성이 다른지 본다. |
| CUDA OOM | sequence/batch/생성 수를 먼저 줄이고 gradient accumulation으로 유효 batch를 보완한다. |
| SFT loss가 거의 0 또는 NaN | assistant label mask가 실제로 1인지, truncation이 답변을 잘랐는지 검사한다. |
| GRPO advantage가 거의 0 | 한 그룹의 reward 분산이 0에 가까운 degenerate group인지 확인한다. |
| API import 실패 | `fastapi`, `uvicorn`을 명시적으로 설치·고정한다. |
| CPU 추론 연산 미지원 | 현재 `.half()` 경로를 확인하고, 검증된 float32/bfloat16 경로를 별도 구현·시험한다. |

다음: [모델과 학습 파이프라인](02_core_concepts.md)
