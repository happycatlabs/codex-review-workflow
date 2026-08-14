from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import urllib.parse
from typing import Any


COMMENT_MAP_VERSION = "codex-review-comment-map/v1"
MAX_INLINE_COMMENTS = 20
STALE_PUBLICATION_CODES = {
    "BASE_BRANCH_INVALID",
    "BASE_REF_DRIFT",
    "PR_STATE_INVALID",
    "STALE_BASE",
    "STALE_HEAD",
}
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
FINGERPRINT_PATTERN = re.compile(r"[0-9a-f]{64}")


def load_json(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8", errors="strict"))


def write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def safe_relative_path(value: str) -> str | None:
    if value != value.strip() or not value or "\\" in value:
        return None
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    if any(ord(character) < 32 for character in value):
        return None
    return path.as_posix()


def markdown_link_label(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    for character in ("[", "]", "(", ")", "`"):
        escaped = escaped.replace(character, f"\\{character}")
    return escaped


def machine_fingerprints(result: dict[str, Any]) -> tuple[str, ...]:
    values = result.get("finding_fingerprints", [])
    findings = result.get("findings", [])
    candidates = list(values) if isinstance(values, list) else []
    if isinstance(findings, list):
        candidates.extend(
            finding.get("fingerprint")
            for finding in findings
            if isinstance(finding, dict)
        )
    return tuple(
        dict.fromkeys(
            value
            for value in candidates
            if isinstance(value, str) and FINGERPRINT_PATTERN.fullmatch(value)
        )
    )


def public_prose(value: str, fingerprints: tuple[str, ...]) -> str:
    prose = value
    for fingerprint in fingerprints:
        prose = re.sub(
            re.escape(fingerprint),
            "[machine-only finding identifier omitted]",
            prose,
            flags=re.IGNORECASE,
        )
    prose = " ".join(prose.split())
    return prose.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def parse_commentable_lines(diff: str) -> tuple[dict[str, list[list[int]]], bool]:
    files: dict[str, list[list[int]]] = {}
    current_path: str | None = None
    in_hunks = False
    complete = True
    hunk_pattern = re.compile(
        r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@"
    )

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current_path = None
            in_hunks = False
            continue
        if line.startswith("+++ ") and not in_hunks:
            raw_path = line[4:]
            if raw_path == "/dev/null":
                current_path = ""
                continue
            if not raw_path.startswith("b/"):
                current_path = None
                complete = False
                continue
            current_path = safe_relative_path(raw_path[2:])
            if current_path is None:
                complete = False
            continue
        if not line.startswith("@@ "):
            continue
        in_hunks = True
        if current_path == "":
            continue
        match = hunk_pattern.match(line)
        if match is None or current_path is None:
            complete = False
            continue
        start = int(match.group("start"))
        count = int(match.group("count") or "1")
        if count == 0:
            continue
        files.setdefault(current_path, []).append([start, start + count - 1])

    return files, complete


def build_comment_map(
    repository: pathlib.Path, review_input: dict[str, Any]
) -> dict[str, Any]:
    actual_head = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if actual_head != review_input["head_sha"]:
        raise ValueError("comment map checkout does not match the reviewed head")

    diff_bytes = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "core.quotePath=false",
            "--no-pager",
            "diff",
            "--unified=0",
            "--output-indicator-new=>",
            "--output-indicator-old=<",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            review_input["base_sha"],
            review_input["head_sha"],
        ],
        check=True,
        capture_output=True,
    ).stdout
    try:
        diff = diff_bytes.decode("utf-8", errors="strict")
        files, complete = parse_commentable_lines(diff)
        changed_output = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "diff",
                "--name-only",
                "-z",
                "--no-renames",
                "--no-ext-diff",
                "--no-textconv",
                review_input["base_sha"],
                review_input["head_sha"],
            ],
            check=True,
            capture_output=True,
        ).stdout
        changed_files = {
            path.decode("utf-8", errors="strict")
            for path in changed_output.split(b"\0")
            if path
        }
        if not set(files).issubset(changed_files):
            files = {}
            complete = False
    except UnicodeDecodeError:
        files = {}
        complete = False

    return {
        "schema_version": COMMENT_MAP_VERSION,
        "complete": complete,
        "pull_number": review_input["pull_number"],
        "head_sha": review_input["head_sha"],
        "base_ref": review_input["base_ref"],
        "base_sha": review_input["base_sha"],
        "diff_sha256": hashlib.sha256(diff_bytes).hexdigest(),
        "files": files,
    }


def finding_location(finding: dict[str, Any]) -> str:
    start_line = finding["start_line"]
    end_line = finding["line"]
    line_label = str(end_line) if start_line == end_line else f"{start_line}-{end_line}"
    return f"{finding['file']}:{line_label}"


def immutable_code_link(
    repository: str, head_sha: str, finding: dict[str, Any]
) -> str | None:
    path = safe_relative_path(finding["file"])
    if (
        REPOSITORY_PATTERN.fullmatch(repository) is None
        or SHA_PATTERN.fullmatch(head_sha) is None
        or path is None
    ):
        return None
    start_line = finding["start_line"]
    end_line = finding["line"]
    fragment = f"#L{start_line}" if start_line == end_line else f"#L{start_line}-L{end_line}"
    encoded_path = urllib.parse.quote(path, safe="/")
    label = markdown_link_label(finding_location({**finding, "file": path}))
    return (
        f"[{label}](https://github.com/{repository}/blob/"
        f"{head_sha}/{encoded_path}{fragment})"
    )


def immutable_head_link(repository: str, head_sha: str) -> str | None:
    if (
        REPOSITORY_PATTERN.fullmatch(repository) is None
        or SHA_PATTERN.fullmatch(head_sha) is None
    ):
        return None
    return f"[`{head_sha}`](https://github.com/{repository}/commit/{head_sha})"


def actions_run_link(repository: str, run_id: int) -> str:
    if REPOSITORY_PATTERN.fullmatch(repository) is None or run_id < 1:
        raise ValueError("invalid Actions run identity")
    return f"https://github.com/{repository}/actions/runs/{run_id}"


def finding_action() -> str:
    return "Action: Correct the described failure before merging."


def finding_body(
    finding: dict[str, Any],
    repository: str,
    head_sha: str,
    fingerprints: tuple[str, ...],
) -> str:
    location = immutable_code_link(repository, head_sha, finding)
    title = public_prose(finding["title"], fingerprints)
    body = public_prose(finding["body"], fingerprints)
    lines = [f"**{finding['severity']} — {title}**"]
    if location is not None:
        lines.extend(["", location])
    lines.extend(
        ["", f"**Impact and trigger:** {body}", "", finding_action()]
    )
    return "\n".join(lines)


def alert_lines(result: dict[str, Any]) -> list[str]:
    error = result.get("error")
    findings = result.get("findings", [])
    coverage = result.get("coverage")
    lookup = result.get("lookup_context")
    complete = (
        isinstance(coverage, dict)
        and coverage.get("complete") is True
        and coverage.get("truncated") is False
        and isinstance(lookup, dict)
        and lookup.get("complete") is True
    )
    if (
        result.get("verdict") == "clean"
        and error is None
        and not findings
        and complete
    ):
        return [
            "> [!NOTE]",
            "> No concrete issues were found in the complete bounded review packet.",
        ]
    if isinstance(error, dict) and error.get("code") == "TICKET_CONTEXT_MISSING":
        message = (
            f"Review skipped (`{error['code']}`) because trusted task context was unavailable. "
            "Automatic approval remains disabled."
        )
    elif isinstance(error, dict):
        preserved = (
            f" {len(findings)} concrete finding(s) were preserved."
            if findings
            else ""
        )
        message = (
            f"Review incomplete because `{error['code']}` stopped the trusted workflow."
            f"{preserved}"
        )
    else:
        count = len(findings)
        message = (
            f"{count} actionable finding{'s' if count != 1 else ''} require attention. "
            "This COMMENT review does not approve or request changes."
        )
    return ["> [!CAUTION]", f"> {message}"]


def coverage_summary(result: dict[str, Any]) -> str:
    coverage = result.get("coverage")
    if not isinstance(coverage, dict):
        return "coverage unavailable"
    complete = coverage.get("complete") is True and coverage.get("truncated") is False
    status = "complete" if complete else "incomplete"
    diff_bytes = coverage.get("diff_bytes_included")
    source_bytes = coverage.get("source_context_bytes")
    if type(diff_bytes) is int and type(source_bytes) is int:
        return f"{status}; {diff_bytes} diff bytes and {source_bytes} source-context bytes"
    return status


def review_body(
    result: dict[str, Any],
    *,
    fallback_reason: str | None,
    include_findings: bool,
    repository: str,
    run_id: int,
) -> str:
    lines = ["## Codex review", "", *alert_lines(result)]
    fingerprints = machine_fingerprints(result)
    lines.extend(
        [
            "",
            "### Production impact",
            "",
            public_prose(result["summary"], fingerprints),
        ]
    )

    findings = result.get("findings", [])
    if include_findings and findings:
        lines.extend(["", "#### Findings"])
        for index, finding in enumerate(findings, 1):
            rendered = finding_body(
                finding,
                repository,
                result.get("head_sha", ""),
                fingerprints,
            )
            lines.extend(["", f"{index}. {rendered}"])

    lines.extend(["", "### Evidence", ""])
    head = immutable_head_link(repository, result.get("head_sha", ""))
    lines.append(f"- Reviewed head: {head or 'unavailable before exact-snapshot binding'}")
    lines.append(
        f"- Scope: `{result.get('review_scope', 'unknown')}`; {coverage_summary(result)}."
    )
    run_url = actions_run_link(repository, run_id)
    lines.append(
        f"- Full result: [Actions run and `codex-review-result` artifact]({run_url})."
    )
    if fallback_reason:
        lines.append(
            f"- Publication: complete summary fallback (`{fallback_reason}`); "
            "no finding was omitted."
        )
    return "\n".join(lines).strip()


def complete_review_body(
    result: dict[str, Any],
    fallback_reason: str | None,
    repository: str,
    run_id: int,
) -> str:
    return review_body(
        result,
        fallback_reason=fallback_reason,
        include_findings=True,
        repository=repository,
        run_id=run_id,
    )


def inline_review_body(
    result: dict[str, Any], repository: str, run_id: int
) -> str:
    return review_body(
        result,
        fallback_reason=None,
        include_findings=False,
        repository=repository,
        run_id=run_id,
    )


def comment_body(
    finding: dict[str, Any],
    repository: str,
    head_sha: str,
    fingerprints: tuple[str, ...],
) -> str:
    return finding_body(finding, repository, head_sha, fingerprints)


def comment_map_matches(
    comment_map: Any, result: dict[str, Any]
) -> bool:
    if not isinstance(comment_map, dict):
        return False
    if (
        comment_map.get("schema_version") != COMMENT_MAP_VERSION
        or comment_map.get("complete") is not True
        or not isinstance(comment_map.get("files"), dict)
    ):
        return False
    for key in ("pull_number", "head_sha", "base_ref", "base_sha"):
        if comment_map.get(key) != result.get(key):
            return False
    return True


def range_is_commentable(
    intervals: Any, start_line: int, end_line: int
) -> bool:
    if not isinstance(intervals, list):
        return False
    return any(
        isinstance(interval, list)
        and len(interval) == 2
        and all(type(value) is int for value in interval)
        and interval[0] <= start_line <= end_line <= interval[1]
        for interval in intervals
    )


def plan_publication(
    result: dict[str, Any],
    comment_map: Any,
    *,
    repository: str,
    run_id: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise ValueError("invalid repository identity")
    if run_id < 1:
        raise ValueError("invalid Actions run identity")
    findings = result.get("findings", [])
    fingerprints = machine_fingerprints(result)
    fallback_reason: str | None = None
    comments: list[dict[str, Any]] = []

    error = result.get("error")
    error_code = error.get("code") if isinstance(error, dict) else None
    if findings and error_code:
        fallback_reason = str(error_code)
    elif len(findings) > MAX_INLINE_COMMENTS:
        fallback_reason = "INLINE_COMMENT_LIMIT_EXCEEDED"
    elif findings and not comment_map_matches(comment_map, result):
        fallback_reason = "COMMENT_MAP_INVALID"
    elif findings:
        files = comment_map["files"]
        for finding in findings:
            path = safe_relative_path(finding["file"])
            start_line = finding["start_line"]
            end_line = finding["line"]
            if path is None or not range_is_commentable(
                files.get(path), start_line, end_line
            ):
                fallback_reason = "INVALID_LOCATION"
                comments = []
                break
            comment = {
                "path": path,
                "line": end_line,
                "side": "RIGHT",
                "body": comment_body(
                    finding,
                    repository,
                    result["head_sha"],
                    fingerprints,
                ),
            }
            if start_line != end_line:
                comment["start_line"] = start_line
                comment["start_side"] = "RIGHT"
            comments.append(comment)

    inline = bool(findings) and fallback_reason is None
    summary_request = {
        "body": complete_review_body(result, fallback_reason, repository, run_id),
        "event": "COMMENT",
    }
    if error_code not in STALE_PUBLICATION_CODES:
        summary_request["commit_id"] = result["head_sha"]

    request = (
        {
            "commit_id": result["head_sha"],
            "body": inline_review_body(result, repository, run_id),
            "event": "COMMENT",
            "comments": comments,
        }
        if inline
        else summary_request
    )
    result["publication"] = {
        "status": "pending",
        "mode": "inline" if inline else "summary",
        "fallback_reason": fallback_reason,
        "inline_comment_count": len(comments),
    }
    return result, request, summary_request


def record_publication(
    result: dict[str, Any],
    *,
    status: str,
    mode: str,
    fallback_reason: str | None,
    inline_comment_count: int,
) -> dict[str, Any]:
    if status not in {"published", "failed"}:
        raise ValueError("invalid publication status")
    if mode not in {"inline", "summary"}:
        raise ValueError("invalid publication mode")
    if type(inline_comment_count) is not int or inline_comment_count < 0:
        raise ValueError("invalid inline comment count")
    result["publication"] = {
        "status": status,
        "mode": mode,
        "fallback_reason": fallback_reason,
        "inline_comment_count": inline_comment_count,
    }
    publication_invalidates_review = (
        status == "failed" or fallback_reason == "STALE_BEFORE_PUBLICATION"
    )
    if publication_invalidates_review and result.get("verdict") != "error":
        result["verdict"] = "error"
        result["error"] = {
            "code": (
                "STALE_BEFORE_PUBLICATION"
                if fallback_reason == "STALE_BEFORE_PUBLICATION"
                else "PUBLICATION_FAILED"
            ),
            "reason": (
                "The pull request generation changed immediately before publication."
                if fallback_reason == "STALE_BEFORE_PUBLICATION"
                else "The exact-head review result could not be published."
            ),
        }
    return result


def command_build_comment_map(args: argparse.Namespace) -> None:
    review_input = load_json(pathlib.Path(args.review_input))
    write_json(
        pathlib.Path(args.output),
        build_comment_map(pathlib.Path(args.repository), review_input),
    )


def command_plan(args: argparse.Namespace) -> None:
    result_path = pathlib.Path(args.result)
    result = load_json(result_path)
    try:
        comment_map = load_json(pathlib.Path(args.comment_map))
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError):
        comment_map = {}
    updated, request, summary_request = plan_publication(
        result,
        comment_map,
        repository=args.repository,
        run_id=args.run_id,
    )
    write_json(pathlib.Path(args.output), updated)
    write_json(pathlib.Path(args.request_output), request)
    write_json(pathlib.Path(args.summary_request_output), summary_request)


def command_record(args: argparse.Namespace) -> None:
    result = load_json(pathlib.Path(args.result))
    fallback_reason = args.fallback_reason or None
    updated = record_publication(
        result,
        status=args.status,
        mode=args.mode,
        fallback_reason=fallback_reason,
        inline_comment_count=args.inline_comment_count,
    )
    write_json(pathlib.Path(args.output), updated)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(required=True)

    comment_map = commands.add_parser("build-comment-map")
    comment_map.add_argument("--repository", required=True)
    comment_map.add_argument("--review-input", required=True)
    comment_map.add_argument("--output", required=True)
    comment_map.set_defaults(handler=command_build_comment_map)

    plan = commands.add_parser("plan")
    plan.add_argument("--result", required=True)
    plan.add_argument("--comment-map", required=True)
    plan.add_argument("--output", required=True)
    plan.add_argument("--request-output", required=True)
    plan.add_argument("--summary-request-output", required=True)
    plan.add_argument("--repository", required=True)
    plan.add_argument("--run-id", required=True, type=int)
    plan.set_defaults(handler=command_plan)

    record = commands.add_parser("record")
    record.add_argument("--result", required=True)
    record.add_argument("--status", required=True)
    record.add_argument("--mode", required=True)
    record.add_argument("--fallback-reason", default="")
    record.add_argument("--inline-comment-count", required=True, type=int)
    record.add_argument("--output", required=True)
    record.set_defaults(handler=command_record)
    return root


if __name__ == "__main__":
    arguments = parser().parse_args()
    arguments.handler(arguments)
