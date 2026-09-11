<!-- rumdl-disable MD013 -->

# CPU 전용 예제

이 예제들은 MiniMind의 현재 코드 계약을 작게 관찰하기 위한 학습 도구다. Python 3.10 이상 표준 라이브러리만 사용하며 GPU, network, model weight가 필요 없다.

저장소 루트에서 실행한다.

```bash
python guide/examples/model_config_check.py
python guide/examples/validate_data.py --kind all
python guide/examples/sampling_lab.py --seed 42 --top-k 4 --top-p 0.8
python guide/examples/smoke_test.py
```

## 파일별 목적

| 파일 | 학습 내용 | 실제 코드와의 연결 |
|---|---|---|
| `model_config_check.py` | head/expert 차원 검사와 Dense/MoE 파라미터 추정 | `model/model_minimind.py` |
| `validate_data.py` | Pretrain/SFT/DPO/RLAIF/Agent JSONL schema 검사 | `dataset/lm_dataset.py` |
| `sampling_lab.py` | temperature → repetition → top-k → top-p → 선택 | `MiniMindForCausalLM.generate` |
| `smoke_test.py` | 위 예제의 deterministic regression checks | 가이드 예제 전체 |
| `data/pretrain_tiny.jsonl` | 4행짜리 Pretrain CPU fixture | `PretrainDataset`의 `text` 계약 |
| `data/sft_tiny.jsonl` | 4행짜리 SFT CPU fixture | `SFTDataset`의 `conversations` 계약 |

`smoke_test.py`의 정상 결과는 `11 checks passed`다. fixture는 실행 경로 검사용이며 학습 품질을 만들 만큼 크지 않다.

## 구성 검사 모드

MiniMind의 projection은 `hidden_size`가 query head 수로 정확히 나뉘지 않아도 실행할 수 있다. 검사기는 이를 호환성 경고로 보고 기본값에서는 성공시킨다. 배포·변환에 쓸 정규 구성을 강제할 때만 `--strict-canonical`을 사용한다.

```bash
# 성공(exit 0)하되 query projection 폭 768과 residual 폭 770 차이를 경고
python guide/examples/model_config_check.py --hidden-size 770

# 같은 경고를 오류로 승격(exit 1)
python guide/examples/model_config_check.py --hidden-size 770 --strict-canonical
```

KV head 배수, RoPE에 필요한 짝수 head dimension, MoE expert top-k 위반은 항상 오류다.

## 실제 파일 검사

```bash
python guide/examples/validate_data.py dataset/my_pretrain.jsonl --kind pretrain
python guide/examples/validate_data.py dataset/my_sft.jsonl --kind sft
python guide/examples/validate_data.py dataset/my_dpo.jsonl --kind dpo
python guide/examples/validate_data.py dataset/my_rlaif.jsonl --kind rlaif
python guide/examples/validate_data.py dataset/my_agent.jsonl --kind agent
```

포함된 fixture를 확인하려면 다음을 실행한다.

```bash
python guide/examples/validate_data.py guide/examples/data/pretrain_tiny.jsonl --kind pretrain
python guide/examples/validate_data.py guide/examples/data/sft_tiny.jsonl --kind sft
```

validator는 JSON을 읽고 구조만 검사한다. tokenizer를 실행하거나 tool call을 실행하지 않으며 파일을 수정하지 않는다. schema를 통과해도 chat template 결과, token 길이, loss mask, 학습 품질이 보장되는 것은 아니다.

## 변형 과제

1. hidden size와 layer 수를 바꾸고 attention/FFN/embedding의 비중을 비교한다. `770`에서 기본 경고와 strict 실패의 차이도 확인한다.
2. SFT demo에서 assistant turn을 지워 validator 오류를 확인한다.
3. top-k를 0, 1, 3으로 바꿔 후보 집합을 비교한다.
4. 같은 seed와 다른 seed의 선택 token을 비교한다.
5. sampling 예제의 `--greedy` 결과가 seed와 무관한지 확인한다.
