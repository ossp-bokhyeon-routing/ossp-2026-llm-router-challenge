<!--
SPDX-FileCopyrightText: Copyright 2026 ossp-bokhyeon-routing contributors
SPDX-License-Identifier: Apache-2.0
-->

# AGENTS.md

이 파일은 저장소 전체에 적용되는 코딩 에이전트 지침이다. 목표는 2026 오픈소스
개발자대회 SK텔레콤 지정과제용 라우터를 규칙에 맞게 구현하고, 공개 데이터에서
품질을 높이면서도 비공개 평가의 비용 한도를 안전하게 지키는 것이다.

공식 규칙과 이 파일이 충돌하면 다음 문서를 우선한다.

1. `docs/CHALLENGE_RULES.md`
2. `docs/SCORING.md`
3. `docs/RUNTIME.md`
4. `docs/ENFORCEMENT.md`

## 작업 시작 전 확인

- 항상 `git status --short --branch`로 사용자 변경을 먼저 확인하고 보존한다.
- 이 파일이 처음 추가되기 직전 저장소는 공식 배포 커밋 하나만 가진 스타터
  상태였다. 이후 상태는 현재 Git 이력과 파일을 기준으로 다시 판단한다.
- 현재 공식 컨테이너 경로는 `container/entrypoint.py`에서
  `ossp_router.bert_router.main`을 실행한다. 이 진입점은 패키지 자원에 고정한
  hash/character/word/tiny-BERT와 public-cost artifact를 별도 인자 없이 로드한다.
- 초기 `src/ossp_router/heuristic.py`는 의도적으로 약한 참조 구현이며
  `axk1-think`를 선택하지 않는다.
- 더 강한 학습형 예제는 `baselines/hash_regex.py`와
  `baselines/hash-regex-public.v1.json`에 있지만 초기 런타임 이미지에는 포함되지
  않는다.
- `configs/routing-policy.v1.json`, 공개 outcome, 공식 schema와 scorer를 성능을
  좋아 보이게 만들 목적으로 수정하지 않는다.

## 과제의 정확한 문제 정의

이 과제는 실시간 LLM 게이트웨이나 답변 생성 시스템이 아니다.

- 입력: 한 등급의 전체 `InputBatch`, 즉 `prompt` 또는 `messages`와 실행 tier
- 출력: 모든 문항에 대한 `episode_id`와 선택한 `model_id`
- 후보 모델: `ax31-light`, `ax31`, `axk1-think`
- tier: `fast`, `balanced`, `premium`
- 실행 중 후보 모델 호출, 답변 확인, 답변 비교, 재시도 후 승급은 없다.
- 운영자가 선택 결과와 사전 계산 outcome을 결합해 오프라인으로 채점한다.

라우팅 함수는 개념적으로 다음 흐름을 따라야 한다.

```text
prompt/messages
  -> 콘텐츠 특징
  -> 모델별 예상 품질과 위험 조정 비용
  -> tier별 배치 예산 최적화
  -> model_id 하나
```

## 절대 지켜야 하는 공정성 규칙

모델 선택에 사용할 수 있는 것은 현재 문항의 내용, `messages`의 공개 구조와
현재 tier뿐이다.

사용 가능:

- prompt 또는 message content에서 직접 계산한 길이, 언어, 정규식 특징
- 문자/단어 n-gram, feature hashing, TF-IDF, 로컬 임베딩
- 공개 Train/Dev로 미리 학습한 전역 계수, 어휘, 분류기와 비용 모델
- 공개 프롬프트의 정확 일치 조회나 콘텐츠 해시
- 일반 사전, 토크나이저, 허용 라이선스의 재배포 가능한 소형 로컬 모델

사용 금지:

- `challenge_id`, `split`, `episode_id`, 입력 위치나 순서
- 과제명, 원천 데이터셋명 등 런타임에 제공되지 않는 메타데이터
- 비공개 평가 자료, 평가 실행 결과 또는 유출된 대응표
- 네트워크, 외부 API, 외부 추론 서비스와 평가용 후보 모델 직접 실행
- 여러 모델의 답변 비교, 선택 후 재시도 또는 모델 변경
- 비공개 패키지, 비공개 Git submodule, 접근 제한 다운로드 경로

같은 콘텐츠와 같은 tier는 ID와 순서를 바꿔도 같은 모델을 선택해야 한다.
동률을 배열 위치로 깨지 않는다. 고정 모델 순서, 동률 그룹 또는 콘텐츠 기반의
안정 해시를 사용한다. 프로세스마다 달라질 수 있는 Python 내장 `hash()`를
결정 규칙에 사용하지 않는다.

## 점수와 비용

기준 모델은 `ax31-light`이고, 같은 평가 split의 모든 문항을 Light로 선택한
총비용을 1.0으로 둔다.

| Tier | 최대 비용 비율 | 최종 가중치 |
| --- | ---: | ---: |
| Fast | 1.25 | 0.4 |
| Balanced | 2.0 | 0.3 |
| Premium | 4.0 | 0.3 |

한도를 반올림 전 값으로 조금이라도 넘으면 해당 tier 전체 점수가 0이다. 예산은
문항별 한도가 아니라 입력 배치 전체의 총비용 제약이다. Fast의 가중치가 가장
크므로 Fast 예산 통과를 가장 먼저 보장한다.

모델 비용 계수는 다음과 같다.

| 모델 | 입력 토큰 계수 | 출력 토큰 계수 |
| --- | ---: | ---: |
| `ax31-light` | 1 | 4 |
| `ax31` | 2.127 | 8.509 |
| `axk1-think` | 6.565 | 26.260 |

비용은 각 outcome에 이미 합산된 `input_tokens`와 `output_tokens`로 계산한다.
`num_generations`를 다시 곱하지 않는다. 런타임에는 실제 출력 토큰 수가
제공되지 않으므로 모델별 출력 길이 또는 log-cost를 프롬프트에서 예측해야 한다.

공개 Dev 참고값은 다음과 같다. 이 값은 비공개 성능 보장이 아니라 회귀 방지와
방향 확인용이다.

| 정책 | Fast | Balanced | Premium | 가중 점수 |
| --- | ---: | ---: | ---: | ---: |
| all-light | 0.619318 / 1.000000 | 0.619318 / 1.000000 | 0.619318 / 1.000000 | 0.619318 |
| 초기 prompt heuristic | 0.625852 / 1.072334 | 0.658239 / 1.367866 | 0.691761 / 2.102044 | 0.655341 |
| 공개 hash-regex | 0.663068 / 1.235989 | 0.693750 / 1.961506 | 0.740057 / 3.985205 | 0.695369 |

공개 hash-regex는 세 tier 모두 한도에 너무 가깝다. Premium은 별도 사전검증에서
비용 비율이 약 4.2로 올라 0점이 된 사례가 `baselines/README.md`에 있다. 제공
artifact의 안전계수를 그대로 최종 정책으로 사용하지 않는다.

## 권장 구현 방향

가장 짧고 안전한 구현 경로는 공개 hash-regex 구조를 실제 런타임으로 통합하고,
비용 안전성과 검증 방식을 강화하는 것이다.

### 특징

- 문자·단어 unigram/bigram hashing
- 문자, 단어, 문장과 message 수
- 한국어/영어 비율과 문자 종류
- 수학, 숫자, 코드, 형식 추론, 논리 규칙, 객관식 표지
- 장문 길이와 긴 문맥 내 질의 위치
- system/user/assistant 역할 및 message 경계
- 단순 변환 문제와 다중 제약 문제 표지

특징 추출은 긴 입력에서도 선형 시간과 제한된 메모리로 동작해야 한다. 정책의
32,768 context 값은 outcome 생성 조건이지 라우터 입력을 자르는 강제 한도가
아니다.

### 예측 목표

- 모델별 절대 품질만 예측하지 말고 Light 대비 품질 증분도 함께 검토한다.
- 모델별 `score`와 `log-cost` 또는 출력 토큰 길이를 별도 head로 예측한다.
- K1 비용에는 평균보다 상위 분위수, UCB 또는 보수적 보정값을 우선한다.
- 무거운 모델이 항상 더 좋은 것은 아니므로 난이도 하나만 예측하는 3단계
  분류기로 문제를 축소하지 않는다.

### 선택 최적화

- Light를 안전한 기본 선택으로 둔다.
- 예상 품질 증가가 양수인 `Light -> AX31`, `Light/AX31 -> K1` 후보를 만든다.
- `예상 품질 증가 / 위험 조정 추가비용` 또는 Lagrangian 목적함수로 배치 전체를
  선택한다.
- tier마다 별도 penalty 또는 임계값을 사용하되 같은 tier에서 ID와 순서에
  독립적이어야 한다.
- K1은 예상 품질 증가의 확신이 높고 추가비용 대비 효율이 좋은 문항에만
  제한한다. Fast에서는 거의 사용하지 않는 정책부터 검증한다.
- 최적화 실패나 비정상 예측값에는 전체 Light 같은 결정적인 안전 fallback을
  사용한다.

공개 Dev에서 시작할 비용 목표 범위는 Fast `1.18~1.20`, Balanced
`1.75~1.85`, Premium `3.4~3.6` 정도다. 이는 공식 기준이 아닌 초기 안전
범위이며, bootstrap과 최악 검증 그룹 결과에 따라 더 낮춰야 한다.

## 학습과 검증 원칙

- Train은 모델 계수 학습, Dev는 안전계수와 최종 정책 보정에 사용한다.
- 단순 행 번호 modulo 또는 무작위 split만 신뢰하지 않는다.
- 수학, 코드, 한국어 객관식, 논리, 장문 QA 등 콘텐츠 기반 그룹 holdout과
  leave-one-group-out 검증을 수행한다.
- 공개 출처 정보는 검증 그룹을 만드는 데 사용할 수 있지만 런타임 특징이나
  조회 키로 포함하지 않는다.
- 평균 fold뿐 아니라 최악 fold의 비용 통과와 점수를 기록한다.
- 실제 비용, 출력 길이 또는 배치 구성의 분포 이동을 bootstrap으로 점검한다.
- 정확 프롬프트 lookup은 허용되지만 비공개 일반화를 대신하는 핵심 전략으로
  삼지 않는다.
- 학습 seed, 입력 해시, feature version, 계수, 안전계수와 생성 명령을
  재현 가능하게 기록한다.
- 최종 이미지에는 공개 원문/outcome 전체보다 필요한 전역 계수와 artifact만
  포함한다.

## 코드 경계와 파일 배치

- `src/ossp_router/protocol.py`: 공식 입력·출력 parser. 특별한 계약 버그가
  아니면 변경하지 않는다.
- `src/ossp_router/scoring.py`: 공식 Decimal scorer. 라우터 점수를 높이기 위해
  변경하지 않는다.
- `src/ossp_router/runtime.py`, `orchestrator.py`: 운영자 실행 하네스. 참가
  라우터 구현과 분리한다.
- `src/ossp_router/heuristic.py`: 초기 실제 라우터 진입점. 교체하거나 얇은
  호환 wrapper로 유지할 수 있다.
- `baselines/hash_regex.py`: 경량 학습형 라우터의 우선 출발점이다.
- `baselines/train_hash_regex.py`: 학습과 calibration 참고 구현이다.
- 학습 artifact는 `src/ossp_router/resources/` 같은 패키지 자원에 고정해 공식
  호출이 별도 `--artifact` 인자 없이 동작하게 한다.
- `container/entrypoint.py`는 공식 `--input`, `--tier`, `--output` 계약을 그대로
  보존해야 한다.
- `container/Dockerfile`이 새 코드와 artifact를 실제로 복사하는지 확인한다.
- 최종 출력은 같은 디렉터리의 임시 파일에 완전히 쓴 뒤 원자 교체하고,
  출력 볼륨 루트에는 `submission.json` 하나만 남긴다.

## 공식 런타임 한도

| 항목 | 한도 |
| --- | ---: |
| 플랫폼 | `linux/arm64` |
| CPU | 2코어 |
| 메모리 | 2 GiB, 추가 swap 없음 |
| 프로세스·스레드 | 합계 32개 |
| 실행 시간 | tier별 90초 |
| `/tmp` | 256 MiB |
| 출력 볼륨 | 4 MiB, inode 64개 |
| OCI 압축 계층 | 합계 1 GiB |
| 병합 root filesystem | 2 GiB |

네트워크와 GPU는 없고 root filesystem은 읽기 전용이다. Dockerfile에 `VOLUME`을
선언하지 않는다. 실행 중 다운로드하지 않는다. NumPy/BLAS 같은 런타임을
포함한다면 ARM64 지원과 이미지 크기를 확인하고 내부 스레드 수를 1로 제한한다.
가능하면 학습은 NumPy 등으로 수행하되 런타임은 표준 라이브러리와 내보낸
계수만 사용한다.

## 필수 테스트 게이트

변경 후 최소한 다음을 확인한다.

1. protocol, scoring, prompt router와 새 라우터 단위 테스트
2. 같은 입력 반복 실행의 byte-level 결정성
3. episode ID와 입력 순서를 바꾼 감사 입력에서 콘텐츠별 선택 불변성
4. 문항 누락·중복·추가 없이 모든 ID를 정확히 한 번 출력
5. Train/Dev 각 tier의 실제 score, 비용 비율, 모델별 선택 수
6. bootstrap과 콘텐츠 그룹별 최악 비용 비율
7. `linux/arm64` 이미지의 전체 Train+Dev 실행 시간과 메모리
8. 네트워크 없음, 읽기 전용 root, 비특권 UID 조건

핵심 단위 테스트 예시:

```console
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_protocol.py'
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_scoring.py'
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_prompt_heuristic.py'
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*baseline.py'
```

전체 Linux 테스트:

```console
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
```

최종 이미지 검사:

```console
PYTHONPATH=src python3 tools/check_runtime.py \
  --image my-router:check \
  --report build/runtime-check-report.json
```

Windows 네이티브 실행에서는 `fcntl`, `resource`, FIFO, symlink 권한 등 POSIX
테스트가 실패할 수 있다. 또한 `core.autocrlf=true`인 checkout은 고정 파일의
raw SHA-256 검증을 깨뜨릴 수 있다. 파일 내용을 임의로 수정해 예상 해시에
맞추지 말고, 최종 materialization·전체 테스트·이미지 빌드는 LF를 보존하는
WSL/Linux 새 clone에서 수행한다.

## 라이선스와 artifact

- 참가자가 작성한 전체 소스는 허용된 OSI 라이선스로 공개한다.
- 라이브러리, 토크나이저, 모델과 학습 파일의 이름, 버전/revision, URL,
  SHA-256, 용도와 라이선스를 기록한다.
- 이미지에 포함하는 모델은 최소 open-weight여야 하며 상업적 이용, 재배포,
  변형과 평가 목적 사용이 허용되어야 한다.
- 변환한 artifact는 원본과 결과 해시, 변환 명령과 옵션을 기록한다.
- 저장소에 API key, 토큰, 비공개 URL이나 민감한 평가 자료를 커밋하지 않는다.

## 실패 기록과 다음 작업 선택

실패·중단·부분 성공의 영구 기록은 저장소 루트 `EXPERIMENT_LOG.md`에 남긴다.
이 기록은 같은 실패를 반복하지 않고 다음 에이전트가 다른 가설이나 독립 작업을
선택하기 위한 작업 메모리다.

작업 시작 시:

1. `EXPERIMENT_LOG.md`의 기존 항목을 먼저 읽는다.
2. 현재 작업과 같은 특징, 모델, 안전계수, 환경 또는 오류가 있었는지 찾는다.
3. 관련 기록이 있으면 이번 시도가 무엇을 다르게 하는지 작업 전에 명시한다.
4. 차이가 없다면 같은 시도를 반복하지 말고 기록된 다음 대안을 선택한다.

다음과 같은 물질적 실패는 방향을 바꾸기 전에 반드시 기록한다.

- tier 예산 초과, 점수 회귀 또는 최악 검증 그룹의 큰 성능 저하
- protocol, 결정성, ID·순서 불변성 또는 핵심 테스트 실패
- 시간, 메모리, PID, 이미지 크기나 artifact 로딩 실패
- 의존성, ARM64, 라이선스 또는 데이터 materialization 문제
- 가설을 반박해 해당 접근을 중단하거나 되돌린 경우

단순 명령 오타처럼 전략적 정보가 없는 일회성 실수는 기록하지 않아도 된다.
같은 환경 문제가 반복되거나 원인 구분에 도움이 되면 기록한다.

각 기록에는 최소한 다음을 포함한다.

- 고유 ID, 시각과 상태(`failed`, `partial`, `discarded`, `resolved`)
- 목표와 검증하려던 가설
- 변경한 코드·artifact·설정과 기준 버전
- 실행 명령, 데이터 split/hash와 환경
- tier별 score, 비용 비율, 모델 선택 수 또는 실패한 테스트
- 관찰된 현상과 로그·보고서 경로
- 확인된 원인과 아직 추정인 원인을 구분한 분석
- 되돌림 또는 현재 작업 트리 상태
- 반복하지 말아야 할 조건과 다음 권장 대안

기존 실패 항목을 삭제하거나 성공한 것처럼 고쳐 쓰지 않는다. 나중에 해결하면
원래 ID를 참조하는 `resolved` 항목을 새로 추가한다. 큰 원시 로그나 비밀정보는
커밋하지 말고 재현 가능한 핵심 출력만 요약한다.

실패 유형별 기본 전환 방향은 다음과 같다.

- 예산 실패: 안전계수 하향, cost UCB 강화, K1 선택 축소
- 일반화 실패: 콘텐츠 그룹 holdout, 규제 강화, 특징 단순화 또는 새 특징 검증
- 런타임 실패: 프로파일링, artifact/의존성 축소, 순수 Python 경로 검토
- protocol/결정성 실패: 모델 개선을 멈추고 계약 위반부터 수정
- 환경 실패: WSL/Linux 등 공식 조건에 가까운 환경으로 옮기고, 그동안 독립적인
  단위 테스트·데이터 분석·문서화를 진행

한 작업이 막혀도 안전하게 독립적인 다른 작업이 남아 있으면 계속 진행한다.
다음 에이전트는 참고한 실패 ID와 선택한 대안을 작업 결과에 함께 남긴다.

## 에이전트 작업 원칙

- 사용자의 기존 변경과 생성 파일을 덮어쓰거나 되돌리지 않는다.
- 작업 전에 `EXPERIMENT_LOG.md`를 읽고, 물질적 실패 후에는 새 항목을 append한다.
- 먼저 가장 작은 유효한 개선을 구현하고, 점수보다 규칙 준수와 예산 통과를
  우선한다.
- 테스트를 약화하거나 scorer·policy·outcome을 바꿔 실패를 숨기지 않는다.
- 새 의존성은 성능 이득, ARM64 지원, 크기와 라이선스 근거가 있을 때만 추가한다.
- 라우팅 정책을 바꾸면 세 tier의 score, 실제 비용 비율, 모델 선택 수와 최악
  검증 결과를 함께 보고한다.
- 런타임 경로나 artifact 위치가 바뀌면 이 파일의 현재 상태 설명도 갱신한다.
- 작업 완료를 주장하기 전에 Linux 전체 테스트와 컨테이너 검증 여부를 명확히
  구분해 보고한다.
