from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
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


def complete_review_body(
    result: dict[str, Any], fallback_reason: str | None
) -> str:
    lines = [result["summary"].strip()]
    error = result.get("error")
    if isinstance(error, dict):
        lines.extend(
            [
                "",
                (
                    f"Review infrastructure error: `{error['code']}` — "
                    f"{error['reason']}"
                ),
            ]
        )
    if fallback_reason:
        lines.extend(
            [
                "",
                f"Inline publication fallback: `{fallback_reason}`.",
            ]
        )
    findings = result.get("findings", [])
    if findings:
        lines.extend(["", "### Findings"])
        for index, finding in enumerate(findings, 1):
            lines.extend(
                [
                    "",
                    (
                        f"{index}. **{finding['severity']}: {finding['title']}** "
                        f"(`{finding_location(finding)}`)"
                    ),
                    "",
                    finding["body"].strip(),
                    "",
                    f"Fingerprint: `{finding['fingerprint']}`",
                ]
            )
    return "\n".join(lines).strip()


def inline_review_body(result: dict[str, Any]) -> str:
    finding_count = len(result["findings"])
    return (
        f"{result['summary'].strip()}\n\n"
        f"{finding_count} finding{'s' if finding_count != 1 else ''} "
        "published as resolvable inline comments."
    )


def comment_body(finding: dict[str, Any]) -> str:
    return (
        f"**{finding['severity']}: {finding['title']}**\n\n"
        f"{finding['body'].strip()}\n\n"
        f"Fingerprint: `{finding['fingerprint']}`"
    )


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
    result: dict[str, Any], comment_map: Any
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    findings = result.get("findings", [])
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
                "body": comment_body(finding),
            }
            if start_line != end_line:
                comment["start_line"] = start_line
                comment["start_side"] = "RIGHT"
            comments.append(comment)

    inline = bool(findings) and fallback_reason is None
    summary_request = {
        "body": complete_review_body(result, fallback_reason),
        "event": "COMMENT",
    }
    if error_code not in STALE_PUBLICATION_CODES:
        summary_request["commit_id"] = result["head_sha"]

    request = (
        {
            "commit_id": result["head_sha"],
            "body": inline_review_body(result),
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
    updated, request, summary_request = plan_publication(result, comment_map)
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
