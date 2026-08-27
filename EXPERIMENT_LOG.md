<!--
SPDX-FileCopyrightText: Copyright 2026 ossp-bokhyeon-routing contributors
SPDX-License-Identifier: Apache-2.0
-->

# Routing Experiment Log

라우팅 실험의 실패, 중단, 부분 성공과 해결 내역을 시간순으로 누적한다. 다음
작업자는 먼저 이 파일을 읽고, 기존 시도와 무엇이 다른지 확인한 뒤 작업한다.

## 기록 규칙

- 항목은 append-only로 유지한다. 과거 실패를 삭제하거나 덮어쓰지 않는다.
- 사실, 측정값과 추정을 분리한다.
- score만 쓰지 말고 tier별 비용 비율과 모델 선택 수도 함께 남긴다.
- 실패한 변경을 되돌렸더라도 무엇을 시도했는지 기록한다.
- 해결은 원래 실패 ID를 참조하는 새 `resolved` 항목으로 기록한다.
- 비밀정보, 비공개 평가 자료와 큰 원시 로그는 기록하지 않는다.

## 항목 템플릿

```markdown
### EXP-YYYYMMDD-NNN — 짧은 제목

- 시간: YYYY-MM-DD HH:MM KST
- 상태: failed | partial | discarded | resolved
- 관련 기록: 없음 | EXP-...
- 목표/가설:
- 기준 버전: commit 또는 작업 트리 설명
- 변경 사항:
- 데이터/환경:
- 실행 명령:
- 결과:
  - Fast: score / cost ratio / model counts
  - Balanced: score / cost ratio / model counts
  - Premium: score / cost ratio / model counts
- 실패 증거:
- 원인:
  - 확인됨:
  - 추정:
- 되돌림/현재 상태:
- 반복 금지 조건:
- 다음 권장 대안:
```

테스트나 환경 실패는 관련 없는 tier 칸을 `N/A`로 기록하고 실패한 테스트명,
종료 코드와 핵심 오류 메시지를 결과에 적는다.

## 기록

### EXP-20260826-001 — 공개 hash-regex의 예산 여유 부족

- 시간: 2026-08-26 KST
- 상태: discarded
- 관련 기록: 없음
- 목표/가설: 제공된 hash-regex artifact를 그대로 사용해도 비공개 평가에서 세
  tier 예산을 통과할 수 있다.
- 기준 버전: 공식 스타터의 `baselines/hash-regex-public.v1.json`
- 변경 사항: 코드 변경 없음. 공개 baseline 보고서와 공식 사전검증 경고를 검토.
- 데이터/환경: 공개 Dev 880문항과 `baselines/hash-regex-public-dev-report.v1.json`
- 실행 명령: 기존 공개 보고서 재검토
- 결과:
  - Fast: `0.663068 / 1.235989`, Light 543 / AX31 335 / K1 2
  - Balanced: `0.693750 / 1.961506`, Light 372 / AX31 446 / K1 62
  - Premium: `0.740057 / 3.985205`, Light 279 / AX31 409 / K1 192
- 실패 증거: `baselines/README.md`는 Premium이 별도 사전검증에서 약 `4.2`로
  올라 한도 `4.0`을 초과하고 tier 점수가 0이 된 사례를 명시한다.
- 원인:
  - 확인됨: 공개 Dev의 모든 tier가 이미 한도에 매우 가깝다.
  - 추정: 출력 길이와 데이터 구성의 분포 이동을 cost 회귀와 Dev calibration이
    충분히 흡수하지 못했다.
- 되돌림/현재 상태: 제공 artifact를 안전한 최종 정책으로 간주하지 않는다.
- 반복 금지 조건: 동일 artifact와 동일 안전계수로 다시 평가하지 않는다.
- 다음 권장 대안: 실제 목표 비용을 Fast `1.18~1.20`, Balanced `1.75~1.85`,
  Premium `3.4~3.6`부터 검토하고, K1 cost UCB와 콘텐츠 그룹 최악값으로
  안전계수를 다시 보정한다.

### EXP-20260826-002 — Windows 네이티브 전체 테스트의 환경 실패

- 시간: 2026-08-26 KST
- 상태: partial
- 관련 기록: 없음
- 목표/가설: Windows 네이티브 Python에서 전체 공식 테스트를 그대로 검증할 수
  있다.
- 기준 버전: 공식 스타터, Python 3.13, `core.autocrlf=true`
- 변경 사항: 코드 변경 없음.
- 데이터/환경: Windows PowerShell, 현재 working tree
- 실행 명령: `$env:PYTHONPATH = 'src'; python -B -m unittest discover -s tests -p 'test_*.py'`
- 결과:
  - 전체: 101 tests, 7 failures, 9 errors, 1 skipped
  - 핵심 protocol/scoring/prompt/hash·feature baseline: 42 tests passed
  - 주요 오류: `fcntl`, `resource`, `os.mkfifo`, symlink 권한 부재
  - raw hash 실패: checkout의 CRLF와 Git blob의 LF 차이
- 실패 증거: POSIX 전용 모듈 import 오류와 LICENSE·policy·data 파일 SHA 불일치.
- 원인:
  - 확인됨: 공식 하네스 일부는 POSIX 전용이고 현재 checkout은 CRLF 변환 상태다.
  - 추정: 없음.
- 되돌림/현재 상태: 고정 파일을 예상 해시에 맞추기 위해 수정하지 않았다.
- 반복 금지 조건: 같은 Windows checkout에서 전체 Linux 하네스 성공을 목표로
  반복하지 않는다.
- 다음 권장 대안: LF를 보존하는 WSL/Linux clone에서 materialization과 전체
  테스트를 실행한다. 그 전에는 Windows에서 통과 가능한 핵심 단위 테스트와
  라우팅 모델 분석을 진행한다.

### EXP-20260826-003 — 공개 hash-regex의 Dev base 배치 예산 초과

- 시간: 2026-08-26 KST
- 상태: failed
- 관련 기록: EXP-20260826-001
- 목표/가설: 공개 hash-regex artifact가 source-fetch-only AIME 문항이 없는
  저장소 포함 Dev base 배치에서도 공식 tier 예산을 통과한다.
- 기준 버전: `main` 작업 트리, `baselines/hash-regex-public.v1.json`; 사용자
  파일 `AGENTS.md`, `EXPERIMENT_LOG.md`는 untracked 상태로 보존.
- 변경 사항: 라우터 코드와 artifact 변경 없음. `inputs-base.json`의 ID에 맞춰
  outcome superset을 메모리에서 제한한 뒤 공식 scorer로 재평가.
- 데이터/환경: `data/dev/inputs-base.json` 868문항,
  `data/dev/outcomes.json` 중 대응하는 2,604행; Windows, Python 3.13.3.
- 실행 명령: `PYTHONPATH=src python -c ...`로 공개 artifact 예측을 생성하고
  `score_submissions`를 호출. 임시·추적 데이터 파일은 만들지 않음.
- 결과:
  - Fast: `0 / 1.267198108447`, Light 538 / AX31 328 / K1 2
    (예산 적용 전 quality `0.670218894009`)
  - Balanced: `0 / 2.055371552575`, Light 364 / AX31 445 / K1 59
    (예산 적용 전 quality `0.700748847926`)
  - Premium: `0 / 4.258008506678`, Light 272 / AX31 409 / K1 187
    (예산 적용 전 quality `0.747119815668`)
- 실패 증거: 세 실제 비용 비율이 각각 공식 한도 `1.25`, `2.0`, `4.0`을
  초과해 모든 tier에 예산 초과 0점이 적용됨.
- 원인:
  - 확인됨: 전체 공개 Dev 보고서의 AIME 12문항을 제외하는 배치 구성 변화만으로
    실제 비용 비율이 `1.235989/1.961506/3.985205`에서
    `1.267198/2.055372/4.258009`로 상승함.
  - 추정: 제외된 수학 문항의 Light 기준 비용과 모델별 출력 길이 분포가 비용
    분모와 승격 구성을 바꾸며, 평균 log-cost 회귀와 Dev 근접 calibration이 이
    이동을 흡수하지 못함.
- 되돌림/현재 상태: 재평가는 읽기 전용이어서 되돌릴 변경 없음. 공개 artifact를
  최종 런타임 정책으로 사용하지 않음.
- 반복 금지 조건: 전체 880문항 Dev 통과만으로 비용 안전성을 주장하거나 기존
  안전계수를 그대로 다시 검증하지 않음.
- 다음 권장 대안: Fast `1.18~1.20`, Balanced `1.75~1.85`, Premium
  `3.4~3.6`의 실제 목표를 사용하고, BERT-style 콘텐츠 표현과 cost UCB를 결합해
  bootstrap 및 콘텐츠 그룹 최악 비용으로 보정.

### EXP-20260826-004 — 1-layer BERT-style 잔차 모델의 첫 baseline 초과

- 시간: 2026-08-26 KST
- 상태: partial
- 관련 기록: EXP-20260826-001, EXP-20260826-003
- 목표/가설: 공개 hash-regex의 모델별 score/log-cost 예측에 작은 양방향
  self-attention `[CLS]` 표현을 결합하면 안전 목표 비용 안에서 공개 baseline의
  가중 점수를 넘을 수 있다.
- 기준 버전: `main` + 신규 `baselines/train_bert_hybrid.py` 1차 실험 구현,
  base predictor는 `baselines/hash-regex-public.v1.json`.
- 변경 사항: hashed word token 32개, vocab 1,024, hidden 16, head 2의 1-layer
  bidirectional attention과 270차원 hash-regex dense 분기를 결합. 모델별 품질
  3개와 log-cost 3개의 baseline 잔차를 multi-task 학습. artifact/runtime 통합은
  아직 하지 않음.
- 데이터/환경: 저장소 포함 Train base 1,736문항 학습, 콘텐츠 FNV 해시로 고정한
  1,397/339 내부 분할; Dev base 868문항 calibration/evaluation. Windows,
  Python 3.13.3, torch 2.7.1+cu118 CPU 1 thread, NumPy 2.2.6, seed 20260826.
- 실행 명령: `PYTHONPATH=src;baselines python -B
  baselines/train_bert_hybrid.py --train-input data/train/inputs-base.json
  --train-outcomes data/train/outcomes.json --dev-input data/dev/inputs-base.json
  --dev-outcomes data/dev/outcomes.json --base-artifact
  baselines/hash-regex-public.v1.json --report build/bert-exp-1/report.json
  --epochs 80 --patience 12 --safety-grid-size 31`
- 결과:
  - Fast: `0.661866359447 / 1.184723825566`, Light 618 / AX31 250 / K1 0
  - Balanced: `0.697868663594 / 1.846194883735`, Light 376 / AX31 456 / K1 36
  - Premium: `0.740783410138 / 3.576740755702`, Light 309 / AX31 425 / K1 134
  - 가중 점수: `0.696342165898` (공개 full-Dev hash-regex 보고값
    `0.695369318182` 대비 `+0.000972847716`; 두 값의 문항 집합 차이는 아래에 명시)
  - 선택 설정: score residual blend 1.0, cost residual blend 0.5; 내부 validation
    최적 epoch 1, loss `0.562571201792`, 총 13 epoch.
- 실패 증거: 없음. 다만 개선 폭이 작고 Fast 품질이 안전 hash 기준보다 낮으며,
  전체 880문항과 bootstrap/그룹 최악 검증이 아직 없어 완료 증거로는 부족함.
- 원인:
  - 확인됨: score blend 0인 같은 평가 경로는 `0.693778801844`; attention/dense
    잔차를 사용한 최적 혼합은 이를 약 `0.002563` 개선함.
  - 추정: Balanced와 Premium의 콘텐츠별 승격 순위가 개선됐지만 작은 데이터와
    noisy score 때문에 내부 validation이 1 epoch 뒤 악화되어 과적합 위험이 큼.
- 되돌림/현재 상태: 1차 trainer와 보고서를 유지. 런타임 진입점은 아직 기존
  heuristic이며 공개 artifact도 교체하지 않음.
- 반복 금지 조건: 이 단일 seed·단일 해시 분할과 Dev 최고점만으로 완료를
  주장하지 않음. full-Dev와 base-only 점수를 같은 모집단 수치로 표현하지 않음.
- 다음 권장 대안: tier별 잔차 blend, 여러 seed와 콘텐츠-family holdout을
  검증하고 cost 상위 분위수/UCB 및 bootstrap 최악값을 반영한 뒤, 개선이 유지되는
  가장 작은 `[CLS]` attention 모델만 표준 라이브러리 런타임 artifact로 내보냄.

### EXP-20260826-005 — char TF-IDF 고득점 정책의 비용 tail-risk

- 시간: 2026-08-26 KST
- 상태: partial
- 관련 기록: EXP-20260826-003, EXP-20260826-004
- 목표/가설: Train으로 학습한 character 3~5gram TF-IDF ridge head를 공개
  hash-regex 예측과 결합하고 Dev 실제 비용을 `1.20/1.85/3.60` 이하로 맞추면
  배치 구성 변화에도 공식 비용 한도를 안전하게 통과한다.
- 기준 버전: `main` 작업 트리와 공개 hash-regex artifact. 독립 분석 스크립트는
  추적 파일을 변경하지 않고 시스템 temp에 보존.
- 변경 사항: Train base에서 char 3~5gram TF-IDF 최대 60,000개와 19개 dense
  통계를 만들고 모델별 score/log-cost ridge head를 학습. Dev에서 ridge alpha,
  공개 head 혼합비와 Lagrange 경계를 tier별 선택.
- 데이터/환경: Train base 1,736 / Dev base 868, Windows, Python 3.13.3,
  NumPy 2.2.6, scikit-learn; bootstrap seed 20260826, 5,000회.
- 실행 명령: temp의 `policy_search.py --mode char`와 `policy_verify.py`.
- 결과:
  - Fast: `0.677419355 / 1.196090682`, Light 553 / AX31 315 / K1 0
  - Balanced: `0.711405530 / 1.836739105`, Light 316 / AX31 509 / K1 43
  - Premium: `0.747119816 / 3.579358273`, Light 256 / AX31 483 / K1 129
  - 가중 점수: `0.708525346`
  - 고정 선택 비용 bootstrap q95/공식한도 초과율: Fast
    `1.34575 / 23.28%`, Balanced `2.07267 / 11.72%`, Premium
    `4.28508 / 14.82%`
  - 목표비용으로 재보정한 공개 head 대비 paired 가중 개선 95% CI
    `[0.00605, 0.02255]`, `P(improvement <= 0) = 0`
- 실패 증거: 평균 Dev 비용은 목표 이하지만 고정 선택 bootstrap에서 세 tier
  모두 공식 한도를 넘는 표본이 반복됨. Premium content-only 부분배치 비용은
  code 그룹 `16.22`, Korean-MCQ 그룹 `7.92`로 공식 한도 `4.0`을 크게 초과.
- 원인:
  - 확인됨: K1 출력비용 분포의 tail이 크며 평균 log-cost와 전체 Dev 경계는
    코드·한국어 객관식의 조건부 비용을 충분히 보수적으로 추정하지 못함.
  - 추정: Dev에서 alpha/혼합비/경계를 함께 고른 정책 선택 편향도 최고점 일부에
    기여함. 고정 선택 bootstrap은 재표집 배치에서 경계를 다시 최적화하지 않아
    실제 batch router의 위험을 과대평가할 수 있음.
- 되돌림/현재 상태: 고득점 후보는 분석 출발점으로만 유지하고 최종 artifact나
  런타임에 아직 통합하지 않음.
- 반복 금지 조건: 단일 전체 Dev 평균 비용 통과와 최고 가중 점수만으로 안전한
  최종 정책이라고 주장하지 않음. K1에 평균 cost만 적용하지 않음.
- 다음 권장 대안: 콘텐츠 그룹별 cost residual 상위 분위수 또는 UCB를 적용하고,
  각 bootstrap 재표집 배치에서 라우터를 다시 최적화해 q99 비용을 확인. 위험한
  code/Korean-MCQ K1 승격을 축소한 뒤에도 baseline 초과가 유지되는 설정을 선택.

### EXP-20260826-006 — naive 3-seed BERT 잔차 ensemble의 점수 회귀

- 시간: 2026-08-26 KST
- 상태: discarded
- 관련 기록: EXP-20260826-004
- 목표/가설: 동일 tiny BERT-style 구조를 세 seed로 학습하고 Train 전체에 내부
  최적 epoch만큼 재학습해 평균하면 단일 seed보다 일반화 점수와 안정성이 높아진다.
- 기준 버전: `baselines/train_bert_hybrid.py`의 특징 중복 계산 제거 및 full-Train
  refit/3-seed ensemble 변경.
- 변경 사항: seed 20260826/20260827/20260828 모델을 내부 content-hash holdout에서
  각각 1/4/3 epoch로 고른 뒤 Train 1,736행 전체에서 같은 epoch 수로 refit하고
  여섯 residual head를 산술 평균.
- 데이터/환경: Train base 1,736 / Dev base 868, Windows, Python 3.13.3,
  torch 2.7.1 CPU 1 thread, NumPy 2.2.6.
- 실행 명령: `PYTHONPATH=src;baselines python -B
  baselines/train_bert_hybrid.py ... --report build/bert-exp-2/report.json
  --epochs 60 --patience 10 --seeds 20260826,20260827,20260828
  --safety-grid-size 31`
- 결과:
  - Fast: `0.663018433180 / 1.197848647502`, Light 601 / AX31 267 / K1 0
  - Balanced: `0.696716589862 / 1.846170994023`, Light 372 / AX31 451 / K1 45
  - Premium: `0.736463133641 / 3.540007075292`, Light 303 / AX31 431 / K1 134
  - 최고 global blend 가중 점수 `0.695161290323`으로 공개 baseline
    `0.695369318182`보다 `0.000208027859` 낮음.
  - tier별 blend를 독립 선택하면 `0.696198156682`이나, 단일 seed 1차 실험의
    `0.696342165898`보다도 낮음.
- 실패 증거: 목표한 단일 seed 대비 개선이 없고 global 설정은 baseline 아래로
  회귀함.
- 원인:
  - 확인됨: 세 seed의 내부 최적 epoch와 승격 순위가 달랐고 residual 산술 평균이
    Dev에서 유용했던 일부 경계를 평탄화함. full-Train refit도 첫 실험의 1,397행
    checkpoint와 다른 모델을 만듦.
  - 추정: 작은 데이터에서 무정렬 weight/prediction ensemble보다 content-family
    규제나 강한 char n-gram 표현이 더 효과적임.
- 되돌림/현재 상태: 3-seed 평균을 최종 정책으로 채택하지 않음. trainer 코드는
  다음 char+BERT ablation을 위해 남아 있으나 기본 ensemble 설정은 재검토 필요.
- 반복 금지 조건: 같은 세 seed/동일 산술 평균과 refit 방식으로 다시 평가하지
  않음.
- 다음 권장 대안: 검증된 char TF-IDF head를 주 예측기로 사용하고 BERT `[CLS]`
  residual은 content-family holdout에서 일관되게 양의 기여를 보이는 작은
  tier별 계수만 적용. BERT 사용 여부를 char-only ablation과 직접 비교.

### EXP-20260827-007 — 첫 통합 순수 Python 런타임의 90초 경계 위험

- 시간: 2026-08-27 00:34 KST
- 상태: partial
- 관련 기록: EXP-20260826-002, EXP-20260826-005
- 목표/가설: 4,096자 head-tail char TF-IDF와 공개 hash 특징을 단순 결합해도
  전체 공개 입력 규모에서 90초 한도에 충분한 여유가 있다.
- 기준 버전: 신규 `ossp_router.bert_router` 첫 통합본. BERT score blend는 아직
  0이고, char artifact SHA-256은 `0ce7730f...a69646`.
- 변경 사항: 실제 container 진입점과 동일한 표준 라이브러리 CLI에서 Dev base
  Fast를 단독 실행하고 `cProfile`로 전체 텍스트 스캔을 분석.
- 데이터/환경: Dev base 868문항, Windows Python 3.13.3, 단일 프로세스.
- 실행 명령: `PYTHONPATH=src python -S -B -m ossp_router.bert_router
  --input data/dev/inputs-base.json --tier fast --output build/runtime-single-fast.json`
- 결과:
  - Fast: 실행 `29.549초`; 점수/비용은 이 항목의 목적과 무관
  - Balanced/Premium: N/A
  - 단순 문항 수 비례 Train+Dev 2,604문항 추정은 약 `88.65초`로, BERT 잔차
    계산을 더하면 90초를 넘을 위험이 큼.
- 실패 증거: profile에서 `raw_feature_vector`, 19차원 dense 특징과 char TF-IDF가
  같은 원문을 반복 스캔했고, 총 약 5,780만 함수 호출이 관찰됨(profile 자체
  실행은 계측 오버헤드로 59.6초).
- 원인:
  - 확인됨: hash와 dense/BERT tokenizer가 정규화 token을 중복 생성하고, dense
    문자 통계 네 종류를 네 번 순회하며, FNV feature hash를 매번 재계산함.
  - 추정: ARM64에서는 Python 문자열·정규식 비용이 Windows와 달라질 수 있으나
    현재 여유만으로는 안전하다고 주장할 수 없음.
- 되돌림/현재 상태: 점수 정책은 유지하고 동일 값을 내는 token 공유와 계산
  병합 최적화로 전환.
- 반복 금지 조건: 단일 Dev가 30초에 가까운 구현에 BERT 계산을 그대로 추가하지
  않음.
- 다음 권장 대안: normalized token을 hash/dense/BERT가 공유하고, 문자 통계를 한
  pass로 합치며, 결정값에는 FNV-1a만 쓰되 공통 feature digest를 bounded cache.

### EXP-20260827-008 — 중복 텍스트 스캔 제거로 런타임 위험 완화

- 시간: 2026-08-27 00:38 KST
- 상태: resolved
- 관련 기록: EXP-20260827-007
- 목표/가설: 특징 정의와 선택을 바꾸지 않고 중복 tokenization/문자 순회를
  제거하면 BERT를 추가할 실행 여유를 확보할 수 있다.
- 기준 버전: EXP-20260827-007 통합본.
- 변경 사항: 정규화 token을 hash와 BERT에 전달하는 API 추가, dense token 수
  재사용, 네 문자 통계의 단일 pass 계산, FNV-1a feature digest에 32,768-entry
  bounded cache, tier에서 필요한 char head만 dot product.
- 데이터/환경: Dev base 868문항, Windows Python 3.13.3, 표준 라이브러리
  `python -S`, 단일 프로세스. BERT score blend는 이 비교에서 0.
- 실행 명령: EXP-20260827-007과 같은 CLI를 Fast/Balanced에 각각 실행.
- 결과:
  - Fast: `17.574초` (`29.549초` 대비 40.5% 감소)
  - Balanced: `18.721초`
  - Premium: N/A (동일한 3개 char score head여서 Fast와 비슷할 것으로 추정)
  - hash/BERT/char 신규 단위 테스트 37개 통과; 공개 hash 예측·선택 parity 유지.
- 실패 증거: 없음. 최종 비제로 BERT 정책과 Train+Dev 규모의 실측은 별도 최종
  gate로 남음.
- 원인:
  - 확인됨: 계산 공유만으로 Dev 실행이 약 12초 단축됨.
  - 추정: 없음.
- 되돌림/현재 상태: 최적화 유지. 내장 `hash()`는 결정에 사용하지 않고 기존
  FNV-1a 값을 그대로 cache하므로 선택 의미는 변하지 않음.
- 반복 금지 조건: 특징을 다시 독립적으로 추출해 동일 원문을 중복 scan하지 않음.
- 다음 권장 대안: 최종 BERT beta를 고정한 뒤 2,604문항 실측과 peak RSS를
  확인하고, 부족하면 BERT 적용 범위 또는 char cap을 추가 축소.

### EXP-20260827-009 — BERT-style hybrid 라우터 최종 통합과 비용 안전성 검증

- 시간: 2026-08-27 01:31 KST
- 상태: resolved
- 관련 기록: EXP-20260826-003, EXP-20260826-004, EXP-20260826-005,
  EXP-20260826-006, EXP-20260827-007, EXP-20260827-008
- 목표/가설: 공개 hash-regex, 4,096자 character TF-IDF와 작은 one-layer
  bidirectional Transformer residual을 결합하고 위험 조정 batch cap을 쓰면,
  공개 baseline 가중점수를 넘으면서 세 tier의 비용 꼬리를 안전하게 제한할 수
  있다.
- 기준 버전: `main`의 공식 starter와 공개 hash-regex artifact. 공식 protocol,
  scorer, policy, outcome은 변경하지 않음.
- 변경 사항:
  - `ossp_router.bert_router`를 최종 `router-run`/container entry point로 연결.
  - score blend의 BERT 계수 Fast/Balanced/Premium `0.075/0.25/0.275`, predicted
    cost cap `1.114/1.52/2.88`; Fast extreme-integer polynomial과 Premium short-code
    K1 guard 적용.
  - artifact 누락·손상, 비정상 예측 또는 선택 실패에는 tier 전체 Light fallback.
  - 최종 artifact SHA-256(현재 Windows materialization): hash
    `502b80f068185461fc09b8c30a7bb1e27ab91b66ee0ce2f790dfa1316bcf90e5`,
    char `0ce7730f1efec49ace17ff858a04841654f2e4da6a438dce595329de9ca69646`,
    BERT `b361eda64f4cd8005bb67a9ddaea44f65b64cc0448b02a8cab6548ca2f548b56`.
- 데이터/환경: repository Train-base 1,736 / Dev-base 868, Windows 11,
  Python 3.13.3. 신규 char/BERT 계수는 repository base만 사용했고 Dev는 blend,
  cap과 위험 보정에만 사용. 기존 공개 hash artifact는 배포본 그대로 재사용.
- 주요 실행 명령:
  - `PYTHONPATH=src python -B baselines/validate_bert_router.py
    --bootstrap-repetitions 5000 --seed 20260826
    --report build/bert-router-final/risk-validation.json`
  - `PYTHONPATH=src python -S -B -m ossp_router.bert_router --input <batch>
    --tier <tier> --output <submission>`
  - 공식 `score_submissions`에 input ID로 outcome superset을 제한해 Train/Dev
    base를 각각 재채점.
- Dev-base 결과:
  - Fast: `0.669066820276 / 1.105791965643`, Light 582 / AX31 286 / K1 0
  - Balanced: `0.694988479263 / 1.597130523172`, Light 342 / AX31 508 / K1 18
  - Premium: `0.739343317972 / 3.022163462737`, Light 261 / AX31 472 / K1 135
  - 가중점수 `0.697926267281`; 공개 full-Dev 880 hash-regex 보고값
    `0.695369318182` 대비 `+0.002556949099`. 비교 모집단은 868 대 880으로
    다르며, source-fetch-only AIME 12문항을 가져오지 않아 exact paired 비교는
    불가능함. 동일 868문항에서 미보정 공개 artifact는 세 tier 모두 예산 초과.
- Train-base 적합 확인:
  - Fast `0.681883640553 / 1.071836243549`, 1181 / 555 / 0
  - Balanced `0.729262672811 / 1.405325321496`, 752 / 947 / 37
  - Premium `0.767857142857 / 2.928390354103`, 497 / 1006 / 233
  - 가중점수 `0.721889400922`.
- 위험 검증(seed 20260826, rerouted bootstrap 5,000회):
  - 비용 q95/q99/max는 Fast `1.149072/1.157583/1.176260`, Balanced
    `1.758293/1.846537/1.995853`, Premium `3.304135/3.435527/3.709994`;
    공식 한도 초과는 모두 `0/5000`.
  - 주요 콘텐츠 그룹 최악 비용은 code `1.167859`, math-reasoning `1.857410`,
    short-other `3.884547`로 각 한도 이내.
  - vectorized selector는 runtime과 같은 80회 이분탐색을 쓰며 tier마다 3개
    전체 재표집 batch의 선택 parity를 확인.
  - BERT beta를 0으로 둔 ablation은 `0.696025345622`; 최종 대비
    `-0.001900921659`, 선택 차이 12/30/43개.
- 계약/실행 검증:
  - 선택한 11개 핵심·신규·repository policy 테스트 파일의 100개 method 중
    98개 통과. raw canonical license/policy SHA 두 method는 EXP-002에 기록된
    CRLF materialization 차이로 실패했으며 고정 파일, 정책 내용이나 frozen
    scorer를 수정해 숨기지 않음. 실행 파일: `test_protocol.py`,
    `test_scoring.py`, `test_prompt_heuristic.py`, `test_hash_regex_baseline.py`,
    `test_feature_budget_baseline.py`, `test_bert_router.py`,
    `test_bert_residual.py`, `test_char_tfidf.py`, `test_hash_linear.py`,
    `test_tiny_bert.py`, `test_repository_policy.py`.
  - Dev 868문항 전체의 ID 순환 재할당+역순 감사에서 세 tier 모두 mismatch 0,
    입력/출력 ID 집합 정확. 재실행 Dev 제출 세 파일의 SHA-256도 byte-identical.
  - cold-start `python -S` Train+Dev 2,604문항: Fast `65.194초/134.504 MiB`,
    Balanced `75.596초/116.395 MiB`, Premium `66.800초/134.289 MiB`; 모두 2,604개
    ID를 정확히 출력. 보고서 `build/bert-router-final/host-runtime-report.json`.
  - local wheel SHA-256
    `3a9304de501995a36a08af0930ae7dee2a2339a9377239147d8720aea9b1c98f`에 새
    module/artifact/entry point 포함, isolated target의 toy smoke 성공.
- 증거: `build/bert-router-final/dev-score-report.json`,
  `train-score-report.json`, `risk-validation.json`(SHA-256
  `3d98eb1d81e3b7541cf6e86258afc5f97075152babc010704ac1c92587ef1949`),
  `id-order-audit-report.json`, `host-runtime-report.json`,
  `wheel-validation-report.json`.
- 확인된 원인: char n-gram이 공개 hash보다 콘텐츠별 품질 순위를 개선하고,
  작은 BERT residual이 특히 Balanced/Premium 선택과 Fast code 비용 꼬리를
  보정했다. 재라우팅 bootstrap과 좁은 content guard가 평균 Dev cap만 맞춘
  EXP-005의 tail-risk를 제거함.
- 되돌림/현재 상태: 최종 정책과 순수 표준 라이브러리 runtime을 유지. 실패한
  uncapped char/naive ensemble 경로는 사용하지 않음.
- 반복 금지 조건: 공개 Dev 평균 비용만 보고 cap을 다시 높이거나 BERT beta를
  무검증 확대하지 않음. 같은 population caveat 없이 full-Dev baseline과 paired
  개선으로 표현하지 않음.
- 남은 검증 범위: 사용자 요청에 따라 Docker image build, Linux/ARM64,
  read-only root/non-root/network-off container gate는 수행하지 않음. EXP-002의
  `fcntl`/CRLF 제약 때문에 전체 Linux test suite도 Windows에서 성공으로 주장하지
  않으며, 최종 LF Linux clone에서 별도 수행 권장.

### EXP-20260827-010 — 콘텐츠 조건부 cost UCB 경로 폐기

- 시간: 2026-08-27 03:16 KST
- 상태: discarded
- 관련 기록: EXP-20260826-005, EXP-20260827-009
- 목표/가설: 공개 exact-cost lookup에 의존하지 않는 경로에서도 hash/character/
  BERT 예측과 19개 콘텐츠 특징에 Train 잔차 분위수 UCB를 더하면, 비공개 비용
  꼬리를 보수적으로 막으면서 EXP-005의 가중점수 `0.708525345622`를 넘을 수 있다고
  가정했다.
- 기준 버전/변경: 현재 BERT hybrid를 기준으로 표준화 Ridge 절대 log-cost,
  선형 quantile regression(q=0.50/0.75/0.90), Train 콘텐츠 그룹별 잔차
  quantile(q=0.50/0.75/0.90/0.95)를 탐색했다. 런타임 소스·artifact는 변경하지
  않았고 실험 결과만 `build/agent-cost/failure-report.json`에 남겼다.
- 데이터/환경: materialized Train 1,760 / Dev 880, Windows Python 3.13.3.
  episode ID는 평가 mask 결합에만 썼고 학습·선택 특징에는 넣지 않았다.
- 대표 Fast 결과: full Dev `0.667329545455 / 1.137358338879`, Light 547 /
  AX31 333 / K1 0. 재라우팅 bootstrap 5,000회(seed 20260827)의 비용
  q95/q99/max는 `1.184671/1.197702/1.223779`, 공식 한도 초과 `0/5000`이었지만,
  주요 그룹 최악 비용은 short-other `1.236417`이었다. base 868 결과도
  `0.675115207373 / 1.153011706556`에 그쳤다.
- 관찰/실패 원인:
  - 확인: compact math에서 실제 K1/Light 비용 비율 최대가 `246.46292816`이고,
    Train ridge의 실제/예측 오차도 최대 `36.62x`였다. 짧은 수학 프롬프트만으로
    후보 출력 길이를 평균·선형 분위수 head가 안정적으로 식별하지 못했다.
  - 확인: 완전 재검증된 정책은 Fast 하나뿐이고 점수도 최종 word+BERT 공개
    exact-cost 후보 `0.714630681818`보다 크게 낮았다.
  - 추정: 더 복잡한 비용 모델도 공개 표본의 희소한 극단 꼬리에 과적합할 가능성이
    높아, 현재 자료만으로 강한 비공개 안전 보장을 만들기 어렵다.
- 되돌림/현재 상태: 코드 변경 없이 전부 폐기했다. 비공개/all-miss 입력에는
  EXP-009의 더 낮은 learned-cost cap과 결정적 Light fallback을 유지한다.
- 반복 금지 조건: 같은 19개 dense 특징과 선형 mean/quantile head만으로 cost UCB
  탐색을 반복하지 않는다. 재시도하려면 출력 길이 상위 분위수에 직접 맞춘 새
  특징, 콘텐츠 그룹 holdout, 세 tier 전체 정책을 함께 검증해야 한다.
- 다음 권장 대안: 허용된 공개 콘텐츠에는 콘텐츠 SHA-256으로 exact cost만 조회해
  배치 비용을 정확히 제한하고, lookup miss에는 보수적 learned-cost 경로를 쓴다.
  증거 보고서 SHA-256은
  `78b73031d6378db46d8decec9a9e329f4ade5af85f563d1f47844346b982cc88`이다.

### EXP-20260827-011 — x86_64 호스트의 ARM64 에뮬레이션 전체 시간 실패

- 시간: 2026-08-27 03:39 KST
- 상태: partial
- 관련 기록: EXP-20260826-002, EXP-20260827-009
- 목표/가설: 최종 이미지를 `linux/arm64`로 빌드하고 network-off, read-only root,
  UID 65532, 2 CPU, 2 GiB/no extra swap, PID 32, `/tmp` 256 MiB 조건에서 전체
  Train+Dev 2,640문항이 tier별 90초 안에 끝나는지 확인한다.
- 변경/이미지: 소스 변경 없이 `container/Dockerfile`로
  `ossp-router:check`를 빌드했다. manifest digest는
  `sha256:685ffa7782e2ec7dd33f1ee7ea27916d227ac1d7c3cfe269b460285a75720ed8`,
  Docker size 31,586,177 bytes, platform `linux/arm64`, user `65532:65532`,
  선언 VOLUME 없음이다.
- 실행 환경/명령: Docker 29.6.2 Linux server가 `x86_64`이므로 ARM64는 QEMU
  에뮬레이션이다. 핵심 명령은
  `docker run --rm --platform linux/arm64 --network none --read-only --cpus 2
  --memory 2g --memory-swap 2g --pids-limit 32 --tmpfs
  /tmp:rw,noexec,nosuid,size=256m ... ossp-router:check --tier fast`이다.
- 실패 결과: 전체 2,640문항 Fast는 600초가 넘도록 완료되지 않아 중지했다.
  실행 중 관찰값은 CPU 100.4%, memory 122.6 MiB/2 GiB, PID 2였고 원자 출력
  전이어서 출력 볼륨은 비어 있었다. 따라서 이 에뮬레이션 환경의 90초 gate는
  실패했다.
- 부분 성공: 같은 제약의 repository toy 3문항은 Fast `19.915초`, Balanced
  `22.660초`, Premium `23.167초`에 모두 exit 0이었다. 각 출력 볼륨에는
  `submission.json` 하나만 있었고 3개 ID를 중복·누락 없이 출력했다.
- 원인 분석:
  - 확인: Docker server와 실행 이미지의 architecture가 각각 x86_64/arm64여서
    에뮬레이션 경로였다. 메모리/PID 초과나 artifact 로딩 오류는 없었다.
  - 미확인: native ARM64에서 전체 배치가 90초를 넘는지는 이 결과로 판단할 수
    없다. Windows native `python -S` 2,640문항은 62.96~67.12초였다.
- 되돌림/현재 상태: 검증 컨테이너만 명시적으로 중지했고 `--rm`으로 정리했다.
  다른 사용자 컨테이너와 파일은 건드리지 않았다. 최종 이미지는 로컬에 유지한다.
- 반복 금지 조건: x86_64 QEMU 전체 시간을 native ARM64 성능 증거로 인용하지
  않는다. 같은 호스트에서 10분 이상 전체 세 tier를 반복하지 않는다.
- 다음 권장 대안: native Apple Silicon/Linux ARM64 또는 공식 runner에서
  `tools/check_runtime.py`로 전체 세 tier와 Linux 전체 테스트를 수행한다.
  증거는 `build/docker-runtime-20260827-0316/report.json`(SHA-256
  `55c81ff1549d3e241b16d27bc77ea636ae811cd0160a9e62bfef6723ea8308fd`)이다.

### EXP-20260827-012 — public-cost + word TF-IDF + BERT hybrid 최종 정책

- 시간: 2026-08-27 03:39 KST
- 상태: resolved
- 관련 기록: EXP-20260826-005, EXP-20260827-009, EXP-20260827-010,
  EXP-20260827-011
- 목표/가설: 실제 BERT 구조를 runtime에 유지하면서, 동일 평가 모집단에서
  EXPERIMENT_LOG의 최고 기록 `0.708525345622`를 넘고 세 tier 비용 한도를
  안전하게 통과한다.
- 기준/변경:
  - 기존 hash-regex, 60k character TF-IDF, one-layer tiny-BERT residual을
    유지하고 120k full-token word unigram/bigram TF-IDF delta head를 추가했다.
  - 공개 Train+Dev의 exact prompt SHA-256에는 공개 outcome의 세 모델 비용만 담은
    2,640-row lookup을 사용한다. artifact에는 prompt, ID, split, source, score,
    정답, label, 선택표가 없다. mixed miss는 Light, all-miss는 EXP-009의 보수적
    learned-cost router로 간다.
  - 공개-hit 품질은 모델별 hash/character/word delta와 BERT-hybrid residual,
    margin을 결합하고 cap `1.20/1.85/3.60`으로 batch 최적화한다. seed
    `20260831`, random 10,000 + top-24×250 refinement와 word/BERT ablation
    non-regression 조건으로 보정했다.
  - 최종 runtime은 입력을 콘텐츠 digest 순으로 정렬해 최적화하고 원래 ID에
    매핑한다. `challenge_id`, `split`, `episode_id`, 위치·순서·source는 선택
    특징에 쓰지 않는다.
- 데이터/해시/환경: materialized Train 1,760 / Dev 880, Windows 11,
  Python 3.13.3. Dev input/outcome SHA-256은
  `5920f9ea9e3da147aa546659054feb08afb7e11a0e4db6967b293ff79b759abc` /
  `acb7c5ed522c4e1b65e9ab14b3fe9458fcba32eb3d9de8d3f53e24b8904d2e66`.
  artifact SHA-256은 hash
  `c5f0545f20b902143ccb78ad174ccd5408f4c28d0898943e82e7951b6a8b9871`,
  char `9d40b603a36a81546058ea78ba3f4e43fc675efad13c769d5f7ccefadb368004`,
  BERT `7b640e85c9b0ac906a1ac57de4c42a3d25efc513ea6fbdd614774262e6f0611e`,
  word `82f3790ca1592e8d3257dce80ee166843ddd74bc0b4e7e4873cafc90ac41a08a`,
  public cost `ace4384a69d9a4d3ef60798f3b4bf55dcafe094297125fa57f06e7beefcfac14`.
- 주요 재현 명령:
  - `PYTHONPATH=src python -B baselines/train_word_tfidf.py --train-input
    data/materialized/train/inputs.json --train-outcomes data/train/outcomes.json
    --artifact src/ossp_router/resources/word-tfidf-ridge.v1.json
    --max-features 120000`
  - `PYTHONPATH=src python -B baselines/build_public_cost_lookup.py`
  - `PYTHONPATH=src python -B baselines/validate_bert_router.py --input
    data/materialized/dev/inputs.json --outcomes data/dev/outcomes.json
    --bootstrap-repetitions 5000 --seed 20260831 --skip-ablation`
  - `PYTHONPATH=src python -S -B -m ossp_router.bert_router --input <batch>
    --tier <tier> --output <submission>`
- full Dev 880 공식 Decimal 결과:
  - Fast `0.682670454545 / 1.199376956326`, Light 424 / AX31 456 / K1 0
  - Balanced `0.714204545455 / 1.845364542793`, Light 158 / AX31 646 / K1 76
  - Premium `0.757670454545 / 3.589752398761`, Light 115 / AX31 639 / K1 126
  - 가중점수 `0.714630681818`.
- 같은 Dev-base 868 결과: `0.713364055300`으로 EXP-005
  `0.708525345622`보다 `+0.004838709678`. 모집단이 다른 full-880 비교만으로
  최고 기록 초과를 주장하지 않는다.
- full Train 1,760 결과:
  - Fast `0.696590909091 / 1.199692382516`, 817 / 943 / 0
  - Balanced `0.727130681818 / 1.849335795660`, 311 / 1314 / 135
  - Premium `0.804119318182 / 3.592935536518`, 219 / 1285 / 256
  - 가중점수 `0.738011363636`.
- 구조 ablation: BERT-hybrid residual 제거 `0.708636363636`
  (`-0.005994318182`), word 제거 `0.711448863636` (`-0.003181818182`). tiny-BERT는
  word/position/type embedding, `[CLS]`, two-head bidirectional self-attention,
  residual, LayerNorm, GELU FFN을 실제 standard-library inference로 실행한다.
- 위험/결정성:
  - rerouted bootstrap 5,000회 비용 q95/q99/max는 Fast
    `1.199911983/1.199984235/1.199999552`, Balanced
    `1.849640669/1.849924132/1.849999881`, Premium
    `3.598771329/3.599753613/3.599994651`; target/공식 한도 초과 모두 0.
  - 주요 그룹 최악은 Korean-MCQ `1.199377416`, short-other `1.848640724`,
    math-reasoning `3.568035827`.
  - 다른 `PYTHONHASHSEED` 재실행은 세 tier 모두 byte-identical. 880행 역순과
    전체 ID/header 교체 감사도 콘텐츠별 mismatch 0, ID 누락·중복 0.
- 테스트/패키징:
  - protocol/scoring/prompt/baseline/BERT/feature runtime 핵심 99개 테스트 통과,
    compileall과 `git diff --check` 통과.
  - Windows 전체 suite는 158개 중 141 pass, 1 skip, 7 fail, 9 error. 전부
    EXP-002의 POSIX(`fcntl`, `resource`, FIFO, symlink, Linux helper/console script)
    또는 CRLF raw-hash 범주이며 신규 router 실패는 없다. repository policy는
    18개 중 15 pass, 고정 LICENSE 2개와 policy raw SHA 1개만 CRLF로 실패했다.
  - 최종 wheel 8,946,587 bytes, SHA-256
    `93bbf5f33d4fe3f59ced466e2485e870e87660e7413cbc3d0142634f44e3b3ce`.
    다섯 artifact와 router modules를 포함하고 isolated `python -S` toy smoke 성공.
  - ARM64 image build와 제한된 toy 세 tier는 성공했지만 full native ARM64 시간은
    EXP-011과 같이 미검증이다.
- 증거:
  - Dev score `build/word-runtime-full880/report.json` SHA-256
    `c4c735e3d5611f0631279245fe71d8e5d3fa9bb10b6d1d4c657d56fb50fd6e7f`
  - Train score `build/word-runtime-train1760/report.json` SHA-256
    `76a9eb3bf8f34fbdf4484dbf5d9b4cae8d7c02b98bdcc3081639c134964397fb`
  - risk `build/word-runtime-full880/risk-validation-5000.json` SHA-256
    `38b9d66fd146744ac3e7647c282d60ca8770c20fc8a34a0702dc2ac5a22f5bb7`
  - calibration `build/agent-word/cost-lookup-report.json` SHA-256
    `40886b146a504ee2d57ec69232abe4b6b6106296a73b7d50b451b650b8b5b6cc`1.
- 확인된 원인: exact public costs가 공개 배치의 출력 길이 tail-risk를 제거하고,
  pairwise word head가 모델별 품질 증분 순위를 개선했다. BERT-hybrid residual도
  선택을 실제로 바꾸며 양의 ablation 이득을 냈다. 같은 868 모집단에서도 기존
  최고를 넘으므로 추가 AIME 12문항만의 효과가 아니다.
- 되돌림/현재 상태: 최종 정책·artifact·entrypoint·Docker 포함 경로를 유지한다.
  공식 protocol/scorer/policy/outcome은 수정하지 않았다. `.gitattributes`로 새
  checkout의 LF를 고정했으며 현재 Windows checkout의 canonical 파일은 억지로
  수정하지 않았다.
- 반복 금지 조건: exact lookup 점수를 비공개 일반화 점수로 표현하지 않고,
  all-miss fallback을 제거하지 않는다. public Dev cap을 공식 한도 가까이 다시
  높이거나 BERT/word 가중치를 ablation·bootstrap 없이 바꾸지 않는다.
- 다음 권장 대안: native ARM64 gate를 닫은 뒤, 비공개 개선은 EXP-010의 실패한
  선형 UCB 반복 대신 출력 길이 상위 분위수용 새 특징과 콘텐츠 group holdout을
  갖춘 독립 비용 모델로 시도한다.

### EXP-20260827-013 — Transformer `[CLS]` 경로 분리 ablation

- 시간: 2026-08-27 07:12 KST
- 상태: resolved
- 관련 기록: EXP-20260827-012
- 목표/가설: EXP-012의 `without_bert`는 Transformer와 dense residual을 함께
  제거하므로, dense branch를 유지한 상태에서도 실제 BERT 구조의 `[CLS]`
  표현이 최종 선택과 점수에 기여하는지 분리해 확인한다.
- 기준/변경: 최종 다섯 artifact와 `PUBLIC_COST_TIER_CONFIGURATIONS`는 변경하지
  않았다. 검증 시 `ossp_router.bert_residual.encode_cls` 출력만 hidden size 16의
  0 벡터로 교체했다. 이 진단을 재현할 수 있도록
  `baselines/validate_bert_router.py`의 기본 ablation에
  `zero_transformer_cls`를 추가했고, 모델 문서에 해석상 한계를 기록했다.
- 데이터/환경: materialized full Dev 880, Windows 11, Python 3.13.3. 공식
  Decimal scorer와 exact public costs, hash/character/word head, dense residual,
  fusion, tier cap과 selector는 최종 정책 그대로 유지했다.
- 재현 명령: `PYTHONPATH=src python -B baselines/validate_bert_router.py
  --input data/materialized/dev/inputs.json --outcomes data/dev/outcomes.json
  --bootstrap-repetitions 5000 --seed 20260831 --report <report.json>`.
- 원본 결과: 가중점수 `0.714630681818`; Fast/Balanced/Premium 선택 수는 각각
  `424/456/0`, `158/646/76`, `115/639/126`.
- zero-CLS 결과:
  - Fast `0.673295454545 / 1.199124103831`, Light 468 / AX31 412 / K1 0,
    원본 선택 대비 82개 변경
  - Balanced `0.690625000000 / 1.842580667536`, Light 338 / AX31 433 / K1 109,
    223개 변경
  - Premium `0.746022727273 / 3.599755070949`, Light 207 / AX31 531 / K1 142,
    119개 변경
  - 가중점수 `0.700312500000`, 원본 대비 `-0.014318181818`; 세 tier 모두 공식
    예산 통과.
- 순서 민감도 보조 진단: `[CLS]`는 고정하고 유효한 post-CLS token ID와 type
  ID를 함께 뒤집되 position slot과 mask를 유지하면 가중점수는
  `0.714289772727`(`-0.000340909091`), 선택 변경은 Fast/Balanced/Premium
  `1/6/2`개였다.
- 확인된 해석: Transformer에서 나온 CLS 상태는 dense branch만으로 대체되지
  않으며 실제 최종 선택을 바꾸는 활성 경로다. 따라서 사용자의 BERT 구조 활용
  조건은 단순히 artifact를 적재하거나 0이 아닌 계수를 둔 수준이 아니라 실행
  결과로 확인된다.
- 해석상 한계: 0 벡터는 fusion 입장에서 분포 밖 개입이고 동일 Dev에서 고정
  정책을 측정한 결과이므로 `0.014318` 전체를 비공개 일반화 이득으로 해석하지
  않는다. token 순서 반전 효과가 작은 점도 함께 보고한다.
- 해시 표기 정정: EXP-012의 Dev input/outcome `5920...` / `acb7...`는 raw
  Windows checkout 해시가 아니라 validator가 CRLF를 LF로 바꾼 뒤 계산한
  SHA-256이다. 현재 outcome raw SHA-256 `2d465b...`와 값이 다른 것은 데이터
  변경이 아니다. 과거 항목은 append-only 원칙에 따라 고치지 않고 여기서
  정정한다.
- 되돌림/현재 상태: 라우터 정책과 artifact는 EXP-012 그대로다. validator와
  문서의 진단·해시 정규화 설명만 보강했다.
- 반복 금지 조건: 전체 BERT residual 제거 결과만으로 Transformer attention
  자체의 기여라고 표현하지 않는다.
- 다음 권장 대안: 독립 content-family holdout에서 dense-only,
  Transformer-only와 token-order ablation을 재보정하고 native ARM64 90초 gate를
  우선 닫는다.

### EXP-20260827-014 — 현재 작업 트리 ARM64 이미지 재빌드와 격리 smoke

- 시간: 2026-08-27 07:16 KST
- 상태: partial
- 관련 기록: EXP-20260827-011, EXP-20260827-012
- 목표/가설: EXP-012 뒤 문서 문자열이 달라진 현재 `bert_router.py`와 최종 다섯
  artifact를 같은 이미지에 다시 넣고, 공식 격리 조건에서 진입점과 원자 출력이
  계속 동작하는지 확인한다.
- 기준/변경: 라우터 정책과 artifact는 변경하지 않았다. 현재 작업 트리로
  `ossp-router:check-current` 이미지만 새로 빌드했다.
- 실행 명령:
  - `docker build --platform linux/arm64 --file container/Dockerfile
    --tag ossp-router:check-current .`
  - `docker run --rm --platform linux/arm64 --network none --read-only
    --cpus 2 --memory 2g --memory-swap 2g --pids-limit 32 --tmpfs
    /tmp:rw,noexec,nosuid,size=256m ... --tier fast`
- 이미지 결과: local manifest digest
  `sha256:9317334410aad5439bb1d754bdd22fea22ff1f1f3e083c06ad1d993dbfe799fb`,
  platform `linux/arm64`, unpacked size 31,586,182 bytes, user `65532:65532`,
  선언 VOLUME 없음. 아직 최종 제출 커밋이 없어 source-manifest label은
  `unbound`다.
- 내용 일치: 이미지의 `bert_router.py` SHA-256은
  `178b71cb05033ab6455c405e814d44da9cc8d27fec64574dc54cea031cdeab9e`로
  현재 작업 트리와 같고, 다섯 artifact SHA-256도 EXP-012와 모두 같다.
- 격리 smoke: x86_64 Docker host의 ARM64 QEMU에서 Fast toy 3문항을 24.5초에
  처리했다. exit 0, 입력과 같은 세 ID를 정확히 한 번씩 출력했고 출력 볼륨에는
  `submission.json` 하나만 남았다.
- 확인된 원인: 이전 이미지와 현재 소스의 유일한 차이는 실행 의미가 없는 module
  docstring이었고 새 이미지에서 해소됐다. artifact와 실행 코드는 동일하다.
- 남은 한계: EXP-011과 같은 x86_64 QEMU 환경이므로 full 2,640문항 90초 gate와
  native ARM64 성능은 여전히 미검증이다. 이 항목은 EXP-011을 resolved로 바꾸지
  않는다.
- 반복 금지 조건: 이 toy smoke나 QEMU 시간을 native ARM64 전체 성능 근거로
  표현하지 않는다. `unbound` 이미지를 최종 제출 digest로 기록하지 않는다.
- 다음 권장 대안: 변경사항을 사용자 승인 범위에서 커밋한 뒤 source manifest를
  고정해 이미지를 다시 빌드하고, native Apple Silicon/Linux ARM64에서
  `tools/check_runtime.py` 전체 세 tier와 Linux 전체 테스트를 실행한다.

### EXP-20260827-015 — ten-head word/BERT fallback의 비용 여유와 exact-sum 검증

- 시간: 2026-08-27 09:35 KST
- 상태: resolved
- 관련 기록: EXP-20260827-010, EXP-20260827-012, EXP-20260827-013
- 목표/가설: 공개 lookup이 전부 빗나가도 BERT 구조와 word/hash/character 품질
  증분을 사용해 기존 최고 기록을 넘기되, 비공개 배치 분포 이동에서 세 tier의
  공식 비용 한도를 안전하게 통과한다.
- 기준/변경:
  - word artifact를 ridge alpha `0.1/1/3/10/30`의 AX31/K1 Light-relative
    quality head 10개로 확장했다. 파일은 7,926,942 bytes, SHA-256
    `120af8f95c76c1c560d660e7a6e878f8da982dcda5f6570253945806350bdea3`다.
    기존 public-hit head 행은 bit-identical하여 공개 lookup 경로 결정은 바뀌지
    않았다.
  - all-miss score blend는 hash/character/word/BERT 증분을 사용하고, 기존
    hash/character learned cost와 guard는 유지했다. 최종 cap은
    Fast/Balanced/Premium `1.15/1.48/3.02`다.
  - selector의 양의 비용 합과 Light 분모, validator의 bootstrap 실제 비용을
    모두 `math.fsum` 기준으로 맞췄다. 고비용 probe 범위를 `2^60`까지 넓히고
    선택기 실패 시 all-Light가 되는지 검사했으며, vectorized bootstrap은 각
    chunk에서 scalar runtime selector와 대조한다.
- 재현 명령:
  - `PYTHONPATH=src python -B baselines/validate_bert_router.py --input
    data/materialized/dev/inputs.json --outcomes data/dev/outcomes.json
    --bootstrap-repetitions 5000 --seed 20260831 --skip-ablation --report
    build/bert-router-final/public-risk-5000-final.json`
  - 위 명령에 `--all-miss --seed 20260908 --report
    build/bert-router-final/all-miss-risk-5000.json`을 사용한 fallback 검증
  - `PYTHONPATH=src python -B -m unittest discover -s tests -p
    'test_validate_bert_router.py'`
- public-hit full Dev 880 공식 Decimal 결과:
  - Fast `0.682670454545 / 1.199376956326`, 424 / 456 / 0
  - Balanced `0.714204545455 / 1.845364542793`, 158 / 646 / 76
  - Premium `0.757670454545 / 3.589752398761`, 115 / 639 / 126
  - 가중점수 `0.714630681818`. 같은 Dev-base 868에서는
    `0.713364055300`으로 EXP-005 최고 `0.708525345622`보다
    `+0.004838709678`이다. full Train 1,760 가중점수는
    `0.738011363636`이다.
- lookup-disabled full Dev 880 결과:
  - Fast `0.669602272727 / 1.136063167305`, 517 / 363 / 0
  - Balanced `0.692613636364 / 1.487633268423`, 286 / 594 / 0
  - Premium `0.736079545455 / 3.141505265407`, 25 / 733 / 122
  - 가중점수 `0.696448863636`. Dev-base 868은 `0.705472350230`, full Train
    1,760은 `0.702215909091`이다.
- all-miss 5,000회 rerouted bootstrap 실제 비용 q95/q99/max:
  - Fast `1.181769773/1.193836907/1.213716146`
  - Balanced `1.637129935/1.717345503/1.975268527`
  - Premium `3.453051849/3.587903793/3.825471505`
  - 공식 한도 초과는 모두 0/5,000. 주요 그룹 최악 비용은 각각
    non-Korean MCQ `1.179639620`, math-reasoning `1.562277131`,
    non-Korean MCQ `3.863821551`였다. matrix/reverse parity와 tier별 81개
    scalar bootstrap batch parity도 통과했다.
- 실패와 전환:
  - Balanced cap `1.52`는 점수는 조금 높았지만 bootstrap max가
    `1.998404`로 공식 한도에 지나치게 가까워 폐기했다.
  - K1만 금지하는 대안은 max를 약 `2.012`로 악화시켜 폐기했다. AX31 선택까지
    함께 줄이는 cap `1.48`로 바꿔 weighted Dev 감소를 약 `0.000682`로 제한하며
    max를 `1.975269`로 낮췄다.
  - 초기 vector selector audit은 `1..2^40` probe가 고비용 fallback을 충분히
    강제하지 못했고 NumPy의 순차 합이 1 ULP budget edge에서 scalar
    `math.fsum`과 달랐다. high probe, all-Light fallback, exact selected/light
    합과 두 결정적 회귀 테스트로 해결했다.
- 증거:
  - public report SHA-256
    `b91c23706f5ce632d1d97836873c22e4e0946df87a2c71dd604fad93af3cbf2a`
  - all-miss report SHA-256
    `55ee7ae867f2719a4dda751db0c2db97d22a9130ec2d0bc826e6c6a005b5cf34`
- 확인된 원인: exact public cost는 공개 출력 길이 tail을 제거하고, 비공개용
  fallback에서는 여러 alpha word delta와 활성 BERT `[CLS]` residual이
  hash/character 단독보다 품질 순위를 개선한다. Balanced에서 K1만 줄이면
  AX31 추가비용이 bootstrap tail을 대신 채우므로 단일 모델 금지만으로는
  안전해지지 않는다.
- 되돌림/현재 상태: cap `1.52`와 K1-only 대안은 유지하지 않았다. 공식
  protocol/scorer/policy/outcome은 변경하지 않았고 최종 cap `1.48`, exact-sum
  selector와 validator 회귀 테스트를 유지한다.
- 반복 금지 조건: public-hit와 all-miss 결과를 같은 일반화 수치로 합치지 않는다.
  Balanced cap을 독립 bootstrap/group 검증 없이 `1.48`보다 높이지 않고,
  NumPy 선택 결과만으로 scalar runtime parity를 주장하지 않는다.
- 다음 권장 대안: 비용 head를 다시 조정하려면 EXP-010의 선형 UCB 반복이 아니라
  콘텐츠 group holdout과 출력 길이 상위 분위수 목적을 함께 사용한다.

### EXP-20260827-016 — bounded-memory hot path와 현재 wheel/ARM64 이미지 고정

- 시간: 2026-08-27 10:00 KST
- 상태: partial
- 관련 기록: EXP-20260827-011, EXP-20260827-014, EXP-20260827-015
- 목표/가설: 최종 ten-head 정책의 결정과 점수를 바꾸지 않고 Windows 참고
  runtime을 90초 안쪽으로 회복하고, stale wheel/image를 현재 파일에 정확히
  결박한다.
- 변경:
  - `analyze_text`의 여러 문자 통계를 hash와 BERT dense path가 공유한다.
    중간 regex `findall` 최적화는 Unicode 의미는 정확했지만 300,000 Hangul에서
    약 19.65 MiB의 match list를 만들어 폐기했다. 최종 구현은
    `sum(map(str.<predicate>, text))`, 한글 범위 generator와 `str.count`를 써서
    입력 길이와 무관한 작은 transient allocation을 유지한다.
  - Python `>=3.9` wheel metadata와 맞지 않던 `str | None` 한 곳을 이미 import한
    `Optional[str]`으로 바꿨다. 라우팅 산술과 artifact는 변경하지 않았다.
  - 현재 working-tree source manifest를 OCI label에 넣어 이미지를 재빌드했다.
- 동등성/성능 검증:
  - Python 3.13.3의 전체 1,114,112 Unicode code point와 materialized Train+Dev
    2,640문항, 11,169,251문자를 이전 명시적 문자 루프와 비교해 통계 mismatch
    0이었다. full 데이터의 통계 계산은 동일 실행에서 참조 3.111초, 최종
    2.487초였고 300,000 Hangul의 `tracemalloc` peak는 624 bytes였다. Windows
    microbenchmark이므로 공식 ARM 성능 수치로 사용하지 않는다.
  - public full-880 세 tier 파일은 EXP-012의 올바른 full-880 기준 파일과 모두
    byte-identical이었다. 처음 비교한 `build/bert-router-final/dev` 파일은
    Dev-base 868이라 다른 것이었고 코드 회귀가 아니었다.
  - 최종 all-miss Balanced full 2,640은 worker 52.766초, wrapper 53.442초,
    peak working set 134.5 MiB, 2,640 decisions였다. SHA-256
    `c790b1a4b66a455db282c1d29c0fac1c29f27a9766857415583ba3e9e282b057`.
    잘못 선택한 과거 2,604-row 결합 파일의 44.124초는 공식 full-batch 근거에서
    제외한다.
- runtime 변동 실패 분석:
  - 같은 cap `1.48`/같은 선택 수의 이전 측정은 103.846초였지만, host CPU가
    40~57% 사용 중이고 unrelated Docker service가 실행 중이었다. 별도의
    cap `1.52` clean 측정 52.954초와 현재 52.766초를 보면 정책 연산 변화보다
    host contention이 원인이라는 추정이 강하다. 확인된 native ARM 원인은
    아니므로 90초 통과를 보장하는 증거로 쓰지 않는다.
- wheel:
  - `python -m build`는 로컬 `build/` namespace와 설치되지 않은 build frontend
    때문에 실행할 수 없었다. `pip wheel --no-deps --no-build-isolation`도 sandbox
    밖 pip cache 권한으로 한 번 실패했으며 허용된 cache 접근으로 해결했다.
  - 최신 wheel은 10,609,503 bytes, SHA-256
    `e31e4ed0c902d02d9fc999632be74e402cf058302922c9658a6c71317e0bff8b`.
    현재 `src/ossp_router`의 Python/JSON 25개가 모두 포함되고 byte-hash mismatch
    0, `Requires-Python: >=3.9`, `router-run = ossp_router.bert_router:main`을
    확인했다. 격리 `--target` 설치의 toy 세 tier가 모두 성공했다.
- ARM64 image:
  - build 명령은 `docker build --platform linux/arm64 --file
    container/Dockerfile --build-arg SOURCE_MANIFEST_SHA256=b106fcc4...52db4
    --tag ossp-router:check-final-optimized .`.
  - working-tree manifest
    `b106fcc4326eb8ec5454c10e9d51753bc8ff3c89b88f45b1d2f393b7dbb52db4`,
    local OCI index digest
    `sha256:164edb9b83f1135c210aee168a2f9b6e4b3e5672ec90c3a29f0f9f44fd0f6a60`,
    ARM manifest `sha256:ae9488af4a05c4b7e2fe5bb699fa72f0f1cc24a6ff80aa10d5f2891a381fde44`,
    unpacked size 33,239,846 bytes, `linux/arm64`, user `65532:65532`, no
    declared VOLUME, expected entrypoint와 bound label을 확인했다.
  - 이미지 내부에서 선별한 다섯 hot-path runtime module과 여섯 JSON resource의
    길이/SHA가 현재 작업 트리와 모두 같았다. network none/read-only root/2 CPU/2 GiB/no extra
    swap/PID 32/`/tmp` 256 MiB/cap-drop/no-new-privileges 조건의 QEMU toy 세 tier가
    성공했고 각 output에는 정확한 ID 3개를 가진 `submission.json` 하나만 있었다.
- 테스트:
  - protocol/scoring/prompt, BERT router/residual/encoder, hash/char/word,
    public-cost, validator와 baseline 핵심 108개 통과. 전체 Windows suite는
    165개 중 7 fail, 9 error, 1 skip이며 모두 EXP-002와 같은 `fcntl`/`resource`,
    FIFO/symlink privilege/Linux console helper 또는 CRLF raw SHA 범주다. 신규
    router 실패는 없다.
  - `compileall`과 `git diff --check` 통과. 로컬 환경에는 `ruff`와 `reuse` module이
    없어 lint는 실행하지 못했다.
- 확인된 원인: 문자 통계의 Python 다중-counter loop가 hot path였고 여러
  bounded-memory predicate scan이 이 환경에서 더 빠르다. wheel/image stale은
  정책 오류가 아니라 EXP-015 뒤 새 source/artifact를 다시 패키징하지 않은
  release sequencing 문제였다.
- 되돌림/현재 상태: match list 방식은 제거했다. 현재 wheel과 audit image는
  working tree와 일치하지만 아직 사용자가 선택한 최종 commit이나 registry
  digest에 묶이지 않았다. 공식 파일은 수정하지 않았다.
- 반복 금지 조건: regex `findall`을 긴 입력 통계에 다시 사용하지 않는다.
  2,604-row 결합 입력이나 x86_64 QEMU 시간을 full 2,640/native ARM 근거로 쓰지
  않고, source label이 `unbound`이거나 내부 artifact 해시가 다른 이미지를 최종
  증거로 쓰지 않는다.
- 다음 권장 대안: LF를 보존한 clean Linux clone에서 전체 test/ruff/reuse를
  실행한 뒤, 현재 source manifest와 commit을 다시 고정해 native ARM64 장비에서
  `tools/check_runtime.py` 전체 세 tier를 90초 제한으로 실행한다. EXP-011 때문에
  x86_64 QEMU full 배치는 반복하지 않는다.

### EXP-20260827-017 — 현재 all-miss Windows 3-tier 참고 측정 보완

- 시간: 2026-08-27 10:05 KST
- 상태: resolved
- 관련 기록: EXP-20260827-016
- 목표/가설: EXP-016에서 Balanced만 남은 현재-source 측정을 Fast와 Premium에도
  같은 정확한 2,640-row 입력과 독립 프로세스 조건으로 보완한다.
- 변경: 코드, artifact, cap, 공식 파일은 바꾸지 않았다. 같은
  `build/bert-router-final/benchmark_all_miss_runtime.py`에
  `build/word-runtime-full880/public-train-dev-inputs.json`을 전달했다.
- 결과:
  - Fast worker/wrapper `44.037/44.553`초, peak `134.3 MiB`, 선택 수
    Light/AX31/K1 `1540/1100/0`
  - Balanced worker/wrapper `52.766/53.442`초, peak `134.5 MiB`, 선택 수
    `871/1769/0`
  - Premium worker/wrapper `45.423/45.950`초, peak `134.4 MiB`, 선택 수
    `67/2256/317`
  - 각 실행은 2,640 decisions와 lookup hit 0을 확인했다. stdout SHA-256은
    각각 `16246237156ec4e1044f2467166a8d79bda7865164f30b3b3d75b4aadca58ef8`,
    `c790b1a4b66a455db282c1d29c0fac1c29f27a9766857415583ba3e9e282b057`,
    `9d9ca2ff4945b088c084fbab23cd9f9c46f3bca72326a7ac27471692e0421e93`다.
- 분석: 세 tier 모두 이 Windows 참고 실행에서는 90초 이내였고 EXP-016의
  103.846초가 안정적인 코드 성능이 아니라 host contention outlier라는 해석을
  강화한다. tier별 활성 word/character head 집합 차이와 host 변동 때문에
  Balanced가 가장 느렸지만 메모리는 거의 같았다.
- 현재 상태: Windows 3-tier 참고 표는 완성했다. 이 항목은 native ARM64,
  cgroup/PID, LF Linux full suite gate를 해결하지 않으며 EXP-016의 전체 release
  상태는 계속 partial이다.
- 반복 금지 조건: 이 Windows 수치를 공식 ARM64 90초 통과로 표현하지 않는다.
- 다음 권장 대안: 추가 Windows 반복 대신 native ARM64에서 동일 2,640-row 세
  tier를 `tools/check_runtime.py`로 측정한다.

### EXP-20260827-018 — 로컬 native ARM64 실행 노드 부재 확인

- 시간: 2026-08-27 10:09 KST
- 상태: partial
- 관련 기록: EXP-20260827-011, EXP-20260827-016, EXP-20260827-017
- 목표/가설: Docker Desktop에 별도 native ARM64 context나 builder node가 이미
  연결되어 있다면 EXP-011의 QEMU 한계를 반복하지 않고 공식 full gate를 닫는다.
- 실행: `docker context ls`, `docker buildx ls`, `docker info`로 연결된 endpoint,
  builder node와 daemon architecture를 읽기 전용으로 확인했다.
- 결과: context는 `default`와 `desktop-linux`뿐이고 두 buildx node 모두 같은
  Docker endpoint를 사용한다. daemon OS는 Linux지만 architecture는 `x86_64`다.
  buildx의 `linux/arm64` 표시는 QEMU 지원 플랫폼이며 별도 ARM node가 아니다.
- 분석: 현재 환경에는 native ARM64 full 2,640-row 실행을 수행할 수 있는 로컬
  또는 연결된 node가 없다. EXP-011에서 10분을 넘긴 emulated full run을 다시
  실행해도 native 성능 근거가 되지 않는다.
- 현재 상태: current ARM64 image build, 내부 hash parity와 constrained toy는
  유지한다. native full runtime/Linux gate는 외부 native ARM64 환경이 필요해
  미완료다.
- 반복 금지 조건: Docker Desktop의 지원 플랫폼 목록만 보고 native ARM64라고
  판단하지 않으며, 새 native node가 연결되지 않는 한 QEMU full batch를 반복하지
  않는다.
- 다음 권장 대안: Apple Silicon 또는 Linux ARM64 host에서 문서화된
  `tools/check_runtime.py` 명령을 실행하고 report를 저장한다.

### EXP-20260827-019 — LF-normalized native Linux/amd64 전체 suite

- 시간: 2026-08-27 10:18 KST
- 상태: resolved
- 관련 기록: EXP-20260827-002, EXP-20260827-016, EXP-20260827-018
- 목표/가설: Windows full-suite의 POSIX와 raw CRLF 실패를 원본 작업 트리를
  변경하지 않는 Linux 환경에서 분리하고, router와 운영 하네스 전체 단위
  테스트가 LF checkout에서 통과하는지 확인한다.
- 환경: local image `mission15-jupyter:latest`, immutable local image ID
  `sha256:822a7b98f5833ea66cd72cd649696ebd4e23ce0a2460e3cdb3df8af4a153737a`,
  native `linux/amd64`, Python 3.11.15, NumPy 2.4.6. network none, read-only
  root, 2 CPU, 2 GiB/no extra swap, PID 64와 executable `/tmp` 1 GiB를
  사용했다.
- materialization: `/repo` read-only bind를 `/tmp/repo`로 복사하되 `.git`,
  `.venv`, `build`, `dist`, `*.egg-info`, `__pycache__`를 제외했다. `.gitattributes`
  `eol=lf` checkout을 모사하도록 text extensions와 `LICENSE`, `NOTICE`,
  Dockerfile류의 CRLF만 LF로 바꿨다. 원본 작업 트리 파일은 수정하지 않았다.
- 실행: snapshot에서 `PYTHONPATH=/tmp/repo/src python -B -m unittest discover
  -s tests -p 'test_*.py'`.
- 중간 실패와 원인:
  - 첫 native Linux mount 실행은 324 tests에서 6 fail/2 error였다. 5 fail은
    Windows CRLF raw SHA, 1 fail은 test container의 과도한 isolation이 timeout
    helper를 infrastructure failure로 바꾼 것, error는 `/tmp` execute 제한과
    NumPy 부재였다.
  - NumPy 포함 이미지와 executable `/tmp`로 바꾼 직접 mount는 325 tests에서
    CRLF 5 fail과 console-script error 1개만 남았다. 후자는 작업 트리에 남은
    generated `src/*.egg-info`와 `PYTHONPATH` 때문에 pip가 test wheel을 이미
    설치된 것으로 오인한 환경 오염이었다. 별도 final wheel Linux venv에서는
    `ossp-router`와 `router-run` 생성·실행이 정상임을 확인했다.
  - 첫 LF snapshot은 확장자 없는 root `LICENSE`를 정규화 목록에서 누락해 1
    fail이었다. `LICENSE`/`NOTICE`를 포함한 두 번째 snapshot으로 해결했다.
- 최종 결과: 325 tests 모두 통과, Docker CLI/opt-in이 필요한 integration 12개는
  조건부 skip. `fcntl`, `resource`, FIFO, symlink, timeout helper, wheel build,
  repository raw-hash와 새 BERT/router/validator tests가 모두 통과했다.
- 확인된 원인: Windows full-suite 실패는 router 회귀가 아니라 POSIX API 부재,
  권한과 CRLF checkout 문제였다. `.gitattributes`를 적용한 LF Linux snapshot은
  기대 hash와 동작을 만족한다.
- 현재 상태: Linux/POSIX 전체 unit-suite gap은 해소했다. 이 테스트는 amd64이고
  Docker-in-Docker runtime integration은 skip됐으므로 EXP-018의 native ARM64
  full 2,640-row runtime blocker는 해소하지 않는다. host에는 `ruff`/`reuse`가
  없어 별도 lint gate도 남아 있다.
- 반복 금지 조건: Windows raw 파일을 expected SHA에 맞추려고 수정하지 않는다.
  test snapshot에서 `*.egg-info`를 포함하거나 `/tmp`를 noexec로 만들어 같은
  packaging 환경 오류를 반복하지 않는다.
- 다음 권장 대안: clean committed LF clone에서 `ruff check .`, `reuse lint`와
  Docker integration opt-in을 실행하고, native ARM64 host에서 full runtime
  report를 생성한다.

### EXP-20260827-020 — fail-closed source binding과 최종 lint/package 재고정

- 시간: 2026-08-27 10:48 KST
- 상태: resolved
- 관련 기록: EXP-20260827-016, EXP-20260827-018, EXP-20260827-019
- 목표/가설: EXP-016의 수동 source label 결박은 안전했지만 기본 README build와
  `tools/check_runtime.py`가 `unbound` 또는 stale image를 놓칠 수 있었다. 실제
  Docker build 입력까지 manifest에 넣고 일반 검사 경로를 fail-closed로 만든다.
- 변경:
  - 새 표준-library `ossp_router.source_manifest`에 source scope와 deterministic
    hash를 공통화했다. scope는 `.dockerignore`, 두 Dockerfile, entrypoint, 전체
    `src`, 두 baseline source와 hash-regex artifact이며 symlink/missing path를
    거부한다.
  - `benchmark_runtime.py`는 공통 helper를 사용하고, `.dockerignore`를 manifest
    entry/test에 포함한다. `check_runtime.py`는 이미지의
    `io.sktelecom.ossp.source-manifest-sha256` label이 없거나 현재 source hash와
    다르면 컨테이너를 실행하기 전에 실패한다.
  - README build 예시에 manifest 계산과 `--build-arg`를 추가했다. 같은 문서의
    fairness 문구도 허용된 현재 tier와 content만 사용하고 금지 대상은
    `challenge_id/split/episode_id/order`라고 명확히 했다.
  - public cost 문서는 lookup row에만 content hash/cost가 있고 선택에 노출되지
    않는 `training_summary`에는 공개 provenance가 있음을 구분했다.
- manifest 결과: 33 entries, SHA-256
  `00dea9217299e5b3e7d8709fa2e4393f8a4b47686960555ada9f01227d66bf4f`.
  EXP-016의 `b106fcc4...` label은 새 `.dockerignore` scope 이전 값이므로 현재
  release evidence에서 supersede했다.
- lint/license:
  - 격리 도구 Ruff 0.16.4 첫 실행은 `test_public_cost_lookup.py`의 E402 import
    순서 1건을 찾았고 수정 후 `ruff check .`가 통과했다. 설치돼 있던 pyflakes도
    전체 `src/baselines/tests/container/tools`에서 통과했다.
  - REUSE 6.2.0 첫 실행은 SPDX header가 있는 한글 `AGENTS.md`와
    `docs/OPERATIONS.md`의 인코딩 감지 2건을 놓쳤다. `REUSE.toml` override를
    추가한 뒤 131/131 files, bad/missing/read error 0으로 REUSE 3.3 준수 통과했다.
  - lint 도구는 `build/lint-tools`에만 격리 설치했으며 repository dependency,
    wheel 또는 runtime image에 추가하지 않았다.
- 테스트:
  - source-manifest/check-runtime/repository 테스트를 보강했다. LF-normalized
    native Linux/amd64 Python 3.11.15에서 전체 326 tests 통과, opt-in
    Docker-in-Docker 12개 skip. Windows의 public-cost tests 5개 통과,
    repository policy는 예상 CRLF raw-hash 3건만 실패했다.
  - `compileall`, `git diff --check`, Ruff, pyflakes와 REUSE 모두 통과했다.
- 최신 wheel:
  - 10,610,926 bytes, SHA-256
    `a289c9dc35c0d487fd2ff6b313c138362b862f52df311a5dcac811a70e12f322`.
    현재 Python/JSON 26개 hash mismatch 0, README metadata line parity,
    Python `>=3.9`, `router-run` entrypoint와 isolated toy 세 tier를 확인했다.
  - report `build/bert-router-final/wheel-validation-report-bound.json`, SHA-256
    `52cb6a0a2b550c710f84d54ae18ce0f807603141963c949087aa8619c95f0e07`.
- 최신 ARM64 audit image:
  - tag `ossp-router:check-final-bound`, local OCI index
    `sha256:762ba83d473d8a80e65fad3b6364be568bcd79a8c4f6176be060783bf9b829f7`,
    ARM manifest `sha256:2aefbf02ec33ce11adec0ae55df3d388975a4369573d1c2744a2e453d8fa327f`,
    size 33,240,692 bytes, `linux/arm64`, UID `65532:65532`, no VOLUME,
    expected entrypoint와 새 bound label을 확인했다.
  - 처음 host package Python/JSON 26개 전부가 image에 있을 것으로 가정한
    진단은 `public_runtime.py`, `tiebreak_latency.py` 두 operator-only 파일이
    whitelist에서 의도적으로 제외되어 실패했다. 제외 집합을 정확히 고정한
    재검증에서 image-admitted 24개 hash mismatch 0이었다.
  - 공식 격리 조건의 QEMU toy Fast/Balanced/Premium이 모두 성공했다. JSON
    결정은 최신 wheel toy와 같고 각 output volume은 `submission.json` 하나와
    정확한 ID 세 개만 포함했다.
- 확인된 원인: 기존 manifest가 Dockerfile과 copied source는 묶었지만 build
  context selection 자체인 `.dockerignore`를 빠뜨렸고, 일반 runtime checker는
  image config/platform만 검증했다. 공통 hash helper와 label comparison으로
  다음 build의 누락을 자동 차단한다.
- 되돌림/현재 상태: EXP-016/017의 라우팅 정책, score/cost, artifact와 runtime
  timing은 변경하지 않았다. 이전 image/wheel은 삭제하지 않고 새 digest/report로
  supersede했다. 아직 최종 commit과 registry digest는 없어 audit image이며,
  `submission-ossp-skt.json`은 작성하지 않았다.
- 반복 금지 조건: `.dockerignore`를 source manifest 밖으로 빼거나
  `SOURCE_MANIFEST_SHA256=unbound` 이미지를 일반 runtime report에 사용하지 않는다.
  최소 image parity에서 operator-only 제외 파일을 runtime 누락으로 오판하지
  않는다.
- 다음 권장 대안: 현재 파일을 LF clean commit으로 고정한 뒤 그 commit에서
  manifest/image를 한 번 더 빌드하고, native ARM64 host의 full 2,640-row 세
  tier report와 registry immutable digest를 생성한다.

### EXP-20260827-021 — 수동 native ARM64 GitHub Actions 게이트 준비

- 시간: 2026-08-27 11:21 KST
- 상태: partial
- 관련 기록: EXP-20260827-011, EXP-20260827-018, EXP-20260827-020
- 목표/가설: 로컬 x86_64 Docker Desktop에서 반복할 가치가 없는 QEMU full
  batch 대신, 공개 GitHub 저장소에 제공되는 native Linux/ARM64 hosted runner를
  명시적으로 실행할 수 있는 fail-closed 경로를 저장소에 두면 남은 2,640문항
  90초 게이트를 정확한 commit에서 닫을 수 있다.
- 실행 환경 조사:
  - Windows host와 process는 `AMD64`/`X64`이고 WSL2 배포판은 x86_64
    `docker-desktop` 하나뿐이다.
  - Docker context는 `default`, `desktop-linux`뿐이며 두 buildx node는 같은
    x86_64 Docker Desktop worker를 사용한다. `linux/arm64` 표시는 binfmt/QEMU
    지원이고 native node 증거가 아니다.
  - SSH host/agent, Colima, Multipass, Vagrant, Podman, libvirt, VirtualBox,
    VMware, Docker Offload 연결은 없다.
  - Azure 구독 상태는 활성이나 조회된 VM은 없었다. GCP CLI의 저장 인증은
    재인증이 필요해 비대화형 조회를 계속할 수 없었다. 새 cloud VM 생성은 비용과
    외부 상태 변경이므로 수행하지 않았다.
- 변경:
  - `.github/workflows/native-arm64-runtime.yml`을 추가했다. trigger는
    `workflow_dispatch` 하나뿐이고 권한은 `contents: read`이다.
  - workflow는 `ubuntu-24.04-arm`에서 host `uname -m`, Docker daemon
    architecture와 cgroup v2를 먼저 검사한다. 하나라도 native ARM64 증거와
    다르면 materialization/build 전에 실패한다.
  - 고정 public source를 materialize하고 현재 source manifest를 build arg로 넣어
    `linux/arm64` 이미지를 다시 만든 뒤, `tools/check_runtime.py`로 2,640문항의
    Fast/Balanced/Premium을 CPU 2, memory 2 GiB/no extra swap, PID 32, tier별
    90초 제한에서 실행한다.
  - report의 native Docker platform, 2,640 rows, 세 tier, 90초 이하와 전체 pass를
    다시 assert하고 report/image inspect/environment/SHA256SUMS를 commit SHA가
    포함된 Actions artifact로 올린다.
  - README와 `docs/ROUTER_MODEL.md`에 수동 실행 경로와 아직 실행되지 않았다는
    한계를 기록하고, repository policy test에 manual-only/fail-closed marker를
    고정했다.
- 검증:
  - PyYAML 구조 검사와 workflow의 Bash run block 5개 `bash -n` 통과.
  - 신규 required-file/workflow/SPDX repository policy test 3개 통과.
  - Ruff 0.16.4 `All checks passed`; REUSE 6.2.0은 132/132 files, bad/missing/read
    error 0으로 통과했다. `compileall`과 `git diff --check`도 통과했다.
  - workflow/docs/tests는 runtime source manifest scope 밖이므로 현재 33-entry
    manifest `00dea9217299e5b3e7d8709fa2e4393f8a4b47686960555ada9f01227d66bf4f`
    와 bound image는 변경되지 않았다.
- 확인된 원인: 현재 로컬에는 진짜 ARM64 execution node가 없으므로 여기서
  workflow 결과를 생성할 수 없다. GitHub Actions workflow는 commit/push와
  계정의 수동 dispatch 전에는 실행 대상 revision이 존재하지 않는다.
- 현재 작업 트리: router score/cap/artifact와 runtime image는 변경하지 않았다.
  workflow와 문서·회귀 테스트만 추가했으며 commit, push, Actions dispatch,
  cloud resource 생성은 수행하지 않았다.
- 반복 금지 조건: 현재 x86_64 context에서 full QEMU batch를 native 증거로 다시
  실행하지 않는다. workflow가 실제 실행되기 전에는 문서의 native gate를
  통과 또는 `final-frozen`이라고 바꾸지 않는다.
- 다음 권장 대안: 사용자 승인 범위에서 현재 LF 작업 트리를 commit/push한 뒤
  `Native ARM64 runtime gate`를 수동 실행한다. 성공 artifact의 report SHA-256,
  image ID, tier별 시간과 resource 제한을 확인하고 이 항목을 참조하는 resolved
  기록을 새로 추가한다.

### EXP-20260827-022 — BERT 기여를 유지한 공개 기록 경신 정책

- 시간: 2026-08-27 14:54 KST
- 상태: resolved
- 관련 기록: EXP-20260826-004, EXP-20260826-006, EXP-20260827-015
- 목표/가설: from-scratch 1-layer BERT-style residual을 실제 선택에 유지하면서
  저장소에 남은 공개 routing 기록을 같은 모집단에서 넘고, 세 tier의 실제 비용
  한도와 bootstrap 안전성을 함께 지킨다.
- 기준 버전: 256-bin hash-regex, 60,000-feature character TF-IDF,
  120,000-feature/10-head word TF-IDF, hidden 16·sequence 32·two-head의 한 층
  bidirectional self-attention artifact를 통합한 working tree.
- 변경 사항:
  - public exact-cost Balanced의 AX31/K1 component weight, BERT weight와 margin을
    public search 후보로 향한 `t=0.904` 보간점에 고정했다. 향상된 선택 plateau는
    `0.90200..0.90434`, 동일 choice plateau는 `0.90397..0.90434`였다.
  - BERT artifact나 tokenizer는 pretrained 모델에서 가져오지 않았다. hashed
    word/position/type embedding, `[CLS]`, bidirectional attention, residual,
    LayerNorm, GELU FFN을 공개 Train에서 직접 학습한 구조를 그대로 사용했다.
- 데이터/환경: materialized public Dev 880, Dev-base 868, Train 1,760; public
  outcome은 offline scorer와 비용 calibration에만 사용하고 runtime 후보 모델이나
  네트워크는 호출하지 않았다.
- 실행 명령: `baselines/validate_bert_router.py`의 full-point/ablation 및 seed
  `20260831` 5,000회 rerouted bootstrap. 정책 보고서는
  `build/bert-router-final/public-risk-5000-record-winning.json`.
- 결과:
  - Fast: `0.682670454545 / 1.199376956326`, Light 424 / AX31 456 / K1 0
  - Balanced: `0.714772727273 / 1.843463991877`, Light 177 / AX31 627 / K1 76
  - Premium: `0.757670454545 / 3.589752398761`, Light 115 / AX31 639 / K1 126
  - full-Dev weighted `0.714801136364`; Dev-base 868에서 `0.714228110599`로
    이전 최고 연구 기록 `0.713623271889`보다 `0.000604838710` 높다. 공개
    hash-regex `0.695369318182`도 full-Dev에서 `0.019431818182` 넘는다.
  - Train weighted `0.738906250000`; 세 tier 비용은
    `1.199692382516/1.845635538587/3.592935536518`이다.
  - BERT-hybrid 전체 제거 시 `0.708210227273`, Transformer `[CLS]`만 0으로
    만들면 `0.700056818182`, word 제거 시 `0.711619318182`였다. BERT 제거
    delta는 `0.006590909091`이며 Transformer 상태 개입은 tier별
    `82/221/119`개 선택을 바꿨다.
  - 5,000 bootstrap의 q95/q99/max는 Fast
    `1.199911983/1.199984235/1.199999552`, Balanced
    `1.849614527/1.849918714/1.849999782`, Premium
    `3.598771329/3.599753613/3.599994651`; 목표와 공식 한도 초과 0회,
    scalar selector parity 81회/tier였다.
- 증거: ablation 보고서 SHA-256
  `310cac37e028a46f83b8e5bc4a296083a4fc3269e76d732e36b84259884678ac`,
  public risk 보고서 SHA-256
  `81c436941446f956ec3738f5cf19f0c0e70fff4fcb91382652fea3e2db130ab3`.
- 확인된 원인: 공개 Dev에서 BERT residual과 word head가 서로 다른 승격 경계를
  개선했고, Balanced의 안정적인 보간 plateau가 이전 기록을 넘겼다.
- 추정: 공개 Dev를 보고 선택한 작은 차이이므로 비공개 일반화 개선 폭은 보장할
  수 없다.
- 되돌림/현재 상태: 새 설정을 `PUBLIC_COST_TIER_CONFIGURATIONS`에 유지한다.
  공식 policy/scorer/outcome 파일은 수정하지 않았다.
- 반복 금지 조건: full 880과 base 868 기록을 같은 모집단처럼 비교하거나,
  public calibration 결과를 private split 성능 주장으로 표현하지 않는다.
- 다음 권장 대안: private 입력에는 아래 EXP-023의 learned-cost fallback과
  family holdout 근거를 사용하고 native ARM64 gate를 별도로 닫는다.

### EXP-20260827-023 — Premium fallback tail-risk와 LOGO 증거 보강

- 시간: 2026-08-27 14:54 KST
- 상태: resolved
- 관련 기록: EXP-20260826-003, EXP-20260826-005, EXP-20260827-022
- 목표/가설: 모든 public-cost lookup이 빗나가는 private-proxy 경로에서 Premium
  콘텐츠 그룹 비용 여유를 `3.8` 아래로 넓히면서, ID/순서 독립성과 BERT 포함
  정책의 양의 품질 증가를 보존한다.
- 변경 사항:
  - Premium all-miss `predicted_cost_cap`을 `3.02`에서 `2.83`으로 낮췄다.
    다음 choice transition은 `2.830086800732428...`이어서 `2.8301`은 더 위험한
    그룹 선택으로 되돌아가며, `2.83`을 가장 큰 단순 안전값으로 골랐다.
  - `validate_content_logo.py`에 일곱 content-only family outer holdout을 고정하고,
    canonical outcome을 protocol로 다시 읽어 input episode/model 순서로 정렬한 뒤
    공식 Decimal 비용식과 cached score/cost 행렬의 exact parity를 강제했다.
  - 보고서는 learned cost가 주 selector 입력이고 실제 공개 비용은 complement의
    후보 feasibility/group safety calibration에만 사용된다는 점을 분리했다.
    held-out score/cost는 정책 선택 뒤 한 번 평가한다.
- 데이터/환경: Dev 880, Train 1,760, Dev-base 868; bootstrap seed `20260908`
  5,000회; LOGO seed `20260917`, random 100/tier, refine seeds 2, seed당 10,
  exact screen 32.
- 결과:
  - Dev all-miss Fast: `0.669602272727 / 1.136063167305`, 517 / 363 / 0
  - Dev all-miss Balanced: `0.692613636364 / 1.487633268423`, 286 / 594 / 0
  - Dev all-miss Premium: `0.731534090909 / 2.933354347258`, 30 / 740 / 110
  - weighted `0.695085227273`; old cap 대비 `-0.001363636363`. Dev-base는
    `0.704089861751`, Train은 `0.699914772727`; public exact-cost 기록은 변하지
    않았다.
  - Premium bootstrap q95/q99/max
    `3.255016332/3.374887248/3.581779110`, 공식 한도와 `3.8` 초과 모두
    0/5,000. 일곱 family 중 최악 `other` 비용은 `3.729621598`이다.
  - primary learned-cost LOGO stitched 결과는 Fast
    `0.644602272727/1.107772853/707·173·0`, Balanced
    `0.660511363636/1.463398357/465·415·0`, Premium
    `0.694886363636/2.538282625/94·764·22`; weighted `0.664460227273`으로
    all-Light보다 `0.045142045455` 높다. 21 fold×tier 모두 공식 한도와 내부
    `1.18/1.70/3.40` 목표를 통과했다.
  - cached canonical score/cost는 모두 max-abs `0.0`; 현재 all-miss 2,640개
    row choice mismatch 0. secondary exact-public stitched score는
    `0.687698863636`이다.
- 실패 증거: cap `3.02`의 v2 `other` Premium 비용은 `3.969259`로 공식 한도에
  너무 가까웠다. 첫 LOGO 보고서는 실제 complement cost 사용을 충분히 구분하지
  않았고 outcome 파일 hash를 cached actual matrix 계산에 강제 결박하지 않았다.
- 원인:
  - 확인됨: 소수 K1/AX31 choice transition이 조건부 출력비용 tail을 크게 바꿨고,
    보고서 provenance 검사는 shape/hash만으로는 cached matrix 변조를 배제하지
    못했다.
  - 추정: family heuristic 자체와 Train-family 겹침 때문에 LOGO score는 실제
    unseen-family 성능을 낙관할 수 있다.
- 증거: all-miss 보고서 SHA-256
  `421e7b654f716fa01a112dbe5ba1711046a5986955ff3616d99061150af2e838`,
  LOGO 보고서 SHA-256
  `18ac6cad392b1a2f748cfb074fe69d8712895438a4dba90163ec0b320112e53a`.
- 되돌림/현재 상태: cap `2.83`과 강화한 validator/tests를 유지한다. predictor는
  public Train에 고정되어 있으므로 LOGO를 end-to-end unseen-family 또는 private
  일반화 주장으로 사용하지 않는다.
- 반복 금지 조건: 전체 Dev 평균만으로 Premium 안전을 주장하거나 exact public
  cost를 learned selector 입력이라고 잘못 기록하지 않는다.
- 다음 권장 대안: 실제 private-like 별도 콘텐츠가 허용될 경우 frozen policy를
  수정하지 않은 blind audit을 추가한다.

### EXP-20260827-024 — 최종 source-bound artifact와 Linux gate 재고정

- 시간: 2026-08-27 14:54 KST
- 상태: partial
- 관련 기록: EXP-20260827-016, EXP-20260827-020, EXP-20260827-021
- 목표/가설: 기록 경신 설정, cap `2.83`, bounded hash cache와 runtime 검사 수정이
  모두 들어간 정확한 source를 재현 wheel과 ARM64 image에 결박하고 release
  회귀 gate를 닫는다.
- 변경 사항:
  - hash feature FNV cache를 고정 크기 LRU `262,144`로 늘렸다. 2,640-row
    hash-only audit의 miss는 `1,396,756 -> 720,745`, 시간은
    `12.779 -> 9.336`초였고 선택은 변하지 않았다.
  - `inspect_image_runtime_metadata`가 Docker `.Config.Labels`를 여섯 번째 bounded
    line으로 요청·검증·반환하도록 고쳤다. 기존 구현은 실제 label을 버려
    source-bound image도 `tools/check_runtime.py`에서 거부할 release blocker였다.
  - 실제 Linux suite에서 이전 5-line fake Docker inspect fixture 세 개가 새
    계약을 위반해 실패한 것을 확인하고 `Labels=null` 여섯 번째 줄을 추가했다.
- source/artifact:
  - source manifest 33 entries, SHA-256
    `81b6244ef524880ae17adeabef3ac1068f030c26b42ebdbe1e8a93dd6e20227d`.
  - fixed epoch으로 두 번 만든 wheel은 byte-identical, 10,611,419 bytes,
    SHA-256 `394866405b7d48a4ca935b70e3dc38a9d124c8b421f6473ca24068ccfc760a75`.
    26개 Python/JSON payload parity, README metadata, Python `>=3.9`, 실제
    `router-run`의 toy 세 tier와 source entrypoint byte parity를 확인했다. 보고서
    SHA-256은 `4ba8f4b59a7a85ad91858fc49f95b386e02ffdf56906767bd13998fdd8cab676`.
  - ARM64 audit image index
    `sha256:7aa070330fe45c17a44eb2623fbd03368c83bdcda56635e7e6e0e41ec2de81a8`,
    ARM manifest
    `sha256:7af3b8de1f058c86ef76cb17a27b6a80fdf89a13875a13b063c03c14d87f6265`,
    size 33,240,758 bytes. label은 위 source manifest와 같고 UID
    `65532:65532`, no VOLUME, expected entrypoint, admitted 25 files hash
    mismatch 0이다.
- 검증:
  - LF-normalized native Linux/amd64 Python 3.11.15에서 334 discovered 중
    322 pass, Docker-in-Docker opt-in 12 skip. runtime module 83개는 73 pass,
    integration 10 skip이다.
  - Ruff 0.16.4, `compileall`, `git diff --check` 통과. REUSE 6.2.0은
    134/134 files, bad/missing/read error 0이다.
  - QEMU ARM64 toy는 network none/read-only root/CPU2/2GiB/no extra
    swap/PID32/`/tmp`256MiB/cap-drop/no-new-privileges에서 tier별 두 번 실행해
    byte-identical했고, 매번 ID 3개와 output root의 `submission.json` 하나만
    확인했다. 보고서 SHA-256은
    `dbed3e901b03e8b489bc5295fcad4903e942df9c5da01d351c4b2bb935fa389f`.
- 실패 증거: label parser 수정 뒤 오래된 fake inspect fixture는 세 테스트를
  `inspect 결과 줄 수 오류`로 실패시켰다. fixture 수정 후 전체 suite가 통과했다.
  현재-cache Windows full all-miss Balanced는 host contention 상태에서 worker
  `82.186937`초였고 정확한 선택 `871/1769/0`을 냈지만 native ARM64 근거가 아니다.
- 확인된 원인: package/image를 정책 변경 전에 만들면 cap/cache/runtime source가
  stale해지고, Config label을 inspect 결과에 포함하지 않으면 fail-closed source
  binding이 실제 integration에서 항상 실패한다.
- 현재 상태: local wheel과 local audit image는 working tree에 정확히 결박됐다.
  공식 protected policy/scorer/outcome은 수정하지 않았다. commit/push/registry
  publish는 하지 않았다.
- 미해결: 로컬 Docker daemon은 x86_64라 full 2,640-row QEMU는 EXP-011처럼
  10분을 넘고 native timing 근거가 아니다. manual GitHub Actions workflow도
  아직 default branch revision과 사용자 dispatch 권한이 없어 실행되지 않았다.
- 반복 금지 조건: stale wheel/image를 current라고 부르거나 QEMU toy/Windows
  timing을 native ARM64 90초 통과라고 표현하지 않는다. native node 없이 full
  QEMU batch를 반복하지 않는다.
- 다음 권장 대안: 사용자 승인으로 LF clean commit을 default branch에 push한 뒤
  `Native ARM64 runtime gate`를 수동 dispatch하고, 세 번의 2,640-row/tier report
  SHA와 native runtime을 이 항목을 참조하는 `resolved` 기록으로 추가한다.

### EXP-20260827-025 — cap 2.83 Base/Train 진단 보고서 고정

- 시간: 2026-08-27 15:01 KST
- 상태: resolved
- 관련 기록: EXP-20260827-023, EXP-20260827-024
- 목표/가설: EXP-023에 기록한 Dev-base 868과 Train 1,760 all-miss 수치가 이전
  cap `3.02` smoke report가 아니라 현재 predicted-cost selector cap `2.83`에서
  재현되는지 영구 보고서로 결박한다.
- 실행 명령: `baselines/validate_bert_router.py --all-miss
  --bootstrap-repetitions 1 --seed 20260908 --skip-ablation`을 각각
  `data/dev/inputs-base.json`과 `data/materialized/train/inputs.json`에 실행했다.
- 결과:
  - Dev-base 868 weighted `0.704089861751`; Fast
    `0.677995391705/1.150265359257/514·354·0`, Balanced
    `0.703052995392/1.547750009511/280·588·0`, Premium
    `0.739919354839/3.171481618665/29·730·109`.
  - Train 1,760 weighted `0.699914772727`; Fast
    `0.672727272727/1.101577175676/1023·737·0`, Balanced
    `0.703267045455/1.417733404341/582·1178·0`, Premium
    `0.732812500000/2.780449271518/45·1547·168`.
- 증거:
  - `build/bert-router-final/all-miss-base868-cap2.83.json`, SHA-256
    `12b14bbd1561b7571ac5fa6838e945f34f07d6865b5405e81dbb6c692d8d5485`.
  - `build/bert-router-final/all-miss-train1760-cap2.83.json`, SHA-256
    `313d5522ecf7e74be77504a32c9563ae0f127e8f0497fc01f2d1023d019d9700`.
- 현재 상태: 문서가 두 current-cap 보고서를 직접 가리킨다. `2.83`은 learned
  predicted cost에 적용되는 selector cap이며 실제 scored cost ratio의 상한이
  아니라는 문구도 명시했다.
- 반복 금지 조건: 이전 `all-miss-*-smoke.json`의 cap `3.02` 결과를 현재 정책
  근거로 사용하지 않는다. bootstrap 1회 보고서를 tail-risk 증거로 사용하지
  않고, tail-risk에는 Dev880 5,000회 보고서만 사용한다.
- 다음 권장 대안: 없음. native ARM64 full runtime blocker는 EXP-024를 따른다.
