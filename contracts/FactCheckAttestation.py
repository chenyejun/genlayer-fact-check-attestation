# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

"""FactCheckAttestation — a reusable registry of fact-check attestations.

Anyone submits a public claim together with two or three source URLs.
Validator consensus checks the claim against those sources and stores the
attestation — verdict, confidence band, and the verbatim quotes it rests
on — so other contracts and readers can rely on it without re-running
the check.

- A claim is submitted once. The same normalized claim text cannot be
  submitted twice; new evidence against an existing attestation goes
  through challenge_attestation. Only an opposing substantive verdict
  (SUPPORTED against REFUTED, or the other way round) supersedes the
  old record — or a substantive verdict that replaces an
  INSUFFICIENT_EVIDENCE record. A challenge that reaches
  INSUFFICIENT_EVIDENCE never supersedes: not being able to tell is
  not counter-evidence, and unrelated sources must not be able to
  downgrade a valid current attestation. Attestations are never
  deleted: a successful challenge marks the old record SUPERSEDED and
  links it to the new one.
- Verification runs in two sequential consensus rounds (see
  _verify_with_consensus): the evidence is agreed first, the verdict
  second. The evidence round compares quotes tolerantly, per source:
  quotes are normalized (whitespace, casing, surrounding punctuation)
  and the two sides must share at least one normalized quote from
  every source either side quotes from. Byte-identical agreement was
  tried first and failed on StudioNet: three verify_claim attempts in
  a row ended in "Majority disagreement, rotating the leader" because
  validators extract different spans and trimmings of the same
  passages. The verdict is a judgement call and is compared with plain
  equality instead.
- A source that fails to load is recorded, not hidden. If every source
  fails, verification fails with a classified error ([EXTERNAL] for 4xx,
  [TRANSIENT] otherwise). If at least one loads, the verdict is reached
  from the sources that loaded, and the attestation records how many
  failed. Every source that loads must contribute at least one quote, or
  the verdict is recorded as INSUFFICIENT_EVIDENCE.

The runner version is pinned: GenLayer networks reject py-genlayer:test,
py-genlayer:latest and unversioned aliases. The hash above is the one the
official project boilerplate pins (genlayer-project-boilerplate,
contracts/football_bets.py) and the one docs.genlayer.com publishes.
"""

import json
from dataclasses import dataclass

from genlayer import *


# Error prefixes. Failures are raised as gl.vm.UserError with one of these
# so the leader and the validators can tell deterministic failures apart
# from transient ones: [EXPECTED] and [EXTERNAL] must match exactly,
# [TRANSIENT] only has to be transient on both sides, and [LLM_ERROR]
# never agrees, so a bad model answer rotates the leader instead of
# locking bad state in.
ERR_EXPECTED = "[EXPECTED]"
ERR_EXTERNAL = "[EXTERNAL]"
ERR_TRANSIENT = "[TRANSIENT]"
ERR_LLM = "[LLM_ERROR]"


VERDICT_SUPPORTED = "SUPPORTED"
VERDICT_REFUTED = "REFUTED"
VERDICT_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
VERDICTS = (VERDICT_SUPPORTED, VERDICT_REFUTED, VERDICT_INSUFFICIENT)

STATUS_SUBMITTED = "SUBMITTED"
STATUS_ATTESTED = "ATTESTED"
STATUS_CURRENT = "CURRENT"
STATUS_SUPERSEDED = "SUPERSEDED"


# Bounds. A contract that reads the open web needs limits that do not
# depend on the pages behaving: every transaction has a compute budget,
# and the leader's return value is written to the chain.
MAX_CLAIM_CHARS = 500
MAX_CONTEXT_CHARS = 500
MAX_URL_CHARS = 500
MIN_SOURCES = 2
MAX_SOURCES = 3
MAX_PAGE_CHARS = 6000
MAX_QUOTE_CHARS = 240
MAX_QUOTES = 6


@allow_storage
@dataclass
class Claim:
    claim_id: u256
    claim_text: str
    context: str
    # Source URLs, joined with "\n". URLs are validated to contain no
    # whitespace, so the join is unambiguous and reversible.
    sources: str
    submitter: Address
    submitted_at: str
    status: str


@allow_storage
@dataclass
class Attestation:
    attestation_id: u256
    claim_id: u256
    verdict: str
    confidence: str
    # Canonical quote set: one "source | quote" line per quote, sorted.
    # This is the exact set the validators agreed on, so a reader can
    # re-check it without trusting the submitter.
    quotes: str
    # Sources actually checked, joined with "\n" (same encoding as Claim).
    sources: str
    status: str
    # 0 when this is the first attestation for the claim; otherwise the
    # attestation this one supersedes.
    supersedes: u256
    # 0 while current; otherwise the attestation that superseded this one.
    superseded_by: u256
    verified_by: Address
    verified_at: str
    sources_loaded: u256
    sources_failed: u256


def _fail(prefix: str, detail: str) -> None:
    raise gl.vm.UserError(f"{prefix} {detail}")


def _clean_text(value: str, field: str, max_chars: int) -> str:
    text = str(value).strip()
    if len(text) == 0:
        _fail(ERR_EXPECTED, field + " must not be empty")
    if len(text) > max_chars:
        _fail(ERR_EXPECTED, field + " must be " + str(max_chars) + " characters or fewer")
    return text


def _normalize_claim(text: str) -> str:
    """Duplicate-detection key for a claim: trimmed, lowercased, with
    every run of whitespace collapsed to a single space."""
    return " ".join(str(text).strip().lower().split())


def _clean_url(url: str) -> str:
    """Validate one source URL. HTTPS only, no credentials, no explicit
    port, no whitespace, and a dotted DNS hostname (never an IP literal).
    URL syntax alone never makes a source trustworthy — that is the
    submitter's choice, and the verdict is only as good as the sources
    it was checked against."""
    text = str(url).strip()
    if len(text) == 0:
        _fail(ERR_EXPECTED, "source URL must not be empty")
    if len(text) > MAX_URL_CHARS:
        _fail(ERR_EXPECTED, "source URL must be " + str(MAX_URL_CHARS) + " characters or fewer")
    if not text.startswith("https://"):
        _fail(ERR_EXPECTED, "source URL must start with https://: " + text)
    if text != "".join(text.split()):
        _fail(ERR_EXPECTED, "source URL must not contain whitespace: " + text)
    rest = text[len("https://"):]
    authority_end = len(rest)
    for separator in ("/", "?", "#"):
        found = rest.find(separator)
        if found >= 0 and found < authority_end:
            authority_end = found
    authority = rest[:authority_end]
    if len(authority) == 0:
        _fail(ERR_EXPECTED, "source URL has no host: " + text)
    if "@" in authority:
        _fail(ERR_EXPECTED, "source URL must not contain credentials: " + text)
    if ":" in authority:
        _fail(ERR_EXPECTED, "source URL must not contain a port: " + text)
    host = authority.lower()
    if "." not in host:
        _fail(ERR_EXPECTED, "source URL host must be a dotted domain: " + text)
    return text


def _parse_sources(sources: str) -> list:
    """Split the comma-separated sources argument. The CLI passes strings,
    numbers and booleans only — a list has to go through a deploy script —
    so the public shape is one string and the splitting happens here."""
    parsed = [part.strip() for part in str(sources).split(",")]
    parsed = [part for part in parsed if part != ""]
    if len(parsed) < MIN_SOURCES:
        _fail(ERR_EXPECTED, "at least " + str(MIN_SOURCES) + " source URLs are required")
    if len(parsed) > MAX_SOURCES:
        _fail(ERR_EXPECTED, "at most " + str(MAX_SOURCES) + " source URLs, got " + str(len(parsed)))
    cleaned = [_clean_url(url) for url in parsed]
    if len(set(cleaned)) != len(cleaned):
        _fail(ERR_EXPECTED, "source URLs must be distinct")
    return cleaned


def _clean_quote(text: str) -> str:
    """Normalize one extracted quote: collapse every whitespace run to a
    single space and cap the length. The result is always single-line, so
    the canonical quote set can join lines with "\\n" unambiguously."""
    return " ".join(str(text).split())[:MAX_QUOTE_CHARS].strip()


def _canon_quotes(raw_quotes, loaded: list) -> str:
    """Canonical form of the extracted quotes: one "source | quote" line
    per quote, deduplicated and sorted. Two validators that extracted the
    same evidence agree byte-for-byte regardless of the order their
    models listed the quotes in. Quotes citing a URL that was not one of
    the loaded sources are dropped — that is either a hallucination or an
    injected URL, and neither may be stored."""
    lines = []
    seen = []
    entries = raw_quotes if isinstance(raw_quotes, list) else []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        source = str(entry.get("source", "")).strip()
        quote = _clean_quote(entry.get("quote", ""))
        if source == "" or quote == "":
            continue
        if source not in loaded:
            continue
        line = source + " | " + quote
        if line in seen:
            continue
        seen.append(line)
        lines.append(line)
    lines.sort()
    return "\n".join(lines[:MAX_QUOTES])


def _quotes_from_canon(canon: str) -> list:
    """Split a canonical quote set back into [{"source", "quote"}] dicts
    for readers. A source URL never contains " | " (URLs have no spaces),
    so splitting on the first occurrence is unambiguous."""
    result = []
    if canon == "":
        return result
    for line in canon.split("\n"):
        parts = line.split(" | ", 1)
        if len(parts) == 2:
            result.append({"source": parts[0], "quote": parts[1]})
    return result


def _quoted_sources(canon: str) -> list:
    return [entry["source"] for entry in _quotes_from_canon(canon)]


# Punctuation stripped from the ends of a quote when two extractions are
# compared. A quote is copied text, so a leading or trailing comma,
# period, or quote mark is a boundary choice by the model, not different
# evidence.
_COMPARE_STRIP_CHARS = " \t\n.,;:!?\"'()[]{}«»“”‘’—–-…"


def _normalize_quote_for_compare(quote: str) -> str:
    """Normalize one quote for agreement comparison: collapse whitespace,
    lowercase, strip surrounding punctuation. Two extractions of the same
    passage that differ only in casing, spacing, or where the model cut
    the ends agree after this; extractions of different passages do not."""
    return " ".join(str(quote).split()).lower().strip(_COMPARE_STRIP_CHARS)


def _quotes_agree(leader_canon: str, validator_canon: str) -> bool:
    """Tolerant agreement on two canonical quote sets, per source.

    For every source either side quotes from, the two sides must share at
    least one normalized quote from that source. This is deliberately
    per source rather than a similarity score over the whole set: the
    contract's rule is that every loaded source must back the verdict
    with a quote, so agreement has to hold source by source — a score
    over the pooled set could pass while one source contributes no
    shared evidence at all. It is also tolerant where it should be: a
    side that lists an extra supporting sentence still shares at least
    one quote per source, while a side that quotes a different passage
    from a source, or nothing from it, fails that source and disagrees.
    Two empty sets agree, matching the old byte-equality behaviour for
    the no-quotes case."""
    leader_by_source = {}
    validator_by_source = {}
    for canon, by_source in ((leader_canon, leader_by_source), (validator_canon, validator_by_source)):
        for entry in _quotes_from_canon(canon):
            normalized = _normalize_quote_for_compare(entry["quote"])
            if normalized == "":
                continue
            source = entry["source"]
            if source not in by_source:
                by_source[source] = set()
            by_source[source].add(normalized)
    sources = set(leader_by_source) | set(validator_by_source)
    for source in sources:
        leader_quotes = leader_by_source.get(source, set())
        validator_quotes = validator_by_source.get(source, set())
        if len(leader_quotes & validator_quotes) == 0:
            return False
    return True


def _one_of(value, allowed: tuple, field: str) -> str:
    """Coerce a model field to one of the allowed values, or fail as an
    LLM error. A model that cannot stay inside the enum is not a model
    whose verdict can be stored."""
    text = str(value).strip().upper() if isinstance(value, str) else ""
    if text not in allowed:
        _fail(ERR_LLM, field + " must be one of " + "/".join(allowed) + ", got " + repr(value))
    return text


def _error_text(exc) -> str:
    """Read the message out of a UserError / Result, across the runner
    versions that name the payload `.data` or `.message`."""
    for attr in ("data", "message"):
        value = getattr(exc, attr, None)
        if value is not None:
            return str(value)
    return str(exc)


def _leader_error_is_agreeable(leaders_res, judge) -> bool:
    """Decide whether to agree with a leader that failed. Deterministic
    failures (a bad argument, a 4xx) must match exactly. A transient
    failure only has to be transient on both sides. An LLM failure always
    disagrees, so consensus rotates rather than locking bad state."""
    leader_msg = _error_text(leaders_res)
    try:
        judge()
    except gl.vm.UserError as exc:
        validator_msg = _error_text(exc)
        if validator_msg.startswith(ERR_EXPECTED) or validator_msg.startswith(ERR_EXTERNAL):
            return validator_msg == leader_msg
        if validator_msg.startswith(ERR_TRANSIENT) and leader_msg.startswith(ERR_TRANSIENT):
            return True
        return False
    except Exception:
        return False
    # The leader failed where this validator succeeded: disagree.
    return False


def _claim_to_dict(claim: Claim) -> dict:
    return {
        "claim_id": claim.claim_id,
        "claim_text": claim.claim_text,
        "context": claim.context,
        "sources": claim.sources.split("\n") if claim.sources != "" else [],
        "submitter": claim.submitter.as_hex,
        "submitted_at": claim.submitted_at,
        "status": claim.status,
        "found": True,
    }


def _attestation_to_dict(att: Attestation) -> dict:
    return {
        "attestation_id": att.attestation_id,
        "claim_id": att.claim_id,
        "verdict": att.verdict,
        "confidence": att.confidence,
        "quotes": _quotes_from_canon(att.quotes),
        "sources": att.sources.split("\n") if att.sources != "" else [],
        "status": att.status,
        "supersedes": att.supersedes,
        "superseded_by": att.superseded_by,
        "verified_by": att.verified_by.as_hex,
        "verified_at": att.verified_at,
        "sources_loaded": att.sources_loaded,
        "sources_failed": att.sources_failed,
        "found": True,
    }


# --------------------------------------------------------------------------
# Consensus worker (module level: it takes plain values, touches no
# storage, and is shared by both consensus rounds, so the leader and the
# validators always run the same code).
# --------------------------------------------------------------------------
def _judge(claim_text: str, context: str, sources: list) -> dict:
    """Fetch the sources, ask the model for a verdict, and return the full
    judgement: verdict, confidence band, canonical quote set, and how many
    sources loaded. Raises a prefixed gl.vm.UserError on failure."""
    loaded = []
    failed = u256(0)
    failed_client = u256(0)
    pages = []
    for url in sources:
        try:
            resp = gl.nondet.web.get(url)
        except Exception:
            failed = failed + u256(1)
            continue
        status = resp.status
        if status == 200:
            raw = resp.body
            if raw is None:
                text = ""
            elif isinstance(raw, bytes):
                text = raw.decode("utf-8", "ignore")
            else:
                text = str(raw)
            loaded.append(url)
            pages.append({"source": url, "text": text[:MAX_PAGE_CHARS]})
        elif 400 <= status < 500:
            failed = failed + u256(1)
            failed_client = failed_client + u256(1)
        else:
            failed = failed + u256(1)
    if len(loaded) == 0:
        if failed_client == failed:
            _fail(ERR_EXTERNAL, "all sources failed to load with client errors")
        _fail(ERR_TRANSIENT, "all sources failed to load")
    prompt = (
        "You are checking a public claim against the source pages below. "
        "Answer with JSON only, using exactly these keys:\n"
        '- "verdict": one of SUPPORTED, REFUTED, INSUFFICIENT_EVIDENCE\n'
        '- "confidence": one of high, medium, low\n'
        '- "quotes": a list of objects with keys "source" and "quote"\n\n'
        "Rules:\n"
        '- "source" must be one of the URLs listed below, copied exactly.\n'
        '- "quote" must be text copied verbatim from that page, at most 240 characters. '
        "Do not invent, paraphrase, or complete quotes.\n"
        "- Give at least one quote for every source page listed below.\n"
        "- SUPPORTED only when the quotes directly support the claim; REFUTED only when "
        "they directly contradict it; otherwise INSUFFICIENT_EVIDENCE.\n"
        '- "confidence" is high only when the quotes settle the claim on their own.'
    )
    prompt += "\n\nClaim: " + claim_text
    if context != "":
        prompt += "\nContext: " + context
    prompt += "\n\nSources:\n" + json.dumps(pages)
    try:
        answer = gl.nondet.exec_prompt(prompt, response_format="json")
    except Exception as exc:
        _fail(ERR_LLM, "model call failed: " + str(exc))
    if not isinstance(answer, dict):
        _fail(ERR_LLM, "model answer was not a JSON object")
    verdict = _one_of(answer.get("verdict", ""), VERDICTS, "verdict")
    confidence = _one_of(
        answer.get("confidence", ""), ("HIGH", "MEDIUM", "LOW"), "confidence"
    ).lower()
    canon = _canon_quotes(answer.get("quotes", []), loaded)
    quoted = _quoted_sources(canon)
    missing = [url for url in loaded if url not in quoted]
    if len(missing) > 0:
        # A verdict is only as good as its evidence: every source that
        # loaded must back it with a quote.
        verdict = VERDICT_INSUFFICIENT
        confidence = "low"
    return {
        "verdict": verdict,
        "confidence": confidence,
        "quotes": canon,
        "sources_loaded": u256(len(loaded)),
        "sources_failed": failed,
    }


class FactCheckAttestation(gl.Contract):
    """Registry of fact-check attestations.

    Claims are submitted once, verified once by validator consensus, and
    then challenged only with new counter-evidence. A challenge only
    supersedes the old record when it reaches the opposing substantive
    verdict, or a substantive verdict replacing an INSUFFICIENT_EVIDENCE
    record; a challenge that reaches INSUFFICIENT_EVIDENCE is rejected
    and the old record stands. Attestations are never deleted: a
    successful challenge supersedes the old record and links the two, so
    the full history stays readable on chain.
    """

    claims: TreeMap[u256, Claim]
    attestations: TreeMap[u256, Attestation]
    claim_id_by_key: TreeMap[str, u256]
    claim_ids: DynArray[u256]
    attestation_ids: DynArray[u256]
    claim_counter: u256
    attestation_counter: u256

    def __init__(self):
        self.claim_counter = u256(0)
        self.attestation_counter = u256(0)

    # -- consensus ------------------------------------------------------
    # Two sequential rounds, because the two mechanisms cannot nest: a
    # nondet call is only valid inside one of the two. Each round runs
    # the same _judge code, with the enforcement that fits what it is
    # agreeing on:
    # - Round 1 agrees the EVIDENCE with gl.vm.run_nondet_unsafe.
    #   The leader and each validator re-run _judge and their quote
    #   sets must agree tolerantly, per source, under _quotes_agree:
    #   normalized quotes, at least one shared quote from every source
    #   either side quotes from. Byte-identical agreement was tried
    #   first (gl.eq_principle.strict_eq on the canonical quote set)
    #   and failed on StudioNet three times in a row — validators
    #   extract different spans and trimmings of the same passages, so
    #   a strict majority was never reached. No strict_eq is used on
    #   any LLM output anywhere in this contract.
    # - Round 2 agrees the VERDICT with gl.vm.run_nondet_unsafe. The
    #   leader and each validator re-run _judge and the verdict (and
    #   the confidence band) must match by plain equality — the verdict
    #   is a judgement call, so it is never put through strict_eq. The
    #   validator also requires both sides' fresh quotes to agree
    #   tolerantly with the round-1 agreed set, so a stored attestation
    #   never pairs a verdict with quotes from a run that extracted
    #   something else; if the model contradicts itself across rounds,
    #   consensus fails and nothing is stored. The gate is tolerant for
    #   the same reason round 1 is: a byte-equality check here would
    #   re-impose the exact brittleness round 1 removed.
    def _verify_with_consensus(self, claim_text: str, context: str, sources: list) -> dict:
        def quote_leader_fn() -> str:
            return _judge(claim_text, context, sources)["quotes"]

        def quote_validator_fn(leaders_res) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return _leader_error_is_agreeable(
                    leaders_res, lambda: _judge(claim_text, context, sources)["quotes"]
                )
            try:
                mine = _judge(claim_text, context, sources)["quotes"]
            except Exception:
                return False
            return _quotes_agree(leaders_res.calldata, mine)

        agreed_quotes = gl.vm.run_nondet_unsafe(quote_leader_fn, quote_validator_fn)

        def verdict_leader_fn() -> dict:
            return _judge(claim_text, context, sources)

        def verdict_validator_fn(leaders_res) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return _leader_error_is_agreeable(
                    leaders_res, lambda: _judge(claim_text, context, sources)
                )
            try:
                mine = _judge(claim_text, context, sources)
            except Exception:
                return False
            leader = leaders_res.calldata
            if mine["verdict"] != leader["verdict"]:
                return False
            if mine["confidence"] != leader["confidence"]:
                return False
            if not _quotes_agree(mine["quotes"], agreed_quotes):
                return False
            if not _quotes_agree(leader["quotes"], agreed_quotes):
                return False
            return True

        result = gl.vm.run_nondet_unsafe(verdict_leader_fn, verdict_validator_fn)
        # The stored quotes are always the leader's round-1 set, agreed
        # tolerantly, never one side's fresh extraction.
        result["quotes"] = agreed_quotes
        return result

    def _store_attestation(self, claim_id: u256, result: dict, sources: list, supersedes: u256) -> u256:
        self.attestation_counter = self.attestation_counter + u256(1)
        attestation_id = self.attestation_counter
        self.attestations[attestation_id] = Attestation(
            attestation_id=attestation_id,
            claim_id=claim_id,
            verdict=result["verdict"],
            confidence=result["confidence"],
            quotes=result["quotes"],
            sources="\n".join(sources),
            status=STATUS_CURRENT,
            supersedes=supersedes,
            superseded_by=u256(0),
            verified_by=gl.message.sender_address,
            verified_at=gl.message_raw["datetime"],
            sources_loaded=result["sources_loaded"],
            sources_failed=result["sources_failed"],
        )
        self.attestation_ids.append(attestation_id)
        return attestation_id

    # -- public writes --------------------------------------------------
    @gl.public.write
    def submit_claim(self, claim_text: str, sources: str, context: str) -> dict:
        """Submit a claim with 2-3 source URLs (comma-separated) and
        optional context (pass "" for none). Rejects a claim whose
        normalized text was already submitted."""
        text = _clean_text(claim_text, "claim", MAX_CLAIM_CHARS)
        key = _normalize_claim(text)
        if key in self.claim_id_by_key:
            _fail(
                ERR_EXPECTED,
                "claim already submitted as claim " + str(self.claim_id_by_key[key]),
            )
        parsed = _parse_sources(sources)
        clean_context = ""
        if context != "":
            clean_context = _clean_text(context, "context", MAX_CONTEXT_CHARS)
        self.claim_counter = self.claim_counter + u256(1)
        claim_id = self.claim_counter
        self.claims[claim_id] = Claim(
            claim_id=claim_id,
            claim_text=text,
            context=clean_context,
            sources="\n".join(parsed),
            submitter=gl.message.sender_address,
            submitted_at=gl.message_raw["datetime"],
            status=STATUS_SUBMITTED,
        )
        self.claim_id_by_key[key] = claim_id
        self.claim_ids.append(claim_id)
        return {"claim_id": claim_id, "status": STATUS_SUBMITTED}

    @gl.public.write
    def verify_claim(self, claim_id: u256) -> dict:
        """Run validator consensus on a submitted claim and store the
        attestation. One claim is verified once; later evidence goes
        through challenge_attestation."""
        if claim_id not in self.claims:
            _fail(ERR_EXPECTED, "unknown claim " + str(claim_id))
        claim = self.claims[claim_id]
        if claim.status != STATUS_SUBMITTED:
            _fail(ERR_EXPECTED, "claim " + str(claim_id) + " is already " + claim.status)
        # Storage cannot be read inside a nondet block: copy first, then
        # hand the consensus round plain in-memory values.
        claim_mem = gl.storage.copy_to_memory(claim)
        sources = claim_mem.sources.split("\n")
        result = self._verify_with_consensus(claim_mem.claim_text, claim_mem.context, sources)
        attestation_id = self._store_attestation(claim_id, result, sources, u256(0))
        claim.status = STATUS_ATTESTED
        self.claims[claim_id] = claim
        return {
            "attestation_id": attestation_id,
            "claim_id": claim_id,
            "verdict": result["verdict"],
            "confidence": result["confidence"],
            "status": STATUS_CURRENT,
        }

    @gl.public.write
    def challenge_attestation(self, attestation_id: u256, sources: str, context: str) -> dict:
        """Challenge a current attestation with new counter-evidence
        (2-3 source URLs, comma-separated). The claim is re-checked
        against the new sources; the new attestation supersedes the old
        one only when the verdict is the opposing substantive one
        (SUPPORTED against REFUTED, or the other way round), or a
        substantive verdict replacing an INSUFFICIENT_EVIDENCE record.
        A challenge that reaches the same verdict, or
        INSUFFICIENT_EVIDENCE, is rejected and the old record stands:
        not being able to tell is not counter-evidence."""
        if attestation_id not in self.attestations:
            _fail(ERR_EXPECTED, "unknown attestation " + str(attestation_id))
        old = self.attestations[attestation_id]
        if old.status != STATUS_CURRENT:
            _fail(
                ERR_EXPECTED,
                "attestation " + str(attestation_id) + " is " + old.status
                + "; only a current attestation can be challenged",
            )
        claim_id = old.claim_id
        if claim_id not in self.claims:
            _fail(ERR_EXPECTED, "claim " + str(claim_id) + " for attestation "
                  + str(attestation_id) + " is missing")
        claim = self.claims[claim_id]
        parsed = _parse_sources(sources)
        clean_context = ""
        if context != "":
            clean_context = _clean_text(context, "context", MAX_CONTEXT_CHARS)
        claim_mem = gl.storage.copy_to_memory(claim)
        result = self._verify_with_consensus(claim_mem.claim_text, clean_context, parsed)
        if result["verdict"] == VERDICT_INSUFFICIENT:
            _fail(
                ERR_EXPECTED,
                "challenge reached INSUFFICIENT_EVIDENCE; attestation stands",
            )
        if result["verdict"] == old.verdict:
            _fail(
                ERR_EXPECTED,
                "challenge reached the same verdict " + old.verdict + "; attestation stands",
            )
        new_id = self._store_attestation(claim_id, result, parsed, attestation_id)
        old.status = STATUS_SUPERSEDED
        old.superseded_by = new_id
        self.attestations[attestation_id] = old
        return {
            "attestation_id": new_id,
            "claim_id": claim_id,
            "verdict": result["verdict"],
            "confidence": result["confidence"],
            "supersedes": attestation_id,
            "status": STATUS_CURRENT,
        }

    # -- public views ---------------------------------------------------
    @gl.public.view
    def get_claim(self, claim_id: u256) -> dict:
        if claim_id not in self.claims:
            _fail(ERR_EXPECTED, "unknown claim " + str(claim_id))
        return _claim_to_dict(self.claims[claim_id])

    @gl.public.view
    def get_attestation(self, attestation_id: u256) -> dict:
        if attestation_id not in self.attestations:
            _fail(ERR_EXPECTED, "unknown attestation " + str(attestation_id))
        return _attestation_to_dict(self.attestations[attestation_id])

    @gl.public.view
    def get_current_attestation(self, claim_id: u256) -> dict:
        """The attestation a reader should trust for a claim: the one
        with status CURRENT. Raises when the claim has none yet."""
        if claim_id not in self.claims:
            _fail(ERR_EXPECTED, "unknown claim " + str(claim_id))
        for attestation_id in self.attestation_ids:
            att = self.attestations[attestation_id]
            if att.claim_id == claim_id and att.status == STATUS_CURRENT:
                return _attestation_to_dict(att)
        _fail(ERR_EXPECTED, "claim " + str(claim_id) + " has no current attestation yet")

    @gl.public.view
    def get_claim_id(self, claim_text: str) -> dict:
        """Look a claim up by its text (normalized the same way as
        submit_claim), so callers can find an existing claim instead of
        submitting a duplicate."""
        key = _normalize_claim(claim_text)
        if key not in self.claim_id_by_key:
            _fail(ERR_EXPECTED, "no claim with that text")
        return {"claim_id": self.claim_id_by_key[key]}

    @gl.public.view
    def list_claims(self) -> dict:
        return {"claims": [_claim_to_dict(self.claims[cid]) for cid in self.claim_ids]}

    @gl.public.view
    def list_attestations(self) -> dict:
        return {
            "attestations": [
                _attestation_to_dict(self.attestations[aid]) for aid in self.attestation_ids
            ]
        }

    @gl.public.view
    def stats(self) -> dict:
        current = u256(0)
        for attestation_id in self.attestation_ids:
            if self.attestations[attestation_id].status == STATUS_CURRENT:
                current = current + u256(1)
        return {
            "claims": u256(len(self.claim_ids)),
            "attestations": u256(len(self.attestation_ids)),
            "current_attestations": current,
        }
