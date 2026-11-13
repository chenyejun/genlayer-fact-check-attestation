"""Direct tests for FactCheckAttestation — mocked web + LLM, no network.

Every consensus round runs against mocks: web pages are stubbed with
mock_web, model answers with mock_llm. run_validator() replays the
captured round-2 validator with the current mocks, so swapping the mocks
between the leader run and the replay simulates a validator that sees
different external data.
"""

import json
import re

from tests.direct.conftest import to_hex

URL_A = "https://example.com/article-a"
URL_B = "https://example.com/article-b"
URL_C = "https://example.com/article-c"
URL_D = "https://example.com/article-d"
URL_E = "https://example.com/article-e"
URL_F = "https://example.com/article-f"

CLAIM = "The city of Riverton opened a new public library in 2025."
SOURCES_AB = URL_A + "," + URL_B
SOURCES_CD = URL_C + "," + URL_D
SOURCES_EF = URL_E + "," + URL_F

PAGE_A = "Riverton opened a new public library in 2025, the mayor announced on Tuesday."
PAGE_B = "The new Riverton public library opened its doors in 2025 after two years of construction."
PAGE_C = "Riverton has no public library; the 2025 opening was cancelled before construction."
PAGE_D = "City records show the Riverton library project was cancelled in 2024."
PAGE_E = "Riverton's new public library welcomed its first visitors in 2025."
PAGE_F = "The Riverton public library opened in 2025 with 40,000 books on its shelves."

SUPPORTED = {
    "verdict": "SUPPORTED",
    "confidence": "high",
    "quotes": [
        {"source": URL_A, "quote": "Riverton opened a new public library in 2025."},
        {"source": URL_B, "quote": "The new Riverton public library opened its doors in 2025."},
    ],
}

REFUTED = {
    "verdict": "REFUTED",
    "confidence": "high",
    "quotes": [
        {"source": URL_C, "quote": "Riverton has no public library; the 2025 opening was cancelled before construction."},
        {"source": URL_D, "quote": "City records show the Riverton library project was cancelled in 2024."},
    ],
}

ONE_QUOTE_ONLY = {
    "verdict": "SUPPORTED",
    "confidence": "high",
    "quotes": [
        {"source": URL_A, "quote": "Riverton opened a new public library in 2025."},
    ],
}

# Same verdict as SUPPORTED, but quoted from the challenge sources E/F.
SUPPORTED_EF = {
    "verdict": "SUPPORTED",
    "confidence": "high",
    "quotes": [
        {"source": URL_E, "quote": "Riverton's new public library welcomed its first visitors in 2025."},
        {"source": URL_F, "quote": "The Riverton public library opened in 2025 with 40,000 books on its shelves."},
    ],
}


def _mock_pages(vm, pages):
    for url, body in pages.items():
        vm.mock_web(re.escape(url), {"status": 200, "body": body})


def _mock_page_status(vm, url, status):
    vm.mock_web(re.escape(url), {"status": status, "body": ""})


def _mock_llm(vm, answer):
    vm.mock_llm(r".*", json.dumps(answer))


def _submit(vm, contract, claim=CLAIM, sources=SOURCES_AB, context=""):
    return contract.submit_claim(claim, sources, context)


def _verify_supported(vm, contract, claim_id=1):
    _mock_pages(vm, {URL_A: PAGE_A, URL_B: PAGE_B})
    _mock_llm(vm, SUPPORTED)
    return contract.verify_claim(claim_id)


# -- submission ----------------------------------------------------------


def test_submit_claim_stores_claim(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/FactCheckAttestation.py")
    direct_vm.sender = direct_alice

    result = _submit(direct_vm, contract)

    assert result["claim_id"] == 1
    assert result["status"] == "SUBMITTED"

    claim = contract.get_claim(1)
    assert claim["claim_text"] == CLAIM
    assert claim["sources"] == [URL_A, URL_B]
    assert claim["context"] == ""
    assert claim["status"] == "SUBMITTED"
    assert claim["submitter"] == to_hex(direct_alice)
    assert claim["found"] is True


def test_submit_claim_with_context(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/FactCheckAttestation.py")

    _submit(direct_vm, contract, context="Riverton is a fictional city used in tests.")

    claim = contract.get_claim(1)
    assert claim["context"] == "Riverton is a fictional city used in tests."


def test_duplicate_claim_rejected(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/FactCheckAttestation.py")

    _submit(direct_vm, contract)

    with direct_vm.expect_revert("[EXPECTED]"):
        # Same text, different case and spacing: still a duplicate.
        _submit(direct_vm, contract, claim="  the CITY of riverton opened  a new public library in 2025. ")


def test_get_claim_id_finds_existing_claim(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/FactCheckAttestation.py")

    _submit(direct_vm, contract)

    assert contract.get_claim_id("THE CITY OF RIVERTON OPENED A NEW PUBLIC LIBRARY IN 2025.") == {
        "claim_id": 1
    }

    with direct_vm.expect_revert("[EXPECTED]"):
        contract.get_claim_id("A claim nobody submitted.")


def test_submit_validation(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/FactCheckAttestation.py")

    with direct_vm.expect_revert("[EXPECTED]"):
        _submit(direct_vm, contract, sources=URL_A)
    with direct_vm.expect_revert("[EXPECTED]"):
        _submit(direct_vm, contract, sources="http://example.com/article-a," + URL_B)
    with direct_vm.expect_revert("[EXPECTED]"):
        _submit(direct_vm, contract, sources=URL_A + "," + URL_A)
    with direct_vm.expect_revert("[EXPECTED]"):
        _submit(direct_vm, contract, claim="   ")
    with direct_vm.expect_revert("[EXPECTED]"):
        _submit(direct_vm, contract, sources=URL_A + "," + URL_B + "," + URL_C + "," + URL_D)


# -- verification --------------------------------------------------------


def test_verify_supported_stores_attestation(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/FactCheckAttestation.py")
    direct_vm.sender = direct_alice
    _submit(direct_vm, contract)

    result = _verify_supported(direct_vm, contract)

    assert result["attestation_id"] == 1
    assert result["claim_id"] == 1
    assert result["verdict"] == "SUPPORTED"
    assert result["confidence"] == "high"
    assert result["status"] == "CURRENT"

    att = contract.get_attestation(1)
    assert att["verdict"] == "SUPPORTED"
    assert att["confidence"] == "high"
    assert att["status"] == "CURRENT"
    assert att["supersedes"] == 0
    assert att["superseded_by"] == 0
    assert att["sources_loaded"] == 2
    assert att["sources_failed"] == 0
    assert att["verified_by"] == to_hex(direct_alice)
    assert [q["source"] for q in att["quotes"]] == [URL_A, URL_B]
    assert att["quotes"][0]["quote"] == "Riverton opened a new public library in 2025."

    assert contract.get_claim(1)["status"] == "ATTESTED"
    assert contract.get_current_attestation(1)["attestation_id"] == 1


def test_validator_agrees_with_leader(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/FactCheckAttestation.py")
    _submit(direct_vm, contract)
    _verify_supported(direct_vm, contract)

    # Replays the captured round-2 validator with the same mocks.
    assert direct_vm.run_validator() is True


def test_validator_disagrees_on_verdict(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/FactCheckAttestation.py")
    _submit(direct_vm, contract)
    _verify_supported(direct_vm, contract)

    # The validator sees different external data: its run says REFUTED.
    direct_vm.clear_mocks()
    _mock_pages(vm=direct_vm, pages={URL_A: PAGE_A, URL_B: PAGE_B})
    _mock_llm(direct_vm, REFUTED)

    assert direct_vm.run_validator() is False


def test_validator_disagrees_on_quotes(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/FactCheckAttestation.py")
    _submit(direct_vm, contract)
    _verify_supported(direct_vm, contract)

    # Same verdict, but the validator extracts a different span for
    # source A: after normalization the two sides share no quote from
    # that source, so the tolerant per-source check disagrees in both
    # rounds. (Whitespace or casing alone would agree — see the
    # tolerance tests below — but a different passage does not.)
    direct_vm.clear_mocks()
    _mock_pages(vm=direct_vm, pages={URL_A: PAGE_A, URL_B: PAGE_B})
    _mock_llm(
        direct_vm,
        {
            "verdict": "SUPPORTED",
            "confidence": "high",
            "quotes": [
                {"source": URL_A, "quote": "Riverton opened a new public library in 2025, the mayor announced."},
                {"source": URL_B, "quote": "The new Riverton public library opened its doors in 2025."},
            ],
        },
    )

    assert direct_vm.run_validator(index=0) is False
    assert direct_vm.run_validator() is False


def test_validator_agrees_when_quotes_differ_only_in_case_and_whitespace(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/FactCheckAttestation.py")
    _submit(direct_vm, contract)
    _verify_supported(direct_vm, contract)

    # The validator extracts the same passages, but with different
    # casing, extra spaces, and different trailing punctuation. After
    # normalization the quotes are the same, so both rounds agree.
    direct_vm.clear_mocks()
    _mock_pages(vm=direct_vm, pages={URL_A: PAGE_A, URL_B: PAGE_B})
    _mock_llm(
        direct_vm,
        {
            "verdict": "SUPPORTED",
            "confidence": "high",
            "quotes": [
                {"source": URL_A, "quote": "  RIVERTON   opened a new public library in 2025 "},
                {"source": URL_B, "quote": "the new riverton public library opened its doors in 2025."},
            ],
        },
    )

    assert direct_vm.run_validator(index=0) is True
    assert direct_vm.run_validator() is True


def test_validator_agrees_when_it_extracts_an_extra_quote(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/FactCheckAttestation.py")
    _submit(direct_vm, contract)
    _verify_supported(direct_vm, contract)

    # The validator lists one extra supporting sentence from source A.
    # The per-source rule only needs one shared quote per source, so
    # the extra quote does not break agreement in either round.
    direct_vm.clear_mocks()
    _mock_pages(vm=direct_vm, pages={URL_A: PAGE_A, URL_B: PAGE_B})
    _mock_llm(
        direct_vm,
        {
            "verdict": "SUPPORTED",
            "confidence": "high",
            "quotes": [
                {"source": URL_A, "quote": "Riverton opened a new public library in 2025."},
                {"source": URL_A, "quote": "the mayor announced on Tuesday."},
                {"source": URL_B, "quote": "The new Riverton public library opened its doors in 2025."},
            ],
        },
    )

    assert direct_vm.run_validator(index=0) is True
    assert direct_vm.run_validator() is True


def test_validator_disagrees_when_quotes_have_no_overlap(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/FactCheckAttestation.py")
    _submit(direct_vm, contract)
    _verify_supported(direct_vm, contract)

    # Same verdict and confidence, but the validator quotes different
    # passages from the same sources: no normalized quote is shared for
    # either source, so both rounds disagree.
    direct_vm.clear_mocks()
    _mock_pages(vm=direct_vm, pages={URL_A: PAGE_A, URL_B: PAGE_B})
    _mock_llm(
        direct_vm,
        {
            "verdict": "SUPPORTED",
            "confidence": "high",
            "quotes": [
                {"source": URL_A, "quote": "the mayor announced on Tuesday."},
                {"source": URL_B, "quote": "after two years of construction."},
            ],
        },
    )

    assert direct_vm.run_validator(index=0) is False
    assert direct_vm.run_validator() is False


def test_missing_quote_for_a_source_means_insufficient(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/FactCheckAttestation.py")
    _submit(direct_vm, contract)

    _mock_pages(direct_vm, {URL_A: PAGE_A, URL_B: PAGE_B})
    _mock_llm(direct_vm, ONE_QUOTE_ONLY)
    result = contract.verify_claim(1)

    assert result["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert result["confidence"] == "low"
    assert contract.get_attestation(1)["verdict"] == "INSUFFICIENT_EVIDENCE"


def test_one_source_down_still_verifies_from_the_other(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/FactCheckAttestation.py")
    _submit(direct_vm, contract)

    _mock_page_status(direct_vm, URL_A, 404)
    _mock_pages(direct_vm, {URL_B: PAGE_B})
    _mock_llm(
        direct_vm,
        {
            "verdict": "SUPPORTED",
            "confidence": "medium",
            "quotes": [
                {"source": URL_B, "quote": "The new Riverton public library opened its doors in 2025."},
            ],
        },
    )
    result = contract.verify_claim(1)

    assert result["verdict"] == "SUPPORTED"
    att = contract.get_attestation(1)
    assert att["sources_loaded"] == 1
    assert att["sources_failed"] == 1


def test_all_sources_down_fails_transient_or_external(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/FactCheckAttestation.py")
    _submit(direct_vm, contract)

    _mock_page_status(direct_vm, URL_A, 404)
    _mock_page_status(direct_vm, URL_B, 404)

    # Both sources failed with client errors: classified [EXTERNAL].
    with direct_vm.expect_revert("[EXTERNAL]"):
        contract.verify_claim(1)


def test_all_sources_server_error_is_transient(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/FactCheckAttestation.py")
    _submit(direct_vm, contract)

    _mock_page_status(direct_vm, URL_A, 500)
    _mock_page_status(direct_vm, URL_B, 503)

    with direct_vm.expect_revert("[TRANSIENT]"):
        contract.verify_claim(1)


def test_bad_model_answer_is_llm_error(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/FactCheckAttestation.py")
    _submit(direct_vm, contract)

    _mock_pages(direct_vm, {URL_A: PAGE_A, URL_B: PAGE_B})
    _mock_llm(direct_vm, {"verdict": "MAYBE", "confidence": "high", "quotes": []})

    with direct_vm.expect_revert("[LLM_ERROR]"):
        contract.verify_claim(1)


def test_verify_twice_rejected(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/FactCheckAttestation.py")
    _submit(direct_vm, contract)
    _verify_supported(direct_vm, contract)

    with direct_vm.expect_revert("[EXPECTED]"):
        contract.verify_claim(1)


def test_verify_unknown_claim_rejected(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/FactCheckAttestation.py")

    with direct_vm.expect_revert("[EXPECTED]"):
        contract.verify_claim(999)


# -- challenge -----------------------------------------------------------


def test_challenge_supersedes_attestation(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/FactCheckAttestation.py")
    _submit(direct_vm, contract)
    _verify_supported(direct_vm, contract)

    # Clear the verify-run mocks: mock matching is first-registered wins,
    # so the old LLM mock (SUPPORTED) would otherwise answer the challenge.
    direct_vm.clear_mocks()
    _mock_pages(direct_vm, {URL_C: PAGE_C, URL_D: PAGE_D})
    _mock_llm(direct_vm, REFUTED)
    result = contract.challenge_attestation(1, SOURCES_CD, "")

    assert result["attestation_id"] == 2
    assert result["verdict"] == "REFUTED"
    assert result["supersedes"] == 1
    assert result["status"] == "CURRENT"

    old = contract.get_attestation(1)
    assert old["status"] == "SUPERSEDED"
    assert old["superseded_by"] == 2

    new = contract.get_attestation(2)
    assert new["status"] == "CURRENT"
    assert new["supersedes"] == 1
    assert new["claim_id"] == 1

    assert contract.get_current_attestation(1)["attestation_id"] == 2


def test_challenge_same_verdict_rejected(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/FactCheckAttestation.py")
    _submit(direct_vm, contract)
    _verify_supported(direct_vm, contract)

    # Clear the verify-run mocks: mock matching is first-registered wins,
    # so the old LLM mock (SUPPORTED) would otherwise answer the challenge.
    direct_vm.clear_mocks()
    _mock_pages(direct_vm, {URL_E: PAGE_E, URL_F: PAGE_F})
    _mock_llm(direct_vm, SUPPORTED_EF)

    with direct_vm.expect_revert("[EXPECTED]"):
        contract.challenge_attestation(1, SOURCES_EF, "")

    assert contract.get_attestation(1)["status"] == "CURRENT"


def test_challenge_superseded_attestation_rejected(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/FactCheckAttestation.py")
    _submit(direct_vm, contract)
    _verify_supported(direct_vm, contract)

    # Clear the verify-run mocks: mock matching is first-registered wins,
    # so the old LLM mock (SUPPORTED) would otherwise answer the challenge.
    direct_vm.clear_mocks()
    _mock_pages(direct_vm, {URL_C: PAGE_C, URL_D: PAGE_D})
    _mock_llm(direct_vm, REFUTED)
    contract.challenge_attestation(1, SOURCES_CD, "")

    with direct_vm.expect_revert("[EXPECTED]"):
        contract.challenge_attestation(1, SOURCES_CD, "")


def test_challenge_unknown_attestation_rejected(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/FactCheckAttestation.py")

    with direct_vm.expect_revert("[EXPECTED]"):
        contract.challenge_attestation(7, SOURCES_CD, "")


# -- views ---------------------------------------------------------------


def test_views_and_stats(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/FactCheckAttestation.py")
    _submit(direct_vm, contract)
    _submit(direct_vm, contract, claim="The Riverton library has 40,000 books.")
    _verify_supported(direct_vm, contract, claim_id=1)

    claims = contract.list_claims()
    assert len(claims["claims"]) == 2
    assert claims["claims"][0]["claim_id"] == 1
    assert claims["claims"][1]["claim_id"] == 2

    atts = contract.list_attestations()
    assert len(atts["attestations"]) == 1
    assert atts["attestations"][0]["claim_id"] == 1

    assert contract.stats() == {
        "claims": 2,
        "attestations": 1,
        "current_attestations": 1,
    }

    with direct_vm.expect_revert("[EXPECTED]"):
        contract.get_claim(99)
    with direct_vm.expect_revert("[EXPECTED]"):
        contract.get_attestation(99)
    with direct_vm.expect_revert("[EXPECTED]"):
        contract.get_current_attestation(2)
