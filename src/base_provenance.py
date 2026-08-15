"""Pure validation and canonicalization for pull-request base provenance."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
from typing import Any


BASE_PROVENANCE_VERSION = "codex-review-base-provenance/v1"
DEFAULT_MAX_STACK_DEPTH = 8
MAX_STACK_RESPONSE_NODES = 100
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
REF_PATTERN = re.compile(r"^(?!-)(?!.*\.\.)(?!.*[\s~^:?*\[\\])[^\x00-\x1f\x7f]+$")
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class BaseProvenanceError(ValueError):
    """The supplied pull-request generation or stack topology is not trusted."""


def _record(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BaseProvenanceError(f"{name}_invalid")
    return value


def _exact_record(value: Any, name: str, keys: set[str]) -> dict[str, Any]:
    record = _record(value, name)
    if set(record) != keys:
        raise BaseProvenanceError(f"{name}_shape_invalid")
    return record


def _positive_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BaseProvenanceError(f"{name}_invalid")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or value == "":
        raise BaseProvenanceError(f"{name}_invalid")
    return value


def _sha(value: Any, name: str) -> str:
    candidate = _text(value, name)
    if SHA_PATTERN.fullmatch(candidate) is None:
        raise BaseProvenanceError(f"{name}_invalid")
    return candidate


def _ref(value: Any, name: str) -> str:
    candidate = _text(value, name)
    if REF_PATTERN.fullmatch(candidate) is None:
        raise BaseProvenanceError(f"{name}_invalid")
    return candidate


def _repository(value: Any) -> dict[str, Any]:
    repository = _exact_record(
        value,
        "repository",
        {"id", "name_with_owner", "default_ref", "default_sha"},
    )
    repository_id = _positive_integer(repository["id"], "repository_id")
    name_with_owner = _text(repository["name_with_owner"], "repository_name")
    if REPOSITORY_PATTERN.fullmatch(name_with_owner) is None:
        raise BaseProvenanceError("repository_name_invalid")
    return {
        "id": repository_id,
        "name_with_owner": name_with_owner,
        "default_ref": _ref(repository["default_ref"], "default_ref"),
        "default_sha": _sha(repository["default_sha"], "default_sha"),
    }


def _target(value: Any) -> dict[str, Any]:
    target = _exact_record(
        value,
        "target",
        {"number", "base_ref", "base_sha", "head_ref", "head_sha"},
    )
    return {
        "number": _positive_integer(target["number"], "target_number"),
        "base_ref": _ref(target["base_ref"], "target_base_ref"),
        "base_sha": _sha(target["base_sha"], "target_base_sha"),
        "head_ref": _ref(target["head_ref"], "target_head_ref"),
        "head_sha": _sha(target["head_sha"], "target_head_sha"),
    }


def _expected_actor(value: Any) -> dict[str, Any]:
    actor = _exact_record(value, "stack_actor", {"login", "actor_id"})
    return {
        "login": _text(actor["login"], "stack_actor_login"),
        "actor_id": _positive_integer(actor["actor_id"], "stack_actor_id"),
    }


def _stack_node(
    value: Any,
    *,
    repository_id: int,
    expected_actor: dict[str, Any],
) -> dict[str, Any]:
    node = _exact_record(
        value,
        "stack_node",
        {
            "number",
            "state",
            "merged_at",
            "draft",
            "author",
            "base_ref",
            "base_sha",
            "base_repository_id",
            "head_ref",
            "head_sha",
            "head_repository_id",
        },
    )
    author = _exact_record(node["author"], "stack_node_author", {"login", "actor_id"})
    state = node["state"]
    merged_at = node["merged_at"]
    if (
        state not in {"open", "closed"}
        or (state == "open" and merged_at is not None)
        or (
            state == "closed"
            and (not isinstance(merged_at, str) or merged_at == "")
        )
        or not isinstance(node["draft"], bool)
        or author.get("login") != expected_actor["login"]
        or author.get("actor_id") != expected_actor["actor_id"]
        or node["base_repository_id"] != repository_id
        or node["head_repository_id"] != repository_id
    ):
        raise BaseProvenanceError("stack_node_identity_invalid")
    return {
        "number": _positive_integer(node["number"], "stack_node_number"),
        "state": state,
        "merged_at": merged_at,
        "author": dict(expected_actor),
        "base_ref": _ref(node["base_ref"], "stack_node_base_ref"),
        "base_sha": _sha(node["base_sha"], "stack_node_base_sha"),
        "base_repository_id": repository_id,
        "head_ref": _ref(node["head_ref"], "stack_node_head_ref"),
        "head_sha": _sha(node["head_sha"], "stack_node_head_sha"),
        "head_repository_id": repository_id,
    }


def _direct_provenance(
    repository: dict[str, Any], target: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": BASE_PROVENANCE_VERSION,
        "mode": "default",
        "repository": {
            "id": repository["id"],
            "name_with_owner": repository["name_with_owner"],
        },
        "default": {
            "ref": repository["default_ref"],
            "sha": repository["default_sha"],
        },
        "target": dict(target),
        "parent": None,
        "stack": None,
    }


def validate_base_provenance(
    payload: Any,
    *,
    max_depth: int = DEFAULT_MAX_STACK_DEPTH,
) -> dict[str, Any]:
    """Return a deterministic trusted generation or raise ``BaseProvenanceError``.

    Network clients reduce GitHub responses to this exact input. Direct default-
    branch PRs deliberately ignore stack metadata so their existing review
    behavior does not depend on descendants.
    """

    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 2:
        raise BaseProvenanceError("max_depth_invalid")
    root = _exact_record(payload, "provenance", {"repository", "target", "stack"})
    repository = _repository(root["repository"])
    target = _target(root["target"])

    if (
        target["base_ref"] == repository["default_ref"]
        and target["base_sha"] == repository["default_sha"]
    ):
        return _direct_provenance(repository, target)
    if target["base_ref"] == repository["default_ref"]:
        raise BaseProvenanceError("default_base_stale")

    stack = _exact_record(
        root["stack"],
        "stack",
        {"number", "open", "base_ref", "expected_actor", "pull_requests"},
    )
    if stack["open"] is not True:
        raise BaseProvenanceError("stack_not_open")
    if _ref(stack["base_ref"], "stack_base_ref") != repository["default_ref"]:
        raise BaseProvenanceError("stack_root_ref_mismatch")
    stack_number = _positive_integer(stack["number"], "stack_number")
    expected_actor = _expected_actor(stack["expected_actor"])
    values = stack["pull_requests"]
    if (
        not isinstance(values, list)
        or not 2 <= len(values) <= MAX_STACK_RESPONSE_NODES
    ):
        raise BaseProvenanceError("stack_depth_invalid")
    raw_target_indexes = [
        index
        for index, value in enumerate(values)
        if isinstance(value, dict)
        and value.get("number") == target["number"]
        and value.get("base_ref") == target["base_ref"]
        and value.get("base_sha") == target["base_sha"]
        and value.get("head_ref") == target["head_ref"]
        and value.get("head_sha") == target["head_sha"]
    ]
    if len(raw_target_indexes) != 1:
        raise BaseProvenanceError("stack_target_ambiguous")
    dependency_values = values[: raw_target_indexes[0] + 1]
    nodes = [
        _stack_node(
            value,
            repository_id=repository["id"],
            expected_actor=expected_actor,
        )
        for value in dependency_values
    ]

    numbers: set[int] = set()
    head_refs: set[str] = {repository["default_ref"]}
    head_shas: set[str] = {repository["default_sha"]}
    for node in nodes:
        if (
            node["number"] in numbers
            or node["head_ref"] in head_refs
            or node["head_sha"] in head_shas
            or node["base_sha"] == node["head_sha"]
        ):
            raise BaseProvenanceError("stack_cycle_or_duplicate")
        numbers.add(node["number"])
        head_refs.add(node["head_ref"])
        head_shas.add(node["head_sha"])

    active_root_indexes = [
        index
        for index, node in enumerate(nodes)
        if node["state"] == "open"
        and node["base_ref"] == repository["default_ref"]
        and node["base_sha"] == repository["default_sha"]
    ]
    if len(active_root_indexes) != 1:
        raise BaseProvenanceError("stack_root_generation_mismatch")
    active_root_index = active_root_indexes[0]
    if any(node["state"] != "closed" for node in nodes[:active_root_index]):
        raise BaseProvenanceError("stack_history_invalid")
    active_nodes = nodes[active_root_index:]
    if (
        not 2 <= len(active_nodes) <= max_depth
        or any(node["state"] != "open" for node in active_nodes)
    ):
        raise BaseProvenanceError("stack_active_suffix_invalid")

    target_indexes: list[int] = []
    for index, node in enumerate(active_nodes):
        if index > 0:
            parent = active_nodes[index - 1]
            if (
                node["base_ref"] != parent["head_ref"]
                or node["base_sha"] != parent["head_sha"]
            ):
                raise BaseProvenanceError("stack_order_invalid")
        if (
            node["number"] == target["number"]
            and node["base_ref"] == target["base_ref"]
            and node["base_sha"] == target["base_sha"]
            and node["head_ref"] == target["head_ref"]
            and node["head_sha"] == target["head_sha"]
        ):
            target_indexes.append(index)

    if len(target_indexes) != 1:
        raise BaseProvenanceError("stack_target_ambiguous")
    target_index = target_indexes[0]
    if target_index == 0:
        raise BaseProvenanceError("stack_target_not_dependent")
    parent_node = active_nodes[target_index - 1]

    return {
        "schema_version": BASE_PROVENANCE_VERSION,
        "mode": "stacked",
        "repository": {
            "id": repository["id"],
            "name_with_owner": repository["name_with_owner"],
        },
        "default": {
            "ref": repository["default_ref"],
            "sha": repository["default_sha"],
        },
        "target": dict(target),
        "parent": {
            "number": parent_node["number"],
            "head_ref": parent_node["head_ref"],
            "head_sha": parent_node["head_sha"],
        },
        "stack": {
            "number": stack_number,
            "base_ref": repository["default_ref"],
            "open": True,
            "target_index": target_index,
            "size": len(active_nodes),
            "nodes": active_nodes,
        },
    }


def ancestry_edges(provenance: Any) -> list[tuple[str, str]]:
    """Return every dependency edge that must be ancestral for this target."""

    canonical = _exact_record(
        provenance,
        "canonical_provenance",
        {"schema_version", "mode", "repository", "default", "target", "parent", "stack"},
    )
    if canonical["schema_version"] != BASE_PROVENANCE_VERSION:
        raise BaseProvenanceError("canonical_provenance_version_invalid")
    target = _target(canonical["target"])
    if canonical["mode"] == "default":
        if canonical["parent"] is not None or canonical["stack"] is not None:
            raise BaseProvenanceError("canonical_default_invalid")
        return [(target["base_sha"], target["head_sha"])]
    if canonical["mode"] != "stacked":
        raise BaseProvenanceError("canonical_provenance_mode_invalid")
    stack = _record(canonical["stack"], "canonical_stack")
    nodes = stack.get("nodes")
    target_index = stack.get("target_index")
    if (
        not isinstance(nodes, list)
        or not nodes
        or type(target_index) is not int
        or target_index != len(nodes) - 1
    ):
        raise BaseProvenanceError("canonical_stack_invalid")
    last = _record(nodes[-1], "canonical_target_node")
    if any(last.get(key) != target[key] for key in target):
        raise BaseProvenanceError("canonical_target_invalid")
    edges: list[tuple[str, str]] = []
    for node in nodes:
        record = _record(node, "canonical_stack_node")
        edges.append(
            (
                _sha(record.get("base_sha"), "canonical_base_sha"),
                _sha(record.get("head_sha"), "canonical_head_sha"),
            )
        )
    return edges


def complete_ancestry_is_valid(repository: pathlib.Path, provenance: Any) -> bool:
    for base_sha, head_sha in ancestry_edges(provenance):
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "merge-base",
                "--is-ancestor",
                base_sha,
                head_sha,
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return False
    return True


def command_check_ancestry(args: argparse.Namespace) -> None:
    provenance = json.loads(
        pathlib.Path(args.provenance).read_text(encoding="utf-8", errors="strict")
    )
    if complete_ancestry_is_valid(pathlib.Path(args.repository), provenance):
        return
    error_output = pathlib.Path(args.error_output)
    error_output.parent.mkdir(parents=True, exist_ok=True)
    error_output.write_text(
        json.dumps({"status": "error", "code": "BASE_NOT_ANCESTOR"}) + "\n",
        encoding="utf-8",
    )
    raise BaseProvenanceError("BASE_NOT_ANCESTOR")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(required=True)
    ancestry = commands.add_parser("check-ancestry")
    ancestry.add_argument("--repository", required=True)
    ancestry.add_argument("--provenance", required=True)
    ancestry.add_argument("--error-output", required=True)
    ancestry.set_defaults(handler=command_check_ancestry)
    return root


if __name__ == "__main__":
    arguments = parser().parse_args()
    arguments.handler(arguments)
