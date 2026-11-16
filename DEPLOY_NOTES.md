# Deploy notes — FactCheckAttestation

## Contract

- File: `contracts/FactCheckAttestation.py`
- Class: `FactCheckAttestation`
- Runner (line 1, pinned): `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6`
  — the hash the official project boilerplate pins
  (`genlayer-project-boilerplate`, `contracts/football_bets.py`), verified
  against the boilerplate on 2026-11-13. Keep the blank line after it.
- `genvm-lint check` passes (3 checks) and validation passes: 10 methods
  (7 view, 3 write). Note: the linter reports a newer runner is available
  (`1zr6nqk...`); the contract targets the boilerplate's runner, and
  moving to a newer runner requires the renamed APIs, not just the hash —
  do not change the hash alone.
- Direct tests: 24 passed
  (`./.venv/bin/python -m pytest tests/direct -q`).

## Constructor

**No constructor arguments.** `__init__` takes none and only zeroes the
two counters. Deploy from Studio (https://studio.genlayer.com/contracts,
StudioNet network) or the CLI:

```bash
genlayer deploy --contract contracts/FactCheckAttestation.py
```

No value needs to be sent with any call. Consensus rounds (fetching
pages + model calls) run on the leader and validators; Studio shows the
fee estimate before you confirm — if a `verify_claim` or
`challenge_attestation` fee looks wrong, do not confirm; re-check the
sources first.

## Post-deploy checklist

Run these on Studio against the deployed address, in order. The claim
and sources below are real pages chosen so the expected verdict is
stable; if a page has changed since this was written, pick any two
accessible pages that plainly support the claim and adjust the expected
quotes accordingly.

Smoke-test claim: `The Eiffel Tower is located in Paris.`

Sources (comma-separated, one argument):

```text
https://en.wikipedia.org/wiki/Eiffel_Tower,https://www.britannica.com/place/Eiffel-Tower
```

### 1. `submit_claim` — happy path

Args:

- `claim_text`: `The Eiffel Tower is located in Paris.`
- `sources`: `https://en.wikipedia.org/wiki/Eiffel_Tower,https://www.britannica.com/place/Eiffel-Tower`
- `context`: `""`

Expected: returns `{claim_id: 1, status: "SUBMITTED"}`.
Then call `get_claim(1)` and confirm `claim_text`, `sources` (list of
the two URLs), `status: "SUBMITTED"`, and `submitter` is your address.

### 2. `submit_claim` — duplicate rejection

Same args as step 1 (try different casing, e.g. `THE EIFFEL TOWER IS
LOCATED IN PARIS.`).

Expected: reverts with `[EXPECTED]` (`claim already submitted as claim
1`). Nothing is written; `stats` still shows 1 claim.

### 3. `verify_claim` — happy path

Args: `claim_id: 1`.

Expected: returns `{attestation_id: 1, claim_id: 1, verdict:
"SUPPORTED", confidence: "high"|"medium", status: "CURRENT"}`. (The
confidence band depends on the model's reading of the pages; either
`high` or `medium` is a pass — what matters is the verdict.)
Then call `get_attestation(1)` and confirm: `verdict: "SUPPORTED"`,
`quotes` has one entry per source (each citing the source URL it came
from), `sources_loaded: 2`, `sources_failed: 0`, `status: "CURRENT"`,
`supersedes: 0`, `superseded_by: 0`. Call `get_current_attestation(1)`
and confirm it returns attestation 1. Call `get_claim(1)` and confirm
`status: "ATTESTED"`.

If it reverts with `[TRANSIENT]`, a source was briefly unreachable —
wait and retry the same call. If it reverts with `[EXTERNAL]`, a source
URL is dead — replace it (submit a new claim; the old one cannot be
edited) and continue from step 1 with the new claim id.

### 4. `verify_claim` — already attested

Args: `claim_id: 1`.

Expected: reverts with `[EXPECTED]` (`claim 1 is already ATTESTED`).

### 5. `challenge_attestation` — what can (and cannot) supersede

Only an opposing substantive verdict replaces a current attestation:
`SUPPORTED` against `REFUTED`, or the other way round — or a
substantive verdict replacing an `INSUFFICIENT_EVIDENCE` record. A
challenge that reaches `INSUFFICIENT_EVIDENCE` is rejected: not being
able to tell is not counter-evidence, and unrelated sources must not
downgrade a valid current attestation.

(a) An `INSUFFICIENT_EVIDENCE` challenge is rejected. Challenge the
Paris `SUPPORTED` attestation with sources that do not address the
claim:

`challenge_attestation(1, "https://en.wikipedia.org/wiki/London_Eye,https://en.wikipedia.org/wiki/Big_Ben", "")`

Expected: reverts with `[EXPECTED]` (`challenge reached
INSUFFICIENT_EVIDENCE; attestation stands`). Nothing is written;
`get_attestation(1)` still shows `status: "CURRENT"` and
`superseded_by: 0`. If it instead reverts with `[TRANSIENT]` or
`[LLM_ERROR]`, retry — a source was briefly unreachable or the model
answer was malformed. (If it succeeds with a substantive verdict, the
model read the new sources as actually deciding the claim; re-check
the sources before trusting that result.)

(b) Upgrade from `INSUFFICIENT_EVIDENCE`. Submit a second claim with
sources that do not address it, so its first verification cannot reach
a verdict:

1. `submit_claim("The Eiffel Tower is 330 metres tall.", "https://en.wikipedia.org/wiki/London_Eye,https://en.wikipedia.org/wiki/Big_Ben", "")` → `{claim_id: 2, status: "SUBMITTED"}`.
2. `verify_claim(2)` → expect `{attestation_id: 2, claim_id: 2, verdict:
   "INSUFFICIENT_EVIDENCE", status: "CURRENT"}`. The model cannot quote
   the off-topic pages for or against the height, so the per-source
   quote rule records `INSUFFICIENT_EVIDENCE`. If it returns a
   substantive verdict instead, the model misread the pages — retry
   once, then pick different off-topic sources.
3. `challenge_attestation(2, "https://en.wikipedia.org/wiki/Eiffel_Tower,https://www.britannica.com/place/Eiffel-Tower", "")`
   → expect `{attestation_id: 3, claim_id: 2, verdict: "SUPPORTED",
   supersedes: 2, status: "CURRENT"}`.
4. `get_attestation(2)` → expect `status: "SUPERSEDED"` and
   `superseded_by: 3`. `get_attestation(3)` → expect `status:
   "CURRENT"` and `supersedes: 2`. `get_current_attestation(2)` →
   expect attestation 3. The old record is still readable — history is
   never deleted.

A `SUPPORTED`↔`REFUTED` flip on live pages is not demonstrated here: a
truthful flip needs a claim whose evidence genuinely changed between
source sets, and no stable real-page example was found. The flip path
is covered by the direct tests instead
(`test_challenge_supersedes_attestation` and
`test_challenge_refuted_to_supported_flip` in
`tests/direct/test_fact_check_attestation.py`).

### 6. `challenge_attestation` — same verdict rejection

Args: `attestation_id: 1`, the same two sources as step 1, `context:
""`.

Expected: reverts with `[EXPECTED]` (`challenge reached the same
verdict SUPPORTED; attestation stands`). Attestation 1 stays `CURRENT`.

### 7. Views (read-only, no fee)

- `get_claim(1)` → claim 1, `status: "ATTESTED"`.
- `get_attestation(1)` → attestation 1, `status: "CURRENT"`.
- `get_current_attestation(1)` → attestation 1.
- `get_claim_id("The Eiffel Tower is located in Paris.")` → `{claim_id: 1}`.
- `list_claims()` → both claims (after step 5).
- `list_attestations()` → all attestations, including any `SUPERSEDED`
  ones (history is never deleted).
- `stats()` → `{claims: 2, attestations: 3, current_attestations: 2}`
  (attestation 2 is `SUPERSEDED`; numbers grow if you ran extra
  challenges).

## If something looks wrong

- Deploy rejected with `invalid_contract`: the Depends line is the
  usual suspect — confirm line 1 is exactly the pinned hash and line 2
  is blank.
- `verify_claim` reverts `[LLM_ERROR]`: the model returned something
  outside the expected shape. Retry once; if it persists, the pages may
  be too long or too ambiguous for the prompt — try shorter, clearer
  sources.
- `verify_claim` hangs or times out on Studio: the consensus round is
  waiting on validators. Wait for the Studio timeout message; nothing
  was written. Retry.
- A verdict you disagree with: do not edit state — there is no edit
  path by design. Submit a challenge with better sources (step 5) and
  let consensus decide.
