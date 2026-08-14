from __future__ import annotations

import argparse
import copy
import json
import pathlib
import re
from typing import Any

import review_publication
import review_publisher
import review_contract


CANDIDATE_PACKET_VERSION = "codex-review-resolution-candidates/v1"
PLAN_VERSION = "codex-review-resolution-plan/v1"
RECEIPT_VERSION = "codex-review-resolution/v1"
MAX_CANDIDATES = 20
MAX_REASON_CHARS = 500
MAX_PROMPT_BYTES = 800_000
DECISIONS = {
    "RESOLVE_ADDRESSED",
    "RESOLVE_SUPERSEDED",
    "KEEP_STILL_VALID",
    "KEEP_AMBIGUOUS",
}
RESOLVE_DECISIONS = {"RESOLVE_ADDRESSED", "RESOLVE_SUPERSEDED"}
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class ResolutionContractError(ValueError):
    pass


load_json = review_publisher.load_json
write_json = review_publisher.write_json


def canonical_sha256(value: Any) -> str:
    return review_publisher.canonical_request_sha256(value)


def _expected_generation(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "head_sha": result.get("head_sha"),
        "base_ref": result.get("base_ref"),
        "base_sha": result.get("base_sha"),
    }


def validate_publication_pair(
    result: Any,
    receipt: Any,
    *,
    repository: str,
    pull_number: int,
    require_inline: bool,
    expected_run_id: int | None = None,
    expected_workflow_sha: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(result, dict) or not isinstance(receipt, dict):
        raise ResolutionContractError("RESOLUTION_PROVENANCE_INVALID")
    publication = result.get("publication")
    coverage = result.get("coverage")
    lookup = result.get("lookup_context")
    expected_generation = _expected_generation(result)
    observed_generation = receipt.get("observed_generation")
    review = receipt.get("review")
    head_sha = result.get("head_sha")
    base_sha = result.get("base_sha")
    base_ref = result.get("base_ref")
    valid = (
        result.get("schema_version") == "codex-review-result/v3"
        and result.get("verdict") in {"clean", "blocking_findings"}
        and result.get("error") is None
        and result.get("pull_number") == pull_number
        and result.get("state") == "open"
        and isinstance(head_sha, str)
        and review_publisher.SHA_PATTERN.fullmatch(head_sha) is not None
        and isinstance(base_sha, str)
        and review_publisher.SHA_PATTERN.fullmatch(base_sha) is not None
        and isinstance(base_ref, str)
        and bool(base_ref)
        and result.get("review_scope") == "source_context_v1"
        and isinstance(coverage, dict)
        and coverage.get("complete") is True
        and coverage.get("truncated") is False
        and isinstance(lookup, dict)
        and lookup.get("complete") is True
        and isinstance(publication, dict)
        and publication.get("status") == "published"
        and publication.get("mode") in {"inline", "summary"}
        and receipt.get("schema_version") == "codex-review-publication/v1"
        and receipt.get("status") == "published"
        and receipt.get("repository") == repository
        and receipt.get("pull_number") == pull_number
        and type(receipt.get("actions_run_id")) is int
        and receipt["actions_run_id"] > 0
        and receipt.get("expected_generation") == expected_generation
        and isinstance(observed_generation, dict)
        and observed_generation.get("state") == "open"
        and observed_generation.get("head_sha") == result.get("head_sha")
        and observed_generation.get("base_ref") == result.get("base_ref")
        and observed_generation.get("base_sha") == result.get("base_sha")
        and observed_generation.get("default_branch") == result.get("base_ref")
        and observed_generation.get("default_branch_sha") == result.get("base_sha")
        and receipt.get("actor")
        == {
            "login": review_publisher.EXPECTED_DANCER_LOGIN,
            "id": review_publisher.EXPECTED_DANCER_ACTOR_ID,
        }
        and receipt.get("event") == "COMMENT"
        and receipt.get("mode") == publication.get("mode")
        and isinstance(review, dict)
        and type(review.get("id")) is int
        and review["id"] > 0
        and review.get("commit_id") == result.get("head_sha")
        and review.get("url")
        == f"https://github.com/{repository}/pull/{pull_number}#pullrequestreview-{review['id']}"
        and isinstance(review.get("request_sha256"), str)
        and SHA256_PATTERN.fullmatch(review["request_sha256"]) is not None
    )
    if expected_run_id is not None:
        valid = valid and receipt.get("actions_run_id") == expected_run_id
    if expected_workflow_sha is not None:
        valid = (
            valid
            and review_publisher.SHA_PATTERN.fullmatch(expected_workflow_sha)
            is not None
            and result.get("workflow_revision") == expected_workflow_sha
        )
    if not valid or (require_inline and publication.get("mode") != "inline"):
        raise ResolutionContractError("RESOLUTION_PROVENANCE_INVALID")

    findings = result.get("findings")
    fingerprints = result.get("finding_fingerprints")
    if (
        not isinstance(findings, list)
        or not isinstance(fingerprints, list)
        or any(not isinstance(item, dict) for item in findings)
        or [item.get("fingerprint") for item in findings] != fingerprints
        or any(
            not isinstance(value, str)
            or SHA256_PATTERN.fullmatch(value) is None
            for value in fingerprints
        )
        or any(
            review_contract.finding_fingerprint(item) != item.get("fingerprint")
            for item in findings
        )
    ):
        raise ResolutionContractError("RESOLUTION_PROVENANCE_INVALID")
    if require_inline and not findings:
        raise ResolutionContractError("RESOLUTION_PROVENANCE_INVALID")
    return result, receipt


def inline_request(
    result: dict[str, Any], *, repository: str, run_id: int
) -> dict[str, Any]:
    files: dict[str, list[list[int]]] = {}
    for finding in result["findings"]:
        path = review_publication.safe_relative_path(finding.get("file"))
        start_line = finding.get("start_line")
        line = finding.get("line")
        if (
            path is None
            or type(start_line) is not int
            or type(line) is not int
            or start_line < 1
            or start_line > line
        ):
            raise ResolutionContractError("RESOLUTION_PROVENANCE_INVALID")
        files.setdefault(path, []).append([start_line, line])
    comment_map = {
        "schema_version": review_publication.COMMENT_MAP_VERSION,
        "complete": True,
        "pull_number": result["pull_number"],
        "head_sha": result["head_sha"],
        "base_ref": result["base_ref"],
        "base_sha": result["base_sha"],
        "diff_sha256": "0" * 64,
        "files": files,
    }
    _, request, _ = review_publication.plan_publication(
        copy.deepcopy(result),
        comment_map,
        repository=repository,
        run_id=run_id,
    )
    if not request.get("comments") or len(request["comments"]) != len(result["findings"]):
        raise ResolutionContractError("RESOLUTION_PROVENANCE_INVALID")
    return request


def validate_inline_provenance(
    result: Any,
    receipt: Any,
    *,
    repository: str,
    pull_number: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    result, receipt = validate_publication_pair(
        result,
        receipt,
        repository=repository,
        pull_number=pull_number,
        require_inline=True,
    )
    request = inline_request(
        result, repository=repository, run_id=receipt["actions_run_id"]
    )
    if (
        review_publisher.canonical_request_sha256(request)
        != receipt["review"]["request_sha256"]
    ):
        raise ResolutionContractError("RESOLUTION_PROVENANCE_INVALID")
    return result, receipt, request


def build_candidate_packet(
    *,
    repository: str,
    pull_number: int,
    run_id: int,
    workflow_sha: str,
    current_result: dict[str, Any],
    current_receipt: dict[str, Any],
    candidates: list[dict[str, Any]],
    excluded: dict[str, int],
    observations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    current_result, current_receipt = validate_publication_pair(
        current_result,
        current_receipt,
        repository=repository,
        pull_number=pull_number,
        require_inline=False,
        expected_run_id=run_id,
        expected_workflow_sha=workflow_sha,
    )
    proven_candidate_count = len(candidates)
    if proven_candidate_count > MAX_CANDIDATES:
        status = "overflow"
        candidates = []
    else:
        status = "ready" if candidates else "no_candidates"
    current_fingerprints = set(current_result["finding_fingerprints"])
    normalized: list[dict[str, Any]] = []
    seen_threads: set[str] = set()
    for candidate in candidates:
        thread_id = candidate.get("thread_id")
        fingerprint = candidate.get("provenance", {}).get("fingerprint")
        if (
            not isinstance(thread_id, str)
            or not thread_id
            or thread_id in seen_threads
            or not isinstance(fingerprint, str)
            or SHA256_PATTERN.fullmatch(fingerprint) is None
        ):
            raise ResolutionContractError("RESOLUTION_CANDIDATE_INVALID")
        seen_threads.add(thread_id)
        prepared = copy.deepcopy(candidate)
        prepared["deterministic_decision"] = (
            "KEEP_STILL_VALID" if fingerprint in current_fingerprints else None
        )
        normalized.append(prepared)
    normalized.sort(key=lambda item: item["thread_id"])
    safe_observations = copy.deepcopy(observations or [])
    if any(
        not isinstance(item, dict)
        or set(item) != {"thread_id", "reason", "is_resolved"}
        or not isinstance(item.get("thread_id"), str)
        or not item["thread_id"]
        or len(item["thread_id"]) > 256
        or not isinstance(item.get("reason"), str)
        or re.fullmatch(r"[a-z0-9_]+", item["reason"]) is None
        or item.get("is_resolved") is not False
        for item in safe_observations
    ):
        raise ResolutionContractError("RESOLUTION_OBSERVATION_INVALID")
    packet = {
        "schema_version": CANDIDATE_PACKET_VERSION,
        "status": status,
        "repository": repository,
        "pull_number": pull_number,
        "actions_run_id": run_id,
        "workflow_revision": workflow_sha,
        "current_generation": _expected_generation(current_result),
        "current_publication": {
            "actions_run_id": current_receipt["actions_run_id"],
            "review_id": current_receipt["review"]["id"],
            "request_sha256": current_receipt["review"]["request_sha256"],
            "result_sha256": canonical_sha256(current_result),
            "receipt_sha256": canonical_sha256(current_receipt),
        },
        "candidate_count": len(normalized) if status != "overflow" else 0,
        "model_candidate_count": (
            sum(item["deterministic_decision"] is None for item in normalized)
            if status != "overflow"
            else 0
        ),
        "excluded": dict(sorted(excluded.items())),
        "observations": sorted(
            safe_observations,
            key=lambda item: (str(item.get("thread_id")), str(item.get("reason"))),
        ),
        "candidates": normalized if status != "overflow" else [],
        "overflow_count": proven_candidate_count if status == "overflow" else 0,
    }
    return packet


def render_prompt(instructions: str, packet: dict[str, Any]) -> str:
    model_candidates = [
        {
            "thread_id": item["thread_id"],
            "prior_fingerprint": item["provenance"]["fingerprint"],
            "prior_finding": item["prior_finding"],
            "current_evidence": item["current_evidence"],
        }
        for item in packet.get("candidates", [])
        if item.get("deterministic_decision") is None
    ]
    evidence = json.dumps(
        {
            "current_head_sha": packet["current_generation"]["head_sha"],
            "candidates": model_candidates,
        },
        indent=2,
        ensure_ascii=False,
    )
    if "<<<BEGIN" in evidence or "<<<END" in evidence:
        raise ResolutionContractError("RESOLUTION_UNTRUSTED_MARKER_COLLISION")
    prompt = (
        instructions.rstrip()
        + "\n\n<<<BEGIN UNTRUSTED RESOLUTION EVIDENCE>>>\n"
        + evidence
        + "\n<<<END UNTRUSTED RESOLUTION EVIDENCE>>>\n"
    )
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ResolutionContractError("RESOLUTION_INPUT_LIMIT_EXCEEDED")
    return prompt


def validate_model_output(output: Any, packet: dict[str, Any]) -> dict[str, tuple[str, str]]:
    expected = {
        item["thread_id"]: item["provenance"]["fingerprint"]
        for item in packet.get("candidates", [])
        if item.get("deterministic_decision") is None
    }
    if (
        not isinstance(output, dict)
        or set(output) != {"current_head_sha", "decisions"}
        or output.get("current_head_sha")
        != packet.get("current_generation", {}).get("head_sha")
    ):
        raise ResolutionContractError("RESOLUTION_MODEL_OUTPUT_INVALID")
    decisions = output["decisions"]
    if not isinstance(decisions, list) or len(decisions) != len(expected):
        raise ResolutionContractError("RESOLUTION_MODEL_OUTPUT_INVALID")
    normalized: dict[str, str] = {}
    reasons: dict[str, str] = {}
    for item in decisions:
        if not isinstance(item, dict) or set(item) != {
            "thread_id",
            "prior_fingerprint",
            "decision",
            "reason",
        }:
            raise ResolutionContractError("RESOLUTION_MODEL_OUTPUT_INVALID")
        thread_id = item["thread_id"]
        fingerprint = item["prior_fingerprint"]
        decision = item["decision"]
        reason = item["reason"]
        if (
            thread_id not in expected
            or thread_id in normalized
            or fingerprint != expected[thread_id]
            or decision not in DECISIONS
            or not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > MAX_REASON_CHARS
        ):
            raise ResolutionContractError("RESOLUTION_MODEL_OUTPUT_INVALID")
        normalized[thread_id] = decision
        reasons[thread_id] = reason.strip()
    if set(normalized) != set(expected):
        raise ResolutionContractError("RESOLUTION_MODEL_OUTPUT_INVALID")
    return {
        thread_id: (normalized[thread_id], reasons[thread_id])
        for thread_id in normalized
    }


def build_plan(packet: Any, model_output: Any | None) -> dict[str, Any]:
    if (
        not isinstance(packet, dict)
        or packet.get("schema_version") != CANDIDATE_PACKET_VERSION
        or packet.get("status") not in {"ready", "no_candidates", "overflow"}
    ):
        raise ResolutionContractError("RESOLUTION_PACKET_INVALID")
    if packet["status"] == "overflow":
        return {
            "schema_version": PLAN_VERSION,
            "status": "overflow",
            "packet_sha256": canonical_sha256(packet),
            "current_generation": packet["current_generation"],
            "current_publication": packet["current_publication"],
            "decisions": [],
        }
    if packet.get("model_candidate_count") == 0 and model_output is None:
        model_output = {
            "current_head_sha": packet.get("current_generation", {}).get("head_sha"),
            "decisions": [],
        }
    parsed = validate_model_output(model_output, packet)
    decisions = []
    for candidate in packet["candidates"]:
        deterministic = candidate.get("deterministic_decision")
        if deterministic is not None:
            decision = deterministic
            reason = "The exact prior finding fingerprint is present in the current v3 result."
        else:
            decision, reason = parsed[candidate["thread_id"]]
        decisions.append(
            {
                "thread_id": candidate["thread_id"],
                "decision": decision,
                "reason": reason,
                "candidate_sha256": canonical_sha256(candidate),
            }
        )
    return {
        "schema_version": PLAN_VERSION,
        "status": "ready" if decisions else "no_candidates",
        "packet_sha256": canonical_sha256(packet),
        "current_generation": packet["current_generation"],
        "current_publication": packet["current_publication"],
        "decisions": decisions,
    }


def command_prompt(args: argparse.Namespace) -> None:
    packet = load_json(pathlib.Path(args.packet))
    prompt = render_prompt(
        pathlib.Path(args.instructions).read_text(encoding="utf-8", errors="strict"),
        packet,
    )
    output = pathlib.Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(prompt, encoding="utf-8")


def command_plan(args: argparse.Namespace) -> None:
    packet = load_json(pathlib.Path(args.packet))
    model_output = None
    if args.model_output and pathlib.Path(args.model_output).is_file():
        model_output = load_json(pathlib.Path(args.model_output))
    write_json(pathlib.Path(args.output), build_plan(packet, model_output))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(required=True)
    prompt = commands.add_parser("prompt")
    prompt.add_argument("--packet", required=True)
    prompt.add_argument("--instructions", required=True)
    prompt.add_argument("--output", required=True)
    prompt.set_defaults(handler=command_prompt)
    plan = commands.add_parser("plan")
    plan.add_argument("--packet", required=True)
    plan.add_argument("--model-output")
    plan.add_argument("--output", required=True)
    plan.set_defaults(handler=command_plan)
    return root


if __name__ == "__main__":
    arguments = parser().parse_args()
    arguments.handler(arguments)
