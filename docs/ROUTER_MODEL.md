<!--
SPDX-FileCopyrightText: Copyright 2026 ossp-bokhyeon-routing contributors
SPDX-License-Identifier: Apache-2.0
-->

# BERT-style hybrid router

The submitted program is a deterministic prompt-only batch router. It does
not generate answers, call a candidate model, use the network, or retry a
decision. It predicts content-dependent quality and cost, then applies one
batch-level Lagrangian selector with a fixed model-order tie break.

## Architecture

The quality path combines four local representations:

1. The released 256-bin signed word unigram/bigram hash-regex ridge heads.
2. Character 3–5-gram TF-IDF ridge heads with 60,000 features and 19 full-text
   statistics. N-grams use a deterministic 4,096-character head/tail view;
   the statistics inspect the complete text.
3. Full-token word unigram/bigram TF-IDF ridge score-delta heads with 120,000
   features. The artifact exports ten Light-relative quality heads: one
   AX31-minus-Light and one K1-minus-Light head for each ridge alpha in
   `0.1/1/3/10/30`. Public-hit and all-miss policies select their frozen heads
   from this common artifact.
4. A from-scratch BERT-style residual encoder: hashed word embeddings,
   position and token-type embeddings, `[CLS]`, one bidirectional two-head
   self-attention block, residual connections, LayerNorm, GELU feed-forward
   layers, and a dense feature branch. Its sequence length is 32 and hidden
   size is 16.

The fourth component is a BERT *structure*, not a downloaded BERT model. No
pretrained weights or tokenizer are used. Removing the complete BERT-hybrid
residual from the final public-hit policy lowers full Dev weighted score from
`0.714801136364` to `0.708210227273`, so this branch is a measured part of the
decision rule. Zeroing only the Transformer `[CLS]` state while retaining the
dense residual branch lowers the score to `0.700056818182` and changes
82/221/119 Fast/Balanced/Premium choices. The zero state is an
out-of-distribution intervention, so its larger delta is evidence that the
learned Transformer path is active, not an independent estimate of private-set
generalization. Removing the word component lowers the score to
`0.711619318182`.

For an upgrade model `m`, the public-hit quality utility is:

```text
w_hash * (hash[m] - hash[Light])
+ w_char * (char[m] - char[Light])
+ w_word * word_delta[m]
+ beta * (BERT_residual[m] - BERT_residual[Light])
+ margin
```

AX31 and K1 have separate word heads, character heads, blends, BERT weights,
and margins in each tier. Light utility is zero. The frozen values are in
`PUBLIC_COST_TIER_CONFIGURATIONS` in `ossp_router.bert_router`.

### Selection-preserving runtime implementation

Each episode computes shared character statistics once with `analyze_text`.
The character count, non-space count, Hangul count, Unicode decimal and digit
counts, uppercase and symbol counts, and newline and period counts are reused
by both the hash-regex prompt features and the 19-feature dense vector. The
normalized hash token sequence is likewise reused by the BERT encoder; the
word and character TF-IDF analyzers retain their separately trained token and
n-gram definitions.

The shared statistics use bounded-memory Unicode predicate scans rather than
materializing regex match lists. An exhaustive comparison over all 1,114,112
Unicode code points and a differential check over all 2,640 materialized
Train+Dev prompts found no difference from the original per-character
reference loop. A 300,000-Hangul adversarial check used 624 bytes of traced
temporary Python allocation inside `analyze_text`.

Hot dense products in the hash, character, BERT and blend paths feed
`operator.mul` directly to `math.fsum`. This removes Python generator
multiplication overhead while retaining the existing multiplication and
deterministic summation order. These runtime changes do not add a dependency
or change a fitted feature definition.

## Cost handling and fallback

The packaged public-cost table maps `SHA-256(prompt UTF-8)` to the three costs
published in the public Train/Dev outcomes. Its 2,640 sorted lookup rows
contain hashes and costs only—no prompt text, episode ID, split key, source
name, score, answer, label, or routing decision. Non-runtime
`training_summary` provenance names public cost sources and file hashes, but
the parser does not expose that metadata to selection. Exact public
prompt/content lookup is allowed by the challenge rules.

For lookup hits, the selector uses those exact public costs and target caps
`1.20/1.85/3.60`, leaving margin below the official `1.25/2/4` limits. A
mixed-batch miss is fixed to Light while matched rows are optimized; adding a
positive Light cost to both numerator and denominator cannot increase a
ratio already above one. If every prompt misses, upgrade quality is predicted
as a tier-specific Light-relative blend of hash, character, word and BERT
components. Cost remains the existing learned hash/character log-cost path,
including monotone model-cost repair, the Fast extreme-polynomial Light guard,
the Premium short-code K1 guard, and conservative predicted-cost selector
caps `1.15/1.48/2.83`. These constrain learned costs during selection; they
are not upper bounds on realized scored cost ratios.
Thus public lookup does not replace the generalizing private-input path.
Artifact corruption, non-finite prediction, or selection failure falls back
deterministically to all-Light.

Runtime selection never reads `challenge_id`, `split`, `episode_id`, input
position, or source metadata. Plain-prompt lookup does not accept a joined
`messages` string. Matched rows are sorted by content digest before floating
point optimization. All-miss rows are likewise sorted by a SHA-256 digest of
their canonical prompt or role/content message structure before the learned
selector runs. Both paths map results back to input IDs afterward.

## Packaged artifacts

All files are LF-canonical JSON and bind to policy SHA-256
`7c892c423da5fa762e7e1a93b9fa071be51e259b65d2b63a5ba434c4342d7a8e`.

| Resource | Bytes | SHA-256 | Purpose |
| --- | ---: | --- | --- |
| `hash-regex-public.v1.json` | 65,339 | `c5f0545f20b902143ccb78ad174ccd5408f4c28d0898943e82e7951b6a8b9871` | Released hash score/cost heads, LF-normalized |
| `char-tfidf-ridge.v1.json` | 20,774,565 | `9d40b603a36a81546058ea78ba3f4e43fc675efad13c769d5f7ccefadb368004` | Character vocabulary, IDF, ridge and dense heads |
| `tiny-bert-residual.v1.json` | 1,041,270 | `7b640e85c9b0ac906a1ac57de4c42a3d25efc513ea6fbdd614774262e6f0611e` | One-layer BERT-style Transformer residual |
| `word-tfidf-ridge.v1.json` | 7,926,942 | `120af8f95c76c1c560d660e7a6e878f8da982dcda5f6570253945806350bdea3` | Ten word TF-IDF Light-relative quality heads for alpha `0.1/1/3/10/30` |
| `public-content-costs.v1.json` | 283,417 | `ace4384a69d9a4d3ef60798f3b4bf55dcafe094297125fa57f06e7beefcfac14` | Public content hashes and model costs |

Word IDF and coefficients are exported as little-endian float32, zlib level 9,
base64. The parser bounds decompression, rejects duplicate keys and
non-finite values, verifies dimensions/policy/dense-feature bindings, and
exposes decoded arrays read-only. In the public-hit calibration audit,
float32 export changed no decision on Dev 880, Dev-base 868, or Train 1,760
relative to the search matrices; the earlier measured maximum prediction
difference from float64 was `1.5875e-8`. The ten-head all-miss path has
separate lookup-disabled validation below.

The released hash resource retains the organizer's copyright. The character,
word, BERT, and cost-lookup artifacts are generated by the routing
contributors and distributed under Apache-2.0. They contain fitted global
parameters or public content hashes/costs, not candidate-model weights.

## Training and reproduction

The character and BERT artifacts use repository Train-base (1,736 prompts);
Dev-base (868 prompts) calibrates their original safe fallback. The word
artifact uses materialized Train (1,760 prompts) and public Train outcomes.
The exact-cost table uses materialized public Train+Dev costs. Full Dev is
used to calibrate tier blends/caps and validate risk. Runtime features and
lookup keys remain content-only.

The word artifact records LF-normalized Train input SHA-256
`029a0fb1f70432a05b837a1291d86d42278bb202d808a6a12911b0dae8628ac4`
and outcome SHA-256
`97a5a787086b3e1d9fa9c7945518543540e527ea248df4a4760de581b612a4ba`.
The character/BERT metadata also uses LF-normalized source hashes. The BERT
seed is `20260826`.

Training-only dependencies are pinned in
`baselines/requirements-hybrid-train.txt`: NumPy 2.2.6, SciPy 1.16.0,
scikit-learn 1.7.0, and PyTorch 2.7.1. That file records the official archive
URL, SHA-256, purpose, and BSD-3-Clause license for each dependency. The BERT
artifact metadata records the exact training build as PyTorch `2.7.1+cu118`;
training was deterministic and CPU-only. NumPy/SciPy/scikit-learn fit TF-IDF
and ridge heads; PyTorch trains the small Transformer. None is required by
the final runtime, which uses the Python standard library only.

After materializing the pinned public sources, reproduce the new artifacts:

```console
PYTHONPATH=src python3 -B baselines/train_char_tfidf.py \
  --input data/train/inputs-base.json \
  --outcomes data/train/outcomes.json \
  --artifact src/ossp_router/resources/char-tfidf-ridge.v1.json \
  --report build/bert-router/char-training.json --max-characters 4096

PYTHONPATH=src:baselines python3 -B baselines/train_bert_hybrid.py \
  --train-input data/train/inputs-base.json \
  --train-outcomes data/train/outcomes.json \
  --dev-input data/dev/inputs-base.json \
  --dev-outcomes data/dev/outcomes.json \
  --base-artifact src/ossp_router/resources/hash-regex-public.v1.json \
  --model-artifact src/ossp_router/resources/tiny-bert-residual.v1.json \
  --prediction-cache build/bert-router/dev-predictions.npz \
  --report build/bert-router/bert-training.json \
  --epochs 80 --patience 12 --seeds 20260826 --no-refit \
  --skip-evaluation

PYTHONPATH=src python3 -B baselines/train_word_tfidf.py \
  --train-input data/materialized/train/inputs.json \
  --train-outcomes data/train/outcomes.json \
  --artifact src/ossp_router/resources/word-tfidf-ridge.v1.json \
  --report build/bert-router/word-training.json --max-features 120000

PYTHONPATH=src python3 -B baselines/build_public_cost_lookup.py
```

The final public-hit blend was calibrated with NumPy RNG seed `20260831` on
full Dev. Each tier evaluated 10,000 random candidates, retained 1,000, then
mutated the best 24 candidates 250 times each. Hash/character/word weights
were projected to the probability simplex; word ridge alpha was selected
from `1/3/30`, character score offset from `0/3`, BERT weights from
`[0.005, 1.4]`, and model-specific margins from `[-0.45, 0.45]` (AX31) or
`[-0.65, 0.65]` (K1). Refinement used Gaussian standard deviations `0.08`
for weights/BERT and `0.055` for margins. Candidates were ranked by score and
then lower cost, and the top 500 were screened to require non-regressing word
and BERT-hybrid ablations. Frozen coefficients and caps are recorded in
`PUBLIC_COST_TIER_CONFIGURATIONS`; the original search report SHA-256 is
`40886b146a504ee2d57ec69232abe4b6b6106296a73b7d50b451b650b8b5b6cc`.
The final Balanced blend is a `t=0.904` interpolation from the previous
configuration toward one of that search's top-20 full-Dev candidates. The
improved Dev choice plateau spans `t=0.90200..0.90434`, and the exact selected
choice set is stable over `t=0.90397..0.90434`; it is not a single
floating-point boundary. Its validation report is
`build/record-policy-search/candidate-validation.json` (SHA-256
`c07a79e7b13c0161b7d32c8e705af50d32076bfc26259e0e09c403cdc0ddf680`),
and the fine scan report SHA-256 is
`5804c04eaf4e07bdd9f7591af471ff40de53e27af94f61a18e771d14c82c2972`.
This interpolation was selected after inspecting public Dev and its 868-row
subset, so it is public calibration evidence rather than a private-split
generalization claim.
The report command was:

```console
PYTHONPATH=src:baselines python3 -B build/agent-word/cost_lookup_search.py \
  --random-candidates 10000 --refine-per-seed 250 \
  --bootstrap-repetitions 1000 --seed 20260831
```

The all-miss score blends are frozen separately in
`CONSERVATIVE_SCORE_CONFIGURATIONS` and can use all five exported word ridge
alphas. They retain the pre-existing learned-cost configurations and guards;
they are not calibrated from the exact public-cost rows at runtime.

`build/` is experiment evidence rather than a packaged runtime input. The
checked-in validator below independently recomputes the confirmed public-hit
decisions, official Decimal score, rerouted bootstrap, and content-group
results from the packaged artifacts. Its lookup-disabled mode forces a valid
nonmatching cost table, observes the genuine runtime predictor, verifies
matrix-selector and reverse-order parity, and performs vectorized bootstrap
rerouting with scalar-selector parity audits and `math.fsum` budget-edge
checks.

The BERT trainer may evaluate multiple seeds without exporting a runtime
artifact. When `--model-artifact` is supplied it requires exactly one seed,
matching the standard-library runtime parser and the reproduction command
above.

Validate the final policy:

```console
PYTHONPATH=src python3 -B baselines/validate_bert_router.py \
  --input data/materialized/dev/inputs.json \
  --outcomes data/dev/outcomes.json \
  --bootstrap-repetitions 5000 --seed 20260831 \
  --skip-ablation \
  --report build/bert-router-final/public-risk-5000-record-winning.json

PYTHONPATH=src python3 -B baselines/validate_bert_router.py \
  --all-miss \
  --input data/materialized/dev/inputs.json \
  --outcomes data/dev/outcomes.json \
  --bootstrap-repetitions 5000 --seed 20260908 \
  --skip-ablation \
  --report build/bert-router-final/all-miss-risk-5000.json

python3 -B baselines/validate_content_logo.py \
  --random-candidates 100 --refine-seeds 2 \
  --refine-per-seed 10 --exact-screen 32 \
  --report build/bert-router-final/dev-family-logo-policy.json
```

## Validation status

Public-hit and forced-all-miss results are reported separately. Both use
public prompts and offline public outcomes, so the all-miss run checks the
fallback control flow and observed public-distribution cost risk; it is not a
claim about private-split generalization.

### Confirmed public-hit results

Official Decimal scoring on all 880 materialized Dev prompts:

| Tier | Quality | Cost ratio | Light / AX31 / K1 |
| --- | ---: | ---: | ---: |
| Fast | 0.682670454545 | 1.199376956326 | 424 / 456 / 0 |
| Balanced | 0.714772727273 | 1.843463991877 | 177 / 627 / 76 |
| Premium | 0.757670454545 | 3.589752398761 | 115 / 639 / 126 |

Weighted score is `0.714801136364`. On the same 868-row population used by
the strongest recorded research policy, the final router scores
`0.714228110599` versus `0.713623271889`, a gain of `0.000604838710`.
That earlier policy had no exported or runtime-benchmarked artifact. The
880-row result is reported separately because it includes 12 additionally
materialized AIME prompts. On that full population the final router exceeds
the released hash-regex baseline `0.695369318182` by `0.019431818182`.

Official Decimal scoring on all 1,760 Train prompts is `0.738906250000`:

| Tier | Quality | Cost ratio | Light / AX31 / K1 |
| --- | ---: | ---: | ---: |
| Fast | 0.696590909091 | 1.199692382516 | 817 / 943 / 0 |
| Balanced | 0.730113636364 | 1.845635538587 | 343 / 1287 / 130 |
| Premium | 0.804119318182 | 3.592935536518 | 219 / 1285 / 256 |

In 5,000 deterministic rerouted Dev bootstrap batches, target/official-limit
exceedances were zero for every tier. Cost q95/q99/max values were:

| Tier | q95 | q99 | max |
| --- | ---: | ---: | ---: |
| Fast | 1.199911983 | 1.199984235 | 1.199999552 |
| Balanced | 1.849614527 | 1.849918714 | 1.849999782 |
| Premium | 3.598771329 | 3.599753613 | 3.599994651 |

Worst rerouted major content-group costs were Korean-MCQ `1.199377416`,
long-8k+ `1.845275946`, and math-reasoning `3.568035827`, respectively.
The current exact-sum validation report is
`build/bert-router-final/public-risk-5000-record-winning.json` (SHA-256
`81c436941446f956ec3738f5cf19f0c0e70fff4fcb91382652fea3e2db130ab3`);
its vectorized bootstrap matched 81 scalar runtime batches per tier.

Re-running full Dev under another `PYTHONHASHSEED` produced byte-identical
files for all tiers. Reversing all 880 rows and replacing every ID/header
caused zero content-decision mismatches, and all outputs contained the exact
input ID set once.

### Lookup-disabled all-miss results

The validator replaced the packaged lookup with a valid one-row SHA-256 table
that matched none of the 880 Dev prompts, then called the genuine
`select_batch` fallback. Official Decimal results were:

| Tier | Quality | Cost ratio | Light / AX31 / K1 |
| --- | ---: | ---: | ---: |
| Fast | 0.669602272727 | 1.136063167305 | 517 / 363 / 0 |
| Balanced | 0.692613636364 | 1.487633268423 | 286 / 594 / 0 |
| Premium | 0.731534090909 | 2.933354347258 | 30 / 740 / 110 |

Weighted score is `0.695085227273`. On the separately retained 868-row
Dev-base population, the same packaged runtime policy scores
`0.704089861751`. The current-cap Dev-base report is
`build/bert-router-final/all-miss-base868-cap2.83.json` (SHA-256
`12b14bbd1561b7571ac5fa6838e945f34f07d6865b5405e81dbb6c692d8d5485`).

The same lookup-disabled runtime path scores `0.699914772727` on all 1,760
materialized Train prompts. This is a fitted-data diagnostic, not a
generalization estimate:

| Tier | Quality | Cost ratio | Light / AX31 / K1 |
| --- | ---: | ---: | ---: |
| Fast | 0.672727272727 | 1.101577175676 | 1,023 / 737 / 0 |
| Balanced | 0.703267045455 | 1.417733404341 | 582 / 1,178 / 0 |
| Premium | 0.732812500000 | 2.780449271518 | 45 / 1,547 / 168 |

The current-cap Train report is
`build/bert-router-final/all-miss-train1760-cap2.83.json` (SHA-256
`313d5522ecf7e74be77504a32c9563ae0f127e8f0497fc01f2d1023d019d9700`).

Returning to the 880-row Dev run, seed `20260908` produced zero official-limit
exceedances in 5,000 rerouted bootstrap batches. Actual cost q95/q99/max
values were:

| Tier | q95 | q99 | max | Official exceedances |
| --- | ---: | ---: | ---: | ---: |
| Fast | 1.181769773 | 1.193836907 | 1.213716146 | 0 / 5,000 |
| Balanced | 1.637129935 | 1.717345503 | 1.975268527 | 0 / 5,000 |
| Premium | 3.255016332 | 3.374887248 | 3.581779110 | 0 / 5,000 |

Under the risk validator's content-group definition v1, worst rerouted major
content-group costs were non-Korean MCQ `1.179639620`, math-reasoning
`1.562277131`, and short-other `3.629825524`, respectively.
For all three tiers, the choices reconstructed from captured prediction
matrices matched the runtime choices, and reversing the matrix rows and
mapping back produced the same per-content choices. Unit tests additionally
exercise ID/header/order mutation and byte determinism across hash seeds.
The vectorized bootstrap also matched the scalar runtime selector on 81
complete resampled batches per tier, spread across every processing chunk.
The saved validation report is
`build/bert-router-final/all-miss-risk-5000.json` (SHA-256
`421e7b654f716fa01a112dbe5ba1711046a5986955ff3616d99061150af2e838`).

### Content-family leave-one-group-out calibration audit

The Dev policy calibration audit partitions all 880 rows into seven mutually
exclusive, content-only families: code, Korean MCQ, logic/rules, long context,
math/reasoning, non-Korean MCQ, and other. For each outer fold, random search,
refinement, actual-cost feasibility, per-family cost safety, and the BERT
non-regression screen use only the other six families. The held-out family is
scored once after that fold policy is fixed.

The primary audit reconstructs the runtime's learned hash/character cost
heads and supplies those costs to the selector. Exact public costs are not
selector input; they are used on the six-family complement to reject policies
that miss aggregate or family safety targets. Stitched held-out results are:

| Tier | Quality | Cost ratio | Light / AX31 / K1 |
| --- | ---: | ---: | ---: |
| Fast | 0.644602272727 | 1.107772852823 | 707 / 173 / 0 |
| Balanced | 0.660511363636 | 1.463398356654 | 465 / 415 / 0 |
| Premium | 0.694886363636 | 2.538282625208 | 94 / 764 / 22 |

The stitched weighted score is `0.664460227273`, or `+0.045142045455` over
all-Light. All 21 held-out fold/tier combinations pass both the official
budgets and the stricter internal `1.18/1.70/3.40` targets. The highest
held-out costs are Fast non-Korean MCQ `1.152506264`, Balanced math/reasoning
`1.565577465`, and Premium long-context `2.856951839`. A separately labelled
exact-public-cost secondary audit scores `0.687698863636`; it is not the
primary fallback claim.

Canonical outcomes are parsed through the official protocol, aligned by
episode and model, and reconstructed with the official Decimal cost formula.
They match the cached score and cost matrices exactly (both maximum absolute
errors `0.0`). The current packaged all-miss choices also match all 2,640
captured row decisions. The report is
`build/bert-router-final/dev-family-logo-policy.json` (SHA-256
`18ac6cad392b1a2f748cfb074fe69d8712895438a4dba90163ec0b320112e53a`).

This audit isolates Dev policy-calibration leakage only. The frozen predictors
were trained on public Train and may contain examples from the held-out Dev
family, the seven broad families are heuristic proxies rather than hidden
source labels, and the method was developed after public Dev diagnostics.
It is therefore neither end-to-end unseen-family training nor a private-split
generalization claim.

The Dev input/outcome hashes reported by the validator are likewise computed
after CRLF-to-LF normalization; they are provenance hashes of canonical text,
not raw hashes of a Windows checkout.

On Windows 11 / Python 3.13.3, cold `python -S` runs over the combined 2,640
materialized Train+Dev prompts previously measured conservatively at:

| Tier | Wall time | Peak working set |
| --- | ---: | ---: |
| Fast | 67.115 s | 118.4 MiB |
| Balanced | 65.344 s | 122.3 MiB |
| Premium | 65.144 s | 116.6 MiB |

Those earlier timings predate the expanded ten-head artifact and are
historical regression evidence. A later pre-cache all-miss build measured the
following independent Windows 11 / Python 3.13.3 processes over the exact
2,640-row combined batch:

| Tier | Worker time | Wrapper time | Peak working set |
| --- | ---: | ---: | ---: |
| Fast | 44.037 s | 44.553 s | 134.3 MiB |
| Balanced | 52.766 s | 53.442 s | 134.5 MiB |
| Premium | 45.423 s | 45.950 s | 134.4 MiB |

Each run emitted all 2,640 decisions. These uncontrolled-host Windows
measurements predate the current 262,144-entry bounded hash cache and the
Premium `2.83` cap. Under later host contention, the current-cache Balanced
worker took `82.186937` seconds and emitted the same `871/1769/0` choices;
there is no current-source three-tier Windows timing table. These measurements
are reference evidence only, not native ARM64 runtime results.

The current wheel is 10,611,419 bytes with SHA-256
`394866405b7d48a4ca935b70e3dc38a9d124c8b421f6473ca24068ccfc760a75`.
Two builds with `SOURCE_DATE_EPOCH=315532800` were byte-identical.
All 26 packaged Python/JSON source and resource files match the working tree,
the metadata declares Python `>=3.9`, and an isolated `router-run` install
completed the toy batch for all three tiers with byte-identical source
entrypoint output. The saved wheel report is
`build/bert-router-final/wheel-validation-report-record-safe.json` (SHA-256
`4ba8f4b59a7a85ad91858fc49f95b386e02ffdf56906767bd13998fdd8cab676`).

The current working-tree audit image was built for `linux/arm64` with local
OCI index digest
`sha256:7aa070330fe45c17a44eb2623fbd03368c83bdcda56635e7e6e0e41ec2de81a8`,
ARM manifest `sha256:7af3b8de1f058c86ef76cb17a27b6a80fdf89a13875a13b063c03c14d87f6265`,
unpacked Docker size 33,240,758 bytes, UID `65532:65532`, and no declared
volume. Its source-manifest label is
`81b6244ef524880ae17adeabef3ac1068f030c26b42ebdbe1e8a93dd6e20227d`.
All 25 admitted files—the entrypoint and 24 package Python/JSON files—have the
same SHA-256 as the working tree; the two excluded package files are the
operator-only `public_runtime.py` and `tiebreak_latency.py`. The manifest scope
has 33 entries and includes `.dockerignore`; `tools/check_runtime.py` rejects
a missing or stale label before execution. This binds the audit image to the
current files, but it is not a registry-published release digest or a
commit-bound final submission image.

The same image completed the three-row toy batch for Fast, Balanced, and
Premium with network disabled, a read-only root, 2 CPUs, 2 GiB memory with no
additional swap, 32 PIDs, a 256 MiB `/tmp`, all capabilities dropped, and
`no-new-privileges`. Each tier was run twice with byte-identical output. Each
output volume contained only one valid `submission.json` with all three IDs
exactly once. The image/toy evidence report is
`build/bert-router-final/arm64-toy-record-safe.json` (SHA-256
`dbed3e901b03e8b489bc5295fcad4903e942df9c5da01d351c4b2bb935fa389f`).

The x86_64 Docker host had to emulate ARM64. A full 2,640-row Fast run was
still computing after ten minutes and was stopped, so it failed the 90-second
gate in this emulated environment and is not evidence about native ARM64
speed. The current image therefore still needs native Linux/ARM64 full
Train+Dev timing.

A read-only, network-disabled native Linux/amd64 Python 3.11.15 container ran
the LF-normalized working-tree snapshot's complete test discovery: 334 tests
were discovered, 322 passed, and 12 Docker-in-Docker integration tests were
conditionally skipped.
This closes the Linux/POSIX unit-suite gap, but it does not substitute for the
native ARM64 runtime measurement above. Ruff 0.16.4 reports no findings, and
REUSE 6.2.0 reports all 134 files copyright/license compliant.

The manual `.github/workflows/native-arm64-runtime.yml` workflow is the
fail-closed path for closing that remaining measurement. It targets GitHub's
native `ubuntu-24.04-arm` runner, rejects a non-`aarch64` host or Docker daemon,
materializes the pinned 1,760/880 public inputs, rebuilds the image with the
current source-manifest label, and runs `tools/check_runtime.py` across all
three tiers three times. It pins the compute/isolation limits in the report,
requires byte-identical repeated output, and applies a conservative 1 GiB
Docker-size screen. It uploads the JSON report, immutable image inspection,
host evidence, and checksums under the exact commit SHA. The public checker
post-validates its host-bind output directory rather than reproducing the
official 4 MiB/64-inode output tmpfs, and it enforces but does not report
cgroup memory/PID peaks; a green run must therefore be described as native
full-batch compute/isolation evidence, not proof of every official boundary.
The workflow uses only `workflow_dispatch`, so adding it does not claim that
the gate has run.
