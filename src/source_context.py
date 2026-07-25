from __future__ import annotations

import hashlib
import json
import pathlib
import posixpath
import re
import subprocess
import time
from typing import Any

MAX_CONTEXT_BYTES = 500_000
MAX_FILE_BYTES = 100_000
MAX_CONTEXT_FILES = 50
MAX_SCAN_BYTES = 40_000_000
MAX_TRACKED_FILES = 10_000
LOOKUP_TIMEOUT_SECONDS = 20

SOURCE_EXTENSIONS = frozenset(
    {".cjs", ".js", ".jsx", ".json", ".mjs", ".py", ".sh", ".ts", ".tsx"}
)
EXCLUDED_SEGMENTS = frozenset(
    {".expo", "__generated__", "build", "dist", "node_modules"}
)
EXCLUDED_PREFIXES = ("convex/_generated/",)
IGNORED_ALIAS_PREFIXES = ("@/convex/_generated/",)
IMPORT_PATTERN = re.compile(
    r"""(?:\bfrom\s+|\bimport\s+|\bimport\s*\(|\brequire\s*\()\s*["'](?P<specifier>(?:\.|@/)[^"']+)["']"""
)
MANIFEST_KEYS = {
    "complete",
    "truncated",
    "pull_number",
    "head_sha",
    "base_ref",
    "base_sha",
    "files_scanned",
    "bytes_scanned",
    "files_included",
    "bytes_included",
    "sha256",
    "symlinks_excluded",
    "generated_files_excluded",
    "generated_imports_excluded",
}


class SourceContextError(ValueError):
    code = "SOURCE_CONTEXT_FAILED"


class SourceContextTruncatedError(SourceContextError):
    code = "SOURCE_CONTEXT_TRUNCATED"


class SourceContextTimeoutError(SourceContextError):
    code = "SOURCE_CONTEXT_TIMEOUT"


class SourceContextStaleError(SourceContextError):
    code = "SOURCE_CONTEXT_STALE"


def valid_path(path: str) -> bool:
    if (
        not path
        or path.startswith("/")
        or "\0" in path
        or "\n" in path
        or "\r" in path
    ):
        return False
    pure = pathlib.PurePosixPath(path)
    return ".." not in pure.parts and pure.as_posix() == path


def eligible_path(path: str) -> bool:
    if not valid_path(path):
        return False
    pure = pathlib.PurePosixPath(path)
    return (
        not any(part in EXCLUDED_SEGMENTS for part in pure.parts)
        and not any(path.startswith(prefix) for prefix in EXCLUDED_PREFIXES)
        and not path.endswith(".d.ts")
        and pure.suffix.lower() in SOURCE_EXTENSIONS
    )


def _tracked_blobs(
    repository: pathlib.Path, timeout_seconds: float
) -> tuple[dict[str, tuple[str, int]], int]:
    try:
        output = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "ls-tree",
                "-r",
                "-z",
                "-l",
                "--full-tree",
                "HEAD",
            ],
            check=True,
            capture_output=True,
            timeout=timeout_seconds,
        ).stdout
    except subprocess.TimeoutExpired as error:
        raise SourceContextTimeoutError("git tree lookup timed out") from error
    except (OSError, subprocess.SubprocessError) as error:
        raise SourceContextError("git tree lookup failed") from error
    blobs: dict[str, tuple[str, int]] = {}
    symlinks = 0
    for raw_entry in output.split(b"\0"):
        if not raw_entry:
            continue
        try:
            metadata, separator, raw_path = raw_entry.partition(b"\t")
            columns = metadata.split()
            if not separator or len(columns) != 4:
                raise SourceContextError("malformed git tree entry")
            mode, kind, object_id, size_text = (
                column.decode("ascii", errors="strict") for column in columns
            )
            path = raw_path.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise SourceContextError("git tree encoding is invalid") from error
        if not valid_path(path):
            raise SourceContextError("unsupported repository path")
        if mode == "120000":
            symlinks += 1
        elif kind == "blob" and mode in {"100644", "100755"}:
            if not size_text.isdigit():
                raise SourceContextError("git blob size is invalid")
            blobs[path] = (object_id, int(size_text))
    if len(blobs) > MAX_TRACKED_FILES:
        raise SourceContextTruncatedError("tracked file limit exceeded")
    return blobs, symlinks


def _read_blob(
    repository: pathlib.Path, object_id: str, timeout_seconds: float
) -> tuple[str, int]:
    try:
        content = subprocess.run(
            ["git", "-C", str(repository), "cat-file", "blob", object_id],
            check=True,
            capture_output=True,
            timeout=timeout_seconds,
        ).stdout
    except subprocess.TimeoutExpired as error:
        raise SourceContextTimeoutError("git blob lookup timed out") from error
    except (OSError, subprocess.SubprocessError) as error:
        raise SourceContextError("git blob lookup failed") from error
    if b"\0" in content:
        raise SourceContextError("binary source file is unsupported")
    try:
        return content.decode("utf-8", errors="strict"), len(content)
    except UnicodeDecodeError as error:
        raise SourceContextError("source encoding is invalid") from error


def _module_candidates(importer: str, specifier: str) -> list[str]:
    if "\0" in specifier:
        return []
    if specifier.startswith("@/"):
        joined = posixpath.normpath(specifier[2:])
    elif specifier.startswith("."):
        joined = posixpath.normpath(
            posixpath.join(posixpath.dirname(importer), specifier)
        )
    else:
        return []
    if joined == ".." or joined.startswith("../") or joined.startswith("/"):
        return []
    base = pathlib.PurePosixPath(joined)
    if base.suffix:
        return [base.as_posix()]
    variants = ("ios", "native", "")
    return [
        base.as_posix(),
        *(
            f"{base.as_posix()}.{variant + '.' if variant else ''}{extension[1:]}"
            for variant in variants
            for extension in sorted(SOURCE_EXTENSIONS)
        ),
        *(
            base.joinpath(
                f"index.{variant + '.' if variant else ''}{extension[1:]}"
            ).as_posix()
            for variant in variants
            for extension in sorted(SOURCE_EXTENSIONS)
        ),
    ]


def _resolved_imports(
    importer: str, content: str, resolvable_paths: set[str]
) -> tuple[set[str], int]:
    resolved: set[str] = set()
    generated_imports_excluded = 0
    for match in IMPORT_PATTERN.finditer(content):
        specifier = match.group("specifier")
        for candidate in _module_candidates(importer, specifier):
            if candidate in resolvable_paths:
                resolved.add(candidate)
                break
        else:
            suffix = pathlib.PurePosixPath(specifier).suffix.lower()
            if any(specifier.startswith(prefix) for prefix in IGNORED_ALIAS_PREFIXES):
                generated_imports_excluded += 1
            elif specifier.startswith("@/") and (
                not suffix or suffix in SOURCE_EXTENSIONS
            ):
                raise SourceContextError("unresolved internal root-alias import")
    return resolved, generated_imports_excluded


def _binding_tuple(value: dict[str, Any]) -> tuple[Any, ...]:
    return (
        value.get("pull_number"),
        value.get("head_sha"),
        value.get("base_ref"),
        value.get("base_sha"),
    )


def build_source_context(
    repository: pathlib.Path,
    changed_files: list[str],
    binding: dict[str, Any],
    output_path: pathlib.Path,
    *,
    timeout_seconds: int = LOOKUP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    started = time.monotonic()

    def remaining_time() -> float:
        remaining = timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise SourceContextTimeoutError("source lookup timed out")
        return remaining

    blobs, symlinks_excluded = _tracked_blobs(repository, remaining_time())
    changed_paths = set(changed_files)
    if any(not valid_path(path) for path in changed_paths):
        raise SourceContextError("changed path is unsupported")

    contents: dict[str, str] = {}
    bytes_scanned = 0
    generated_files_excluded = 0
    for path, (object_id, blob_size) in sorted(blobs.items()):
        remaining_time()
        if not eligible_path(path):
            pure = pathlib.PurePosixPath(path)
            if (
                any(part in EXCLUDED_SEGMENTS for part in pure.parts)
                or any(path.startswith(prefix) for prefix in EXCLUDED_PREFIXES)
                or path.endswith(".d.ts")
            ):
                generated_files_excluded += 1
            continue
        if bytes_scanned + blob_size > MAX_SCAN_BYTES:
            raise SourceContextTruncatedError("source scan byte limit exceeded")
        content, byte_count = _read_blob(
            repository, object_id, remaining_time()
        )
        if byte_count != blob_size:
            raise SourceContextError("git blob size mismatched")
        bytes_scanned += byte_count
        contents[path] = content

    targets = {path for path in changed_paths if eligible_path(path)}
    imports_by_file: dict[str, set[str]] = {}
    generated_imports_excluded = 0
    resolvable_paths = set(blobs) | targets
    for importer, content in sorted(contents.items()):
        remaining_time()
        imports, excluded_count = _resolved_imports(
            importer, content, resolvable_paths
        )
        imports_by_file[importer] = imports
        generated_imports_excluded += excluded_count

    roles: dict[str, set[str]] = {}
    for target in sorted(targets):
        if target in contents:
            roles.setdefault(target, set()).add("changed")
            for dependency in imports_by_file[target]:
                if dependency not in changed_paths and dependency in contents:
                    roles.setdefault(dependency, set()).add("dependency")
        for importer, imports in imports_by_file.items():
            if importer not in changed_paths and target in imports:
                roles.setdefault(importer, set()).add("caller")

    if len(roles) > MAX_CONTEXT_FILES:
        raise SourceContextTruncatedError("source context file limit exceeded")
    entries: list[dict[str, Any]] = []
    bytes_included = 0
    for path in sorted(roles):
        content = contents.get(path)
        if content is None:
            continue
        byte_count = len(content.encode("utf-8"))
        if byte_count > MAX_FILE_BYTES:
            raise SourceContextTruncatedError("selected source file is oversized")
        bytes_included += byte_count
        if bytes_included > MAX_CONTEXT_BYTES:
            raise SourceContextTruncatedError("source context byte limit exceeded")
        entries.append(
            {"path": path, "roles": sorted(roles[path]), "content": content}
        )

    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    manifest = {
        "complete": True,
        "truncated": False,
        "pull_number": binding["pull_number"],
        "head_sha": binding["head_sha"],
        "base_ref": binding["base_ref"],
        "base_sha": binding["base_sha"],
        "files_scanned": len(contents),
        "bytes_scanned": bytes_scanned,
        "files_included": len(entries),
        "bytes_included": bytes_included,
        "sha256": hashlib.sha256(canonical).hexdigest(),
        "symlinks_excluded": symlinks_excluded,
        "generated_files_excluded": generated_files_excluded,
        "generated_imports_excluded": generated_imports_excluded,
    }
    result = {"manifest": manifest, "entries": entries}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def load_source_context(
    path: pathlib.Path, binding: dict[str, Any]
) -> tuple[dict[str, Any], str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceContextError("source context is unreadable") from error
    if (
        not isinstance(data, dict)
        or set(data) != {"manifest", "entries"}
        or not isinstance(data.get("manifest"), dict)
        or set(data["manifest"]) != MANIFEST_KEYS
        or not isinstance(data.get("entries"), list)
    ):
        raise SourceContextError("source context shape is invalid")
    manifest = data["manifest"]
    if _binding_tuple(manifest) != _binding_tuple(binding):
        raise SourceContextStaleError("source context binding is stale")
    if manifest.get("complete") is not True or manifest.get("truncated") is not False:
        raise SourceContextTruncatedError("source context is incomplete")

    canonical = json.dumps(
        data["entries"], sort_keys=True, separators=(",", ":")
    ).encode()
    if (
        manifest.get("sha256") != hashlib.sha256(canonical).hexdigest()
        or manifest.get("files_included") != len(data["entries"])
    ):
        raise SourceContextError("source context hash is invalid")

    blocks = []
    content_bytes = 0
    for entry in data["entries"]:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"path", "roles", "content"}
            or not isinstance(entry["path"], str)
            or not valid_path(entry["path"])
            or not isinstance(entry["roles"], list)
            or not entry["roles"]
            or not all(
                role in {"caller", "changed", "dependency"} for role in entry["roles"]
            )
            or not isinstance(entry["content"], str)
        ):
            raise SourceContextError("source context entry is invalid")
        content = entry["content"]
        content_bytes += len(content.encode("utf-8"))
        separator = "" if content.endswith("\n") else "\n"
        roles = ",".join(sorted(set(entry["roles"])))
        blocks.append(
            f"<<<BEGIN UNTRUSTED EXACT-HEAD SOURCE path={entry['path']} roles={roles}>>>\n"
            f"{content}{separator}"
            f"<<<END UNTRUSTED EXACT-HEAD SOURCE path={entry['path']}>>>"
        )
    if content_bytes != manifest.get("bytes_included"):
        raise SourceContextError("source context byte count is invalid")
    rendered = "\n\n".join(blocks) or "(No eligible source relationships found.)"
    return manifest, rendered, path.read_text(encoding="utf-8", errors="strict")
