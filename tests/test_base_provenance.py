from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SHA = {
    "default": "a" * 40,
    "one": "b" * 40,
    "two": "c" * 40,
    "three": "d" * 40,
}


def load_module():
    path = ROOT / "src/base_provenance.py"
    spec = importlib.util.spec_from_file_location("base_provenance", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


provenance = load_module()


def node(
    number: int,
    *,
    base_ref: str,
    base_sha: str,
    head_ref: str,
    head_sha: str,
) -> dict:
    return {
        "number": number,
        "state": "open",
        "merged_at": None,
        "draft": False,
        "author": {"login": "dancer-automation[bot]", "actor_id": 266_699_010},
        "base_ref": base_ref,
        "base_sha": base_sha,
        "base_repository_id": 979_193_317,
        "head_ref": head_ref,
        "head_sha": head_sha,
        "head_repository_id": 979_193_317,
    }


def stacked_payload(*, three_layers: bool = False) -> dict:
    nodes = [
        node(
            266,
            base_ref="master",
            base_sha=SHA["default"],
            head_ref="codex/fable-338-splash",
            head_sha=SHA["one"],
        ),
        node(
            267,
            base_ref="codex/fable-338-splash",
            base_sha=SHA["one"],
            head_ref="codex/fable-339-fonts",
            head_sha=SHA["two"],
        ),
    ]
    if three_layers:
        nodes.append(
            node(
                268,
                base_ref="codex/fable-339-fonts",
                base_sha=SHA["two"],
                head_ref="codex/fable-340-shell",
                head_sha=SHA["three"],
            )
        )
    target = nodes[-1]
    return {
        "repository": {
            "id": 979_193_317,
            "name_with_owner": "happycatlabs/fable",
            "default_ref": "master",
            "default_sha": SHA["default"],
        },
        "target": {
            "number": target["number"],
            "base_ref": target["base_ref"],
            "base_sha": target["base_sha"],
            "head_ref": target["head_ref"],
            "head_sha": target["head_sha"],
        },
        "stack": {
            "number": 269,
            "open": True,
            "base_ref": "master",
            "expected_actor": {
                "login": "dancer-automation[bot]",
                "actor_id": 266_699_010,
            },
            "pull_requests": nodes,
        },
    }


class BaseProvenanceTests(unittest.TestCase):
    def test_preserves_direct_default_behavior_without_stack_or_actor(self):
        payload = stacked_payload()
        payload["target"] = {
            "number": 266,
            "base_ref": "master",
            "base_sha": SHA["default"],
            "head_ref": "codex/fable-338-splash",
            "head_sha": SHA["one"],
        }
        payload["stack"] = {"untrusted": "ignored for direct mode"}

        result = provenance.validate_base_provenance(payload)

        self.assertEqual(result["mode"], "default")
        self.assertIsNone(result["stack"])
        self.assertIsNone(result["parent"])
        self.assertEqual(result["target"], payload["target"])

    def test_canonicalizes_two_and_three_layer_stacks(self):
        two = provenance.validate_base_provenance(stacked_payload())
        three = provenance.validate_base_provenance(stacked_payload(three_layers=True))

        self.assertEqual(two["schema_version"], provenance.BASE_PROVENANCE_VERSION)
        self.assertEqual(two["mode"], "stacked")
        self.assertEqual(two["parent"]["number"], 266)
        self.assertEqual(two["stack"]["target_index"], 1)
        self.assertEqual(two["stack"]["size"], 2)
        self.assertEqual(three["parent"]["number"], 267)
        self.assertEqual(three["stack"]["target_index"], 2)
        self.assertEqual(three["stack"]["size"], 3)

    def test_descendant_or_draft_changes_do_not_invalidate_lower_pr(self):
        payload = stacked_payload(three_layers=True)
        payload["target"] = {
            key: payload["stack"]["pull_requests"][1][key]
            for key in ("number", "base_ref", "base_sha", "head_ref", "head_sha")
        }
        before = provenance.validate_base_provenance(payload)
        payload["stack"]["pull_requests"][0]["draft"] = True
        payload["stack"]["pull_requests"][2]["draft"] = True
        payload["stack"]["pull_requests"][2]["head_sha"] = "e" * 40
        payload["stack"]["pull_requests"][2]["author"]["actor_id"] = 1

        after = provenance.validate_base_provenance(payload)

        self.assertEqual(after, before)
        self.assertEqual(after["stack"]["size"], 2)
        self.assertNotIn("draft", after["stack"]["nodes"][0])

    def test_rejects_stale_default_and_missing_or_closed_stack(self):
        stale = stacked_payload()
        stale["target"]["base_ref"] = "master"
        with self.assertRaisesRegex(provenance.BaseProvenanceError, "default_base_stale"):
            provenance.validate_base_provenance(stale)

        missing = stacked_payload()
        missing["stack"] = None
        with self.assertRaises(provenance.BaseProvenanceError):
            provenance.validate_base_provenance(missing)

        closed = stacked_payload()
        closed["stack"]["open"] = False
        with self.assertRaisesRegex(provenance.BaseProvenanceError, "stack_not_open"):
            provenance.validate_base_provenance(closed)

    def test_rejects_forks_wrong_actor_or_non_open_nodes(self):
        mutations = [
            ("head_repository_id", 1),
            ("base_repository_id", 1),
            ("merged_at", "2026-08-15T00:00:00Z"),
        ]
        for key, value in mutations:
            with self.subTest(key=key):
                payload = stacked_payload()
                payload["stack"]["pull_requests"][0][key] = value
                with self.assertRaises(provenance.BaseProvenanceError):
                    provenance.validate_base_provenance(payload)

        actor = stacked_payload()
        actor["stack"]["pull_requests"][0]["author"]["actor_id"] = 1
        with self.assertRaises(provenance.BaseProvenanceError):
            provenance.validate_base_provenance(actor)

    def test_uses_active_suffix_after_a_lower_layer_merges(self):
        payload = stacked_payload(three_layers=True)
        merged = payload["stack"]["pull_requests"][0]
        merged["state"] = "closed"
        merged["merged_at"] = "2026-08-15T09:42:02Z"
        active_root = payload["stack"]["pull_requests"][1]
        active_root["base_ref"] = "master"
        active_root["base_sha"] = SHA["default"]
        payload["stack"]["pull_requests"][2]["base_sha"] = active_root[
            "head_sha"
        ]

        result = provenance.validate_base_provenance(payload)

        self.assertEqual(result["stack"]["size"], 2)
        self.assertEqual(result["stack"]["target_index"], 1)
        self.assertEqual(
            [item["number"] for item in result["stack"]["nodes"]], [267, 268]
        )
        self.assertEqual(result["parent"]["number"], 267)

    def test_rejects_unmerged_history_or_closed_active_suffix(self):
        history = stacked_payload(three_layers=True)
        history["stack"]["pull_requests"][0]["state"] = "closed"
        history["stack"]["pull_requests"][0]["merged_at"] = None
        history["stack"]["pull_requests"][1]["base_ref"] = "master"
        history["stack"]["pull_requests"][1]["base_sha"] = SHA["default"]
        with self.assertRaises(provenance.BaseProvenanceError):
            provenance.validate_base_provenance(history)

        active = stacked_payload(three_layers=True)
        active["stack"]["pull_requests"][1]["state"] = "closed"
        active["stack"]["pull_requests"][1]["merged_at"] = (
            "2026-08-15T09:42:02Z"
        )
        with self.assertRaisesRegex(
            provenance.BaseProvenanceError, "stack_active_suffix_invalid"
        ):
            provenance.validate_base_provenance(active)

    def test_rejects_broken_order_cycles_duplicates_and_excess_depth(self):
        broken = stacked_payload()
        broken["stack"]["pull_requests"][1]["base_sha"] = "e" * 40
        broken["target"]["base_sha"] = "e" * 40
        with self.assertRaisesRegex(provenance.BaseProvenanceError, "stack_order_invalid"):
            provenance.validate_base_provenance(broken)

        for key in ("number", "head_ref", "head_sha"):
            with self.subTest(key=key):
                payload = stacked_payload()
                payload["stack"]["pull_requests"][1][key] = payload["stack"][
                    "pull_requests"
                ][0][key]
                payload["target"][key] = payload["stack"]["pull_requests"][1][key]
                with self.assertRaisesRegex(
                    provenance.BaseProvenanceError, "stack_cycle_or_duplicate"
                ):
                    provenance.validate_base_provenance(payload)

        with self.assertRaisesRegex(provenance.BaseProvenanceError, "stack_depth_invalid"):
            oversized = stacked_payload()
            oversized["stack"]["pull_requests"] *= 51
            provenance.validate_base_provenance(oversized)

        with self.assertRaisesRegex(
            provenance.BaseProvenanceError, "stack_active_suffix_invalid"
        ):
            provenance.validate_base_provenance(
                stacked_payload(three_layers=True), max_depth=2
            )

        root_cycle = stacked_payload()
        root_cycle["stack"]["pull_requests"][1]["head_ref"] = "master"
        root_cycle["stack"]["pull_requests"][1]["head_sha"] = SHA["default"]
        root_cycle["target"]["head_ref"] = "master"
        root_cycle["target"]["head_sha"] = SHA["default"]
        with self.assertRaisesRegex(
            provenance.BaseProvenanceError, "stack_cycle_or_duplicate"
        ):
            provenance.validate_base_provenance(root_cycle)

    def test_rejects_root_target_and_shape_drift(self):
        root = stacked_payload()
        root["stack"]["pull_requests"][0]["base_sha"] = "e" * 40
        with self.assertRaisesRegex(
            provenance.BaseProvenanceError, "stack_root_generation_mismatch"
        ):
            provenance.validate_base_provenance(root)

        target = stacked_payload()
        target["target"]["head_sha"] = "e" * 40
        with self.assertRaisesRegex(
            provenance.BaseProvenanceError, "stack_target_ambiguous"
        ):
            provenance.validate_base_provenance(target)

        extra = stacked_payload()
        extra["stack"]["pull_requests"][0]["unexpected"] = True
        with self.assertRaisesRegex(
            provenance.BaseProvenanceError, "stack_node_shape_invalid"
        ):
            provenance.validate_base_provenance(extra)

    def test_returns_fresh_canonical_data(self):
        payload = stacked_payload()
        original = copy.deepcopy(payload)
        result = provenance.validate_base_provenance(payload)
        payload["stack"]["pull_requests"][0]["head_ref"] = "changed"

        self.assertEqual(
            result["stack"]["nodes"][0]["head_ref"],
            original["stack"]["pull_requests"][0]["head_ref"],
        )

    def test_complete_ancestry_checks_every_dependency_edge(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = pathlib.Path(directory)
            def git(*args: str, capture: bool = False) -> str:
                result = subprocess.run(
                    ["git", "-C", str(repository), *args],
                    check=True,
                    capture_output=capture,
                    text=capture,
                )
                return result.stdout.strip() if capture else ""

            def commit(name: str) -> str:
                path = repository / f"{name}.txt"
                path.write_text(f"{name}\n")
                git("add", path.name)
                git("commit", "-qm", name)
                return git("rev-parse", "HEAD", capture=True)

            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            git("config", "user.email", "test@example.com")
            git("config", "user.name", "Test")
            root_sha = commit("root")
            parent_sha = commit("parent")
            linear_child_sha = commit("linear-child")

            def canonical_for(child_sha: str) -> dict:
                payload = stacked_payload()
                payload["repository"]["default_sha"] = root_sha
                payload["stack"]["pull_requests"][0]["base_sha"] = root_sha
                payload["stack"]["pull_requests"][0]["head_sha"] = parent_sha
                payload["stack"]["pull_requests"][1]["base_sha"] = parent_sha
                payload["stack"]["pull_requests"][1]["head_sha"] = child_sha
                payload["target"]["base_sha"] = parent_sha
                payload["target"]["head_sha"] = child_sha
                return provenance.validate_base_provenance(payload)

            linear = canonical_for(linear_child_sha)
            self.assertTrue(provenance.complete_ancestry_is_valid(repository, linear))

            git("checkout", "--orphan", "unrelated-child", capture=True)
            git("rm", "-rf", ".", capture=True)
            unrelated_child_sha = commit("unrelated-child")
            broken = canonical_for(unrelated_child_sha)

            self.assertEqual(
                provenance.ancestry_edges(broken),
                [(root_sha, parent_sha), (parent_sha, unrelated_child_sha)],
            )
            self.assertFalse(provenance.complete_ancestry_is_valid(repository, broken))
            provenance_path = repository / "provenance.json"
            error_path = repository / "error.json"
            provenance_path.write_text(json.dumps(broken))
            with self.assertRaisesRegex(
                provenance.BaseProvenanceError, "BASE_NOT_ANCESTOR"
            ):
                provenance.command_check_ancestry(
                    argparse.Namespace(
                        repository=str(repository),
                        provenance=str(provenance_path),
                        error_output=str(error_path),
                    )
                )
            self.assertEqual(
                json.loads(error_path.read_text()),
                {"status": "error", "code": "BASE_NOT_ANCESTOR"},
            )


if __name__ == "__main__":
    unittest.main()
