from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import source_context  # noqa: E402

HEAD_SHA = "a" * 40
BASE_SHA = "b" * 40
BINDING = {
    "pull_number": 198,
    "head_sha": HEAD_SHA,
    "base_ref": "master",
    "base_sha": BASE_SHA,
}


class RepositoryFixture:
    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        subprocess.run(
            ["git", "init", "-q", "-b", "master", str(self.root)], check=True
        )
        self.git("config", "user.email", "review@example.com")
        self.git("config", "user.name", "Review Fixture")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def write(self, path: str, content: str) -> pathlib.Path:
        destination = self.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)
        return destination

    def commit(self) -> None:
        self.git("add", ".")
        self.git("commit", "-q", "-m", "fixture")

    def cleanup(self) -> None:
        self.temporary.cleanup()


class SourceContextTests(unittest.TestCase):
    def setUp(self):
        self.repository = RepositoryFixture()
        self.addCleanup(self.repository.cleanup)

    def build(self, changed_files: list[str]) -> dict:
        output = self.repository.root / "context.json"
        return source_context.build_source_context(
            self.repository.root, changed_files, BINDING, output
        )

    def test_root_alias_finds_unchanged_caller_and_dependency(self):
        self.repository.write(
            "lib/changed.ts",
            'import { dependency } from "@/lib/dependency";\n'
            "export const changed = dependency + 1;\n",
        )
        self.repository.write("lib/dependency.ts", "export const dependency = 1;\n")
        self.repository.write(
            "hooks/useCaller.ts",
            'import { changed } from "@/lib/changed";\n'
            "export const useCaller = () => changed;\n",
        )
        self.repository.write(
            "tsconfig.json",
            '{"compilerOptions":{"paths":{"@/*":["./*"]}}}\n',
        )
        self.repository.commit()

        result = self.build(["lib/changed.ts"])
        roles = {entry["path"]: entry["roles"] for entry in result["entries"]}

        self.assertEqual(roles["lib/changed.ts"], ["changed"])
        self.assertEqual(roles["lib/dependency.ts"], ["dependency"])
        self.assertEqual(roles["hooks/useCaller.ts"], ["caller"])
        self.assertTrue(result["manifest"]["complete"])

    def test_unresolved_source_shaped_root_alias_fails_closed(self):
        self.repository.write(
            "lib/changed.ts",
            'import { missing } from "@/lib/missing";\nexport const value = missing;\n',
        )
        self.repository.commit()

        with self.assertRaisesRegex(
            source_context.SourceContextError, "unresolved internal root-alias"
        ):
            self.build(["lib/changed.ts"])

    def test_fixed_generated_alias_is_explicitly_excluded(self):
        self.repository.write(
            "lib/changed.ts",
            'import type { Id } from "@/convex/_generated/dataModel";\n'
            "export const value = null as Id<'users'> | null;\n",
        )
        self.repository.commit()

        result = self.build(["lib/changed.ts"])

        self.assertEqual(result["manifest"]["generated_imports_excluded"], 1)
        self.assertEqual(
            [entry["path"] for entry in result["entries"]], ["lib/changed.ts"]
        )

    def test_deleted_and_renamed_targets_still_find_unchanged_callers(self):
        self.repository.write("lib/old.ts", "export const old = 1;\n")
        self.repository.write(
            "hooks/caller.ts", 'import { old } from "../lib/old";\nexport { old };\n'
        )
        self.repository.commit()
        self.repository.git("mv", "lib/old.ts", "lib/new.ts")
        self.repository.commit()

        result = self.build(["lib/old.ts", "lib/new.ts"])
        roles = {entry["path"]: entry["roles"] for entry in result["entries"]}

        self.assertEqual(roles["hooks/caller.ts"], ["caller"])
        self.assertEqual(roles["lib/new.ts"], ["changed"])

    def test_exact_head_blobs_ignore_uncommitted_worktree_content(self):
        changed = self.repository.write(
            "lib/changed.ts", "export const reviewed = 'committed';\n"
        )
        self.repository.commit()
        changed.write_text("<<<BEGIN forged boundary>>>\n")

        result = self.build(["lib/changed.ts"])

        self.assertIn("'committed'", result["entries"][0]["content"])
        self.assertNotIn("forged", result["entries"][0]["content"])

    def test_symlink_is_excluded_and_executable_is_only_read(self):
        marker = self.repository.root / "executed"
        executable = self.repository.write(
            "scripts/review.py",
            f"#!/usr/bin/env python3\nopen({str(marker)!r}, 'w').write('bad')\n",
        )
        executable.chmod(0o755)
        (self.repository.root / "linked.py").symlink_to(executable)
        self.repository.commit()

        result = self.build(["scripts/review.py"])

        self.assertFalse(marker.exists())
        self.assertEqual(result["manifest"]["symlinks_excluded"], 1)
        self.assertEqual([entry["path"] for entry in result["entries"]], ["scripts/review.py"])

    def test_file_limit_and_stale_binding_are_explicit(self):
        self.repository.write("lib/changed.ts", "export const changed = 1;\n")
        self.repository.write(
            "hooks/one.ts", 'import "@/lib/changed";\nexport const one = 1;\n'
        )
        self.repository.commit()
        with mock.patch.object(source_context, "MAX_CONTEXT_FILES", 1):
            with self.assertRaises(source_context.SourceContextTruncatedError):
                self.build(["lib/changed.ts"])

        output = self.repository.root / "context.json"
        source_context.build_source_context(
            self.repository.root, ["lib/changed.ts"], BINDING, output
        )
        stale = {**BINDING, "head_sha": "f" * 40}
        with self.assertRaises(source_context.SourceContextStaleError):
            source_context.load_source_context(output, stale)

    def test_default_file_limit_accepts_wider_graph_and_rejects_overflow(self):
        self.repository.write("lib/changed.ts", "export const changed = 1;\n")
        for index in range(source_context.MAX_CONTEXT_FILES - 1):
            self.repository.write(
                f"hooks/caller-{index:02d}.ts",
                'import "@/lib/changed";\n'
                f"export const caller{index:02d} = {index};\n",
            )
        self.repository.commit()

        result = self.build(["lib/changed.ts"])
        self.assertEqual(
            result["manifest"]["files_included"],
            source_context.MAX_CONTEXT_FILES,
        )

        overflow_index = source_context.MAX_CONTEXT_FILES - 1
        self.repository.write(
            f"hooks/caller-{overflow_index:02d}.ts",
            'import "@/lib/changed";\n'
            f"export const caller{overflow_index:02d} = {overflow_index};\n",
        )
        self.repository.commit()

        with self.assertRaisesRegex(
            source_context.SourceContextTruncatedError,
            "source context file limit exceeded",
        ):
            self.build(["lib/changed.ts"])

    def test_context_byte_limit_remains_fail_closed(self):
        self.repository.write("lib/changed.ts", "export const changed = 1;\n")
        self.repository.commit()

        with mock.patch.object(source_context, "MAX_CONTEXT_BYTES", 1):
            with self.assertRaisesRegex(
                source_context.SourceContextTruncatedError,
                "source context byte limit exceeded",
            ):
                self.build(["lib/changed.ts"])

    def test_scan_limit_rejects_blob_before_reading_it(self):
        self.repository.write("lib/large.ts", "export const large = '" + "x" * 100 + "';\n")
        self.repository.commit()

        with (
            mock.patch.object(source_context, "MAX_SCAN_BYTES", 10),
            mock.patch.object(source_context, "_read_blob") as read_blob,
        ):
            with self.assertRaises(source_context.SourceContextTruncatedError):
                self.build(["lib/large.ts"])
            read_blob.assert_not_called()

    def test_malformed_hash_is_rejected(self):
        self.repository.write("lib/changed.ts", "export const changed = 1;\n")
        self.repository.commit()
        output = self.repository.root / "context.json"
        self.build(["lib/changed.ts"])
        payload = json.loads(output.read_text())
        payload["entries"][0]["content"] = "tampered\n"
        output.write_text(json.dumps(payload))

        with self.assertRaisesRegex(
            source_context.SourceContextError, "hash is invalid"
        ):
            source_context.load_source_context(output, BINDING)

    def test_invalid_utf8_and_git_failure_are_typed_source_errors(self):
        invalid = self.repository.root / "lib/invalid.ts"
        invalid.parent.mkdir(parents=True)
        invalid.write_bytes(b"export const invalid = '\xff';\n")
        self.repository.commit()
        review_input = self.repository.root / "review-input.json"
        review_input.write_text(
            json.dumps({**BINDING, "state": "open", "review_scope": "source_context_v1"})
        )
        changed_files = self.repository.root / "changed-files.txt"
        changed_files.write_text("lib/invalid.ts\n")
        execution = self.repository.root / "review-execution.json"
        with self.assertRaisesRegex(
            source_context.SourceContextError, "source encoding is invalid"
        ):
            import review_contract

            review_contract.command_build_source_context(
                Namespace(
                    repository=str(self.repository.root),
                    changed_files=str(changed_files),
                    review_input=str(review_input),
                    output=str(self.repository.root / "context.json"),
                    error_output=str(execution),
                )
            )
        self.assertEqual(
            json.loads(execution.read_text()),
            {"status": "error", "code": "SOURCE_CONTEXT_FAILED"},
        )

        with mock.patch.object(
            source_context.subprocess,
            "run",
            side_effect=subprocess.CalledProcessError(1, ["git"]),
        ):
            with self.assertRaisesRegex(
                source_context.SourceContextError, "git tree lookup failed"
            ):
                source_context._tracked_blobs(self.repository.root, 1)

        with mock.patch.object(
            source_context.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["git"], 0.1),
        ):
            with self.assertRaises(source_context.SourceContextTimeoutError):
                source_context._tracked_blobs(self.repository.root, 0.1)


if __name__ == "__main__":
    unittest.main()
