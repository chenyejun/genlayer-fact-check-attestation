# Fact-Check Attestation Registry

A reusable on-chain primitive for fact-checking: anyone submits a public
claim together with two or three source URLs, validator consensus checks
the claim against those sources, and the resulting attestation is stored
immutably so other contracts and readers can rely on it without
re-running the check.

## Why consensus is needed

A fact-check is only useful if the checker cannot be gamed. If a single
party decided the verdict, a submitter could pick friendly sources, quote
selectively, or run the check until the model gave the answer they
wanted. Here the verdict is produced by a leader and re-produced
independently by every validator, and it is only written to state when
they agree. The evidence is agreed per source, with a tolerance rule
built for how models actually extract quotes (see "How the equivalence
check works" below), and every attestation stores the quotes it rests
on, so anyone can audit the result after the fact instead of trusting
the submitter.

## What it does

- `submit_claim` — post a claim (up to 500 characters) with 2–3 source
  URLs and optional context. The same normalized claim text cannot be
  submitted twice; call `get_claim_id` first to find an existing claim.
- `verify_claim` — run validator consensus on a submitted claim and
  store the attestation: verdict (`SUPPORTED` / `REFUTED` /
  `INSUFFICIENT_EVIDENCE`), confidence band (`high` / `medium` / `low`),
  the verbatim quotes, and how many sources loaded.
- `challenge_attestation` — anyone can challenge a current attestation
  with new counter-evidence (2–3 source URLs). The claim is re-checked
  against the new sources; if the verdict differs, the new attestation
  supersedes the old one. A challenge that reaches the same verdict is
  rejected and the old record stands.
- Views: `get_claim`, `get_attestation`, `get_current_attestation`,
  `get_claim_id`, `list_claims`, `list_attestations`, `stats`.

Attestations are never deleted. A successful challenge marks the old
record `SUPERSEDED`, sets `superseded_by` to the new attestation, and
sets `supersedes` on the new one, so the full history stays readable.

## State design

| Field | Type | Purpose |
| --- | --- | --- |
| `claims` | `TreeMap[u256, Claim]` | Claims by id |
| `attestations` | `TreeMap[u256, Attestation]` | Attestations by id |
| `claim_id_by_key` | `TreeMap[str, u256]` | Normalized claim text → claim id (duplicate detection) |
| `claim_ids` / `attestation_ids` | `DynArray[u256]` | Insertion order for listing |
| `claim_counter` / `attestation_counter` | `u256` | Monotonic ids |

`Claim` stores the claim text, context, sources (newline-joined URLs),
submitter, submission time, and status (`SUBMITTED` / `ATTESTED`).
`Attestation` stores the claim id, verdict, confidence, the canonical
quote set (one `source | quote` line per quote, sorted), the sources
actually checked, status (`CURRENT` / `SUPERSEDED`), the
`supersedes` / `superseded_by` links, verifier, verification time, and
`sources_loaded` / `sources_failed` counts.

Two rules shape the design. First, a claim is verified once: after
verification the claim is `ATTESTED`, and later evidence must go through
`challenge_attestation`, which is the only path that creates a second
attestation for the same claim. Second, sources are stored newline-joined
rather than as a nested list, because stored records are flat
dataclasses; URLs are validated to contain no whitespace, so the join is
unambiguous and reversible.

## How the equivalence check works

Verification runs in two sequential consensus rounds over the same judge
code (`_judge`: fetch each source with `gl.nondet.web.get`, ask the
model for a verdict with `gl.nondet.exec_prompt` and
`response_format="json"`, canonicalize the quotes). The rounds are
sequential, not nested, because the VM and the linter forbid calling an
equivalence principle from inside a `run_nondet` block, and a nondet
call is only valid inside one of the two.

**Round 1 — the evidence.** `gl.vm.run_nondet_unsafe` re-runs the
judge on both sides and compares the quote sets tolerantly, per
source, with `_quotes_agree`: each quote is normalized (whitespace
collapsed, lowercased, surrounding punctuation stripped), and for
every source either side quotes from, the two sides must share at
least one normalized quote from that source. The stored quotes are
the leader's agreed set.

The rule is per source rather than a similarity score over the whole
set, for two reasons. First, the contract's own rule is that every
loaded source must back the verdict with a quote, so agreement has
to hold source by source: a pooled score such as Jaccard ≥ 0.5 can
pass while one source contributes no shared evidence at all (leader
{A1, A2, B1} against validator {A1, A2} scores 2/3, with nothing
shared for B). Second, it tolerates the variance models actually
show: a validator that lists an extra supporting sentence still
shares at least one quote per source, while a validator that quotes
a different passage from a source, or nothing from it, fails that
source and disagrees. What the rule does not forgive is quoting
different passages — normalization fixes casing, spacing, and where
the model cut the ends, not which passage it picked.

This replaced byte-identical agreement. The first version checked the
canonical quote set with `gl.eq_principle.strict_eq`, on the
reasoning that a quote is deterministic text copied from a page.
On StudioNet that never reached a majority: `verify_claim` failed
consensus three times in a row on the deployed contract
(`0x9543D6096e36c32FB46DC83127Ae3ce827dA207D`), each attempt ending
after three "Majority disagreement, rotating the leader" rounds with
UNDETERMINED, and no attestation was ever created. Validators
extract different spans and trimmings of the same passages, so a
strict byte-equality majority is not a bar honest validators can
meet. No `strict_eq` is used on any LLM output anywhere in the
contract now.

**Round 2 — the verdict.** `gl.vm.run_nondet_unsafe(leader_fn,
validator_fn)` re-runs the judge. The validator requires the verdict
and the confidence band to match by plain `==`, and it requires both
sides' fresh quotes to agree tolerantly with the round-1 agreed set
under the same `_quotes_agree` rule. The verdict is deliberately
never put through `strict_eq`: it is a judgement call, and a
validator that reaches the same verdict from the same quotes has
agreed. The extra quote comparison in round 2 is a coherence gate: a
stored attestation never pairs a verdict with quotes from a run that
extracted something else. If the model contradicts itself across
rounds, consensus fails and nothing is stored — a safe failure, retried
later. The gate is tolerant for the same reason round 1 is: a
byte-equality check here would re-impose the exact brittleness
round 1 removed, since each side's round-2 extraction is a fresh
model call.

**The per-source quote rule.** After canonicalization, every source
that loaded must contribute at least one quote. If any does not, the
verdict is recorded as `INSUFFICIENT_EVIDENCE` with confidence `low`,
whatever the model first said. A verdict is only as good as its
evidence.

**Leader failures.** If the leader fails, the validator re-runs the
judge and compares failure classes: `[EXPECTED]` and `[EXTERNAL]` must
match exactly, `[TRANSIENT]` only has to be transient on both sides,
and `[LLM_ERROR]` never agrees, so a bad model answer rotates the
leader instead of locking bad state in. Under `run_nondet_unsafe` a
validator-side error surfaces as a disagreement rather than a clean
transient error; that is a known limitation of the unsafe variant (see
"Limitations").

Storage is never read inside a nondet block: `verify_claim` and
`challenge_attestation` copy the claim to memory with
`gl.storage.copy_to_memory` first and hand the consensus rounds plain
in-memory values. The leader and validator functions never reference
`self`.

## Failure paths

| Prefix | Meaning | Examples | Caller should |
| --- | --- | --- | --- |
| `[EXPECTED]` | Bad input or invalid state | Duplicate claim, fewer than 2 or more than 3 sources, non-HTTPS URL, URL with credentials/port/IP host, verifying an already-attested claim, challenging a non-current attestation, challenge reaching the same verdict | Fix the input; no retry |
| `[EXTERNAL]` | The outside world is at fault, deterministically | Every source failed with a 4xx (page gone, moved, or access denied) | Replace the dead sources and resubmit; no point retrying the same URLs |
| `[TRANSIENT]` | The outside world failed, possibly temporarily | Every source failed with a 5xx, timeout, or connection error | Retry later; nothing was written |
| `[LLM_ERROR]` | The model answer was unusable | Non-JSON answer, verdict or confidence outside the enum | Retry; the leader rotates, so a persistent failure means the prompt or the pages are the problem |

A failed `verify_claim` or `challenge_attestation` writes nothing: the
claim stays `SUBMITTED` (or the attestation stays `CURRENT`) and the
call can be retried.

## Methods

| Method | Access | Arguments | Returns |
| --- | --- | --- | --- |
| `submit_claim` | write | `claim_text: str`, `sources: str` (comma-separated URLs), `context: str` (`""` for none) | `{claim_id, status}` |
| `verify_claim` | write | `claim_id: u256` | `{attestation_id, claim_id, verdict, confidence, status}` |
| `challenge_attestation` | write | `attestation_id: u256`, `sources: str`, `context: str` | `{attestation_id, claim_id, verdict, confidence, supersedes, status}` |
| `get_claim` | view | `claim_id: u256` | Claim dict |
| `get_attestation` | view | `attestation_id: u256` | Attestation dict (quotes as `[{source, quote}]`) |
| `get_current_attestation` | view | `claim_id: u256` | The `CURRENT` attestation for the claim |
| `get_claim_id` | view | `claim_text: str` | `{claim_id}` |
| `list_claims` | view | — | `{claims: [...]}` |
| `list_attestations` | view | — | `{attestations: [...]}` |
| `stats` | view | — | `{claims, attestations, current_attestations}` |

Notes on arguments: source URLs are passed as a single comma-separated
string, because the Studio/CLI call path only passes strings, numbers,
and booleans. URLs must be `https://`, with a dotted DNS hostname and
no credentials, port, or whitespace. Claims are capped at 500
characters, context at 500, each page read is truncated to 6,000
characters, each quote to 240, and at most 6 quotes are stored.

## Deploying on StudioNet

Prerequisites: a GenLayer Studio account and the Studio app at
https://studio.genlayer.com/contracts.

1. Open Studio, switch to the StudioNet network, and create a new
   contract from `contracts/FactCheckAttestation.py` (paste the file or
   upload it).
2. Deploy with **no constructor arguments** — `__init__` takes none.
   The first line of the file pins the runner:
   `# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }`.
   That is the hash the official project boilerplate pins
   (`genlayer-project-boilerplate`, `contracts/football_bets.py`), verified
   against the boilerplate on 2026-11-13. Keep the blank line after it:
   the runner parses the leading `#` lines as one JSON document, and a
   comment glued directly under the Depends line breaks deployment.
3. Note the deployed contract address. The full step-by-step checklist,
   including the exact calls to run after deploy and what each should
   return, is in `DEPLOY_NOTES.md`.

CLI alternative (same network, same file):

```bash
genlayer network set testnet-bradbury   # or the StudioNet equivalent for your CLI version
genlayer deploy --contract contracts/FactCheckAttestation.py
```

## Reading attestations from another contract

Use a typed interface for the methods you need. The interface is only
for type safety; at runtime it is the same as `gl.get_contract_at`.

```python
from genlayer import *


@gl.contract_interface
class FactCheckAttestationReader:
    @gl.public.view
    def get_current_attestation(self, claim_id: u256) -> dict: ...


# ... inside a consumer contract, with the registry address in storage:
registry = gl.get_contract_at(FactCheckAttestationReader, self.registry)
attestation = registry.get_current_attestation(claim_id)
# attestation["verdict"] is SUPPORTED / REFUTED /
# INSUFFICIENT_EVIDENCE; attestation["quotes"] is the
# [{source, quote}] evidence.
```

Two things to keep in mind when composing. First, cross-contract calls
are asynchronous: the child call is created when the parent transaction
reaches `on='finalized'` by default, so do not write a consumer that
expects the verdict back in the same synchronous step — design around
the message flow (for example, read the attestation in a view first,
then act on it in a separate call). Second,
`get_current_attestation` raises `[EXPECTED]` when the claim has no
current attestation yet; handle that in the consumer rather than
assuming every claim is attested. A claim whose current attestation is
`REFUTED` or `INSUFFICIENT_EVIDENCE` is a definitive answer, not an
error — only a missing attestation raises.

## Verifying after deploy

Run each write method once on Studio and check the result against
`DEPLOY_NOTES.md`. In short: submit a claim, read it back with
`get_claim`; submit the same text again and confirm it reverts with
`[EXPECTED]`; verify the claim and confirm `get_attestation` shows the
verdict, the quotes, and `sources_loaded` / `sources_failed`;
challenge with new sources and confirm the old attestation becomes
`SUPERSEDED` and `get_current_attestation` returns the new one.

## Tests

Direct tests (mocked web + LLM, no network) live in
`tests/direct/test_fact_check_attestation.py` and follow the official
`genlayer-test` pattern (`direct_vm` / `direct_deploy` fixtures). They
cover submission and duplicate rejection, validation errors, a
successful verification, validator agreement and disagreement (verdict
and quotes, via mock swapping between leader and replay — the replay
uses `run_validator(index=0)` for the round-1 evidence validator and
`run_validator()` for the round-2 verdict validator), the tolerance
rule (quotes differing only in casing, spacing, or trailing
punctuation agree; an extra quote still agrees; quotes from different
passages with no shared quote disagree), the
per-source quote rule, one-source-down and all-sources-down handling,
challenge supersede and same-verdict rejection, and all views.

```bash
./.venv/bin/python -m pytest tests/direct -q
./.venv/bin/genvm-lint check contracts/FactCheckAttestation.py
```

## Limitations

- **Quotes are not re-verified against page text.** The contract
  checks that quotes cite a loaded source and that validators found
  the same evidence from each source, but it cannot prove a quote is
  verbatim without re-reading the page deterministically. A model
  that invents the same plausible quote on every run would pass. The
  stored quotes are on chain, so a reader can always check them
  against the source.
- **Two rounds cost two model calls per side.** Round 1 and round 2
  each fetch the pages and prompt the model. The cost is the price of
  agreeing the evidence and the verdict in separate rounds.
- **`run_nondet_unsafe` error semantics.** A validator-side failure
  (for example, a source that is down for the validators but up for the
  leader) surfaces as a disagreement, not as a classified transient
  error. Retrying later usually resolves it. The safe `run_nondet`
  variant would classify these cases better, but the contract uses the
  unsafe variant deliberately, as specified for this primitive.
- **Sources are submitter-chosen.** URL validation only removes the
  cheaply detectable traps (non-HTTPS, credentials, ports, IP
  literals). Whether a source is authoritative is the submitter's
  choice, and the verdict is only as good as the sources it was checked
  against. Consumers should weigh the sources listed on the
  attestation, not just the verdict.
- **Runner version.** The contract pins the boilerplate's runner hash
  (`1jb45aa8...`, rc7-era APIs such as `gl.vm.run_nondet_unsafe` and
  `gl.get_contract_at`). The SDK's current docs describe a newer runner
  with renamed APIs (`run_nondet`, `gl.contract.get_at`, and others);
  moving to it requires updating the consensus calls, not just the hash.
