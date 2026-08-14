from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import pathlib
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

import review_publication


API_ROOT = "https://api.github.com"
EXPECTED_DANCER_LOGIN = "dancer-automation[bot]"
EXPECTED_DANCER_ACTOR_ID = 266699010
MAX_REVIEW_PAGES = 10
MAX_COMMENT_PAGES = 2
PAGE_SIZE = 100
REQUEST_TIMEOUT_SECONDS = 30
PUBLICATION_MARKER = "codex-review-publication/v1"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


class PublicationFailure(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class GitHubApiError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"GitHub API returned HTTP {status}")
        self.status = status
        self.body = body


class InlineReviewRejected(Exception):
    pass


@dataclass(frozen=True)
class PublishedReview:
    review_id: int
    url: str
    commit_id: str
    request_sha256: str
    reused: bool


class GitHubClient:
    def __init__(self, token: str):
        if not token:
            raise PublicationFailure("DANCER_AUTH_UNAVAILABLE")
        self.token = token

    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{API_ROOT}{path}",
            data=body,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "happycatlabs-codex-review-workflow",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=REQUEST_TIMEOUT_SECONDS
            ) as response:
                raw = response.read().decode("utf-8", errors="strict")
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")
            raise GitHubApiError(error.code, raw) from error
        except (OSError, UnicodeDecodeError) as error:
            raise GitHubApiError(0, "transport failure") from error
        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise GitHubApiError(0, "invalid JSON response") from error


def load_json(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8", errors="strict"))


def write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def repository_parts(repository: str) -> tuple[str, str]:
    if review_publication.REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise PublicationFailure("PUBLICATION_IDENTITY_INVALID")
    owner, name = repository.split("/", 1)
    return owner, name


def validate_review_request(
    request: Any,
    *,
    expected_head: Any,
    allow_comments: bool,
    require_comments: bool,
) -> None:
    if not isinstance(request, dict):
        raise PublicationFailure("PUBLICATION_REQUEST_INVALID")
    allowed_keys = {"body", "event", "commit_id"}
    if allow_comments:
        allowed_keys.add("comments")
    if (
        not {"body", "event"}.issubset(request)
        or not set(request).issubset(allowed_keys)
        or not isinstance(request["body"], str)
        or not request["body"].strip()
        or request["event"] != "COMMENT"
    ):
        raise PublicationFailure("PUBLICATION_REQUEST_INVALID")
    commit_id = request.get("commit_id")
    if commit_id is not None and (
        commit_id != expected_head
        or not isinstance(commit_id, str)
        or SHA_PATTERN.fullmatch(commit_id) is None
    ):
        raise PublicationFailure("PUBLICATION_REQUEST_INVALID")

    comments = request.get("comments")
    if require_comments and (
        not isinstance(comments, list)
        or not comments
        or len(comments) > review_publication.MAX_INLINE_COMMENTS
    ):
        raise PublicationFailure("PUBLICATION_REQUEST_INVALID")
    if comments is None:
        return
    if not allow_comments or not isinstance(comments, list):
        raise PublicationFailure("PUBLICATION_REQUEST_INVALID")
    for comment in comments:
        if not isinstance(comment, dict) or not set(comment).issubset(
            {"path", "line", "side", "start_line", "start_side", "body"}
        ):
            raise PublicationFailure("PUBLICATION_REQUEST_INVALID")
        if not {"path", "line", "side", "body"}.issubset(comment):
            raise PublicationFailure("PUBLICATION_REQUEST_INVALID")
        path = comment.get("path")
        line = comment.get("line")
        start_line = comment.get("start_line")
        start_side = comment.get("start_side")
        if (
            not isinstance(path, str)
            or review_publication.safe_relative_path(path) != path
            or type(line) is not int
            or line < 1
            or comment.get("side") != "RIGHT"
            or not isinstance(comment.get("body"), str)
            or not comment["body"].strip()
        ):
            raise PublicationFailure("PUBLICATION_REQUEST_INVALID")
        if (start_line is None) != (start_side is None):
            raise PublicationFailure("PUBLICATION_REQUEST_INVALID")
        if start_line is not None and (
            type(start_line) is not int
            or start_line < 1
            or start_line > line
            or start_side != "RIGHT"
        ):
            raise PublicationFailure("PUBLICATION_REQUEST_INVALID")


def require_dancer_actor(client: GitHubClient) -> dict[str, Any]:
    response = client.request(
        "POST",
        "/graphql",
        {"query": "query CodexReviewPublisher { viewer { login databaseId } }"},
    )
    data = response.get("data") if isinstance(response, dict) else None
    viewer = data.get("viewer") if isinstance(data, dict) else None
    if (
        not isinstance(viewer, dict)
        or viewer.get("login") != EXPECTED_DANCER_LOGIN
        or viewer.get("databaseId") != EXPECTED_DANCER_ACTOR_ID
    ):
        raise PublicationFailure("DANCER_ACTOR_MISMATCH")
    return {"login": EXPECTED_DANCER_LOGIN, "id": EXPECTED_DANCER_ACTOR_ID}


def current_generation(
    client: GitHubClient, repository: str, pull_number: int
) -> dict[str, Any]:
    owner, name = repository_parts(repository)
    repository_data = client.request("GET", f"/repos/{owner}/{name}")
    pull = client.request("GET", f"/repos/{owner}/{name}/pulls/{pull_number}")
    if not isinstance(repository_data, dict) or not isinstance(pull, dict):
        raise PublicationFailure("PUBLICATION_STATE_LOOKUP_FAILED")
    default_branch = repository_data.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        raise PublicationFailure("PUBLICATION_STATE_LOOKUP_FAILED")
    encoded_branch = urllib.parse.quote(default_branch, safe="")
    commit = client.request("GET", f"/repos/{owner}/{name}/commits/{encoded_branch}")
    if not isinstance(commit, dict):
        raise PublicationFailure("PUBLICATION_STATE_LOOKUP_FAILED")
    base = pull.get("base")
    head = pull.get("head")
    observed = {
        "state": pull.get("state"),
        "head_sha": head.get("sha") if isinstance(head, dict) else None,
        "base_ref": base.get("ref") if isinstance(base, dict) else None,
        "base_sha": base.get("sha") if isinstance(base, dict) else None,
        "default_branch": default_branch,
        "default_branch_sha": commit.get("sha"),
    }
    if (
        observed["state"] not in {"open", "closed"}
        or not all(
            isinstance(observed[key], str)
            for key in (
                "head_sha",
                "base_ref",
                "base_sha",
                "default_branch_sha",
            )
        )
        or SHA_PATTERN.fullmatch(observed["head_sha"]) is None
        or SHA_PATTERN.fullmatch(observed["base_sha"]) is None
        or SHA_PATTERN.fullmatch(observed["default_branch_sha"]) is None
    ):
        raise PublicationFailure("PUBLICATION_STATE_LOOKUP_FAILED")
    return observed


def generation_is_current(result: dict[str, Any], observed: dict[str, Any]) -> bool:
    return (
        observed["state"] == "open"
        and observed["head_sha"] == result.get("head_sha")
        and observed["base_ref"] == result.get("base_ref")
        and observed["base_sha"] == result.get("base_sha")
        and observed["default_branch"] == result.get("base_ref")
        and observed["default_branch_sha"] == result.get("base_sha")
    )


def observed_state_is_safe_for_stale_summary(observed: dict[str, Any]) -> bool:
    return (
        observed["state"] == "open"
        and observed["base_ref"] == observed["default_branch"]
        and observed["base_sha"] == observed["default_branch_sha"]
    )


def canonical_request_sha256(request: dict[str, Any]) -> str:
    encoded = json.dumps(
        request, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def marked_request(request: dict[str, Any]) -> tuple[dict[str, Any], str]:
    prepared = copy.deepcopy(request)
    digest = canonical_request_sha256(prepared)
    prepared["body"] = (
        f"{prepared['body'].rstrip()}\n\n"
        f"<!-- {PUBLICATION_MARKER} request-sha256={digest} -->"
    )
    return prepared, digest


def summary_fallback_request(
    request: dict[str, Any], reason: str, *, omit_commit: bool
) -> dict[str, Any]:
    prepared = copy.deepcopy(request)
    fallback_line = (
        f"- Publication: complete summary fallback (`{reason}`); "
        "no finding was omitted."
    )
    body_lines = prepared["body"].rstrip().splitlines()
    replaced = False
    for index, line in enumerate(body_lines):
        if line.startswith("- Publication: complete summary fallback ("):
            body_lines[index] = fallback_line
            replaced = True
    if not replaced:
        body_lines.append(fallback_line)
    prepared["body"] = "\n".join(body_lines)
    if omit_commit:
        prepared.pop("commit_id", None)
    return prepared


def stale_summary_request(
    result: dict[str, Any], repository: str, run_id: int
) -> dict[str, Any]:
    rendered_result = copy.deepcopy(result)
    rendered_result["verdict"] = "error"
    rendered_result["error"] = {
        "code": "STALE_BEFORE_PUBLICATION",
        "reason": "The pull request generation changed before publication.",
    }
    if not rendered_result.get("findings"):
        rendered_result["summary"] = (
            "The reviewed pull request generation changed before publication; "
            "no current clean conclusion is available."
        )
    return {
        "body": review_publication.complete_review_body(
            rendered_result,
            "STALE_BEFORE_PUBLICATION",
            repository,
            run_id,
        ),
        "event": "COMMENT",
    }


def paginated_list(
    client: GitHubClient, path: str, *, max_pages: int
) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    separator = "&" if "?" in path else "?"
    for page in range(1, max_pages + 2):
        response = client.request(
            "GET", f"{path}{separator}per_page={PAGE_SIZE}&page={page}"
        )
        if not isinstance(response, list) or any(
            not isinstance(item, dict) for item in response
        ):
            raise PublicationFailure("PUBLICATION_EVIDENCE_INVALID")
        if page > max_pages and response:
            raise PublicationFailure("PUBLICATION_EVIDENCE_LIMIT_EXCEEDED")
        collected.extend(response)
        if len(response) < PAGE_SIZE:
            return collected
    raise PublicationFailure("PUBLICATION_EVIDENCE_LIMIT_EXCEEDED")


def actor_matches(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("login") == EXPECTED_DANCER_LOGIN
        and value.get("id") == EXPECTED_DANCER_ACTOR_ID
    )


def find_existing_review(
    client: GitHubClient,
    repository: str,
    pull_number: int,
    marker: str,
) -> dict[str, Any] | None:
    owner, name = repository_parts(repository)
    reviews = paginated_list(
        client,
        f"/repos/{owner}/{name}/pulls/{pull_number}/reviews",
        max_pages=MAX_REVIEW_PAGES,
    )
    matches = [
        review
        for review in reviews
        if isinstance(review.get("body"), str) and marker in review["body"]
    ]
    if len(matches) > 1:
        raise PublicationFailure("PUBLICATION_EVIDENCE_INVALID")
    if not matches:
        return None
    if not actor_matches(matches[0].get("user")):
        raise PublicationFailure("DANCER_ACTOR_MISMATCH")
    return matches[0]


def expected_comment(comment: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": comment.get("path"),
        "line": comment.get("line"),
        "side": comment.get("side"),
        "start_line": comment.get("start_line"),
        "start_side": comment.get("start_side"),
        "body": comment.get("body"),
    }


def actual_comment(comment: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": comment.get("path"),
        "line": comment.get("line"),
        "side": comment.get("side"),
        "start_line": comment.get("start_line"),
        "start_side": comment.get("start_side"),
        "body": comment.get("body"),
    }


def validate_review_readback(
    client: GitHubClient,
    repository: str,
    pull_number: int,
    request: dict[str, Any],
    request_sha256: str,
    review_id: int,
    observed_head: str,
    *,
    reused: bool,
) -> PublishedReview:
    owner, name = repository_parts(repository)
    review = client.request(
        "GET", f"/repos/{owner}/{name}/pulls/{pull_number}/reviews/{review_id}"
    )
    if not isinstance(review, dict):
        raise PublicationFailure("PUBLICATION_READBACK_FAILED")
    expected_commit = request.get("commit_id", observed_head)
    if (
        review.get("id") != review_id
        or review.get("state") != "COMMENTED"
        or review.get("body") != request.get("body")
        or review.get("commit_id") != expected_commit
    ):
        raise PublicationFailure("PUBLICATION_READBACK_FAILED")
    if not actor_matches(review.get("user")):
        raise PublicationFailure("DANCER_ACTOR_MISMATCH")

    comments = paginated_list(
        client,
        f"/repos/{owner}/{name}/pulls/{pull_number}/reviews/{review_id}/comments",
        max_pages=MAX_COMMENT_PAGES,
    )
    expected_comments = request.get("comments", [])
    if not isinstance(expected_comments, list) or len(comments) != len(
        expected_comments
    ):
        raise PublicationFailure("PUBLICATION_READBACK_FAILED")
    for expected, actual in zip(expected_comments, comments):
        if (
            actual.get("pull_request_review_id") != review_id
            or actual_comment(actual) != expected_comment(expected)
        ):
            raise PublicationFailure("PUBLICATION_READBACK_FAILED")
        if not actor_matches(actual.get("user")):
            raise PublicationFailure("DANCER_ACTOR_MISMATCH")

    url = review.get("html_url")
    commit_id = review.get("commit_id")
    expected_url = (
        f"https://github.com/{repository}/pull/{pull_number}"
        f"#pullrequestreview-{review_id}"
    )
    if url != expected_url:
        raise PublicationFailure("PUBLICATION_READBACK_FAILED")
    if not isinstance(commit_id, str) or SHA_PATTERN.fullmatch(commit_id) is None:
        raise PublicationFailure("PUBLICATION_READBACK_FAILED")
    return PublishedReview(
        review_id=review_id,
        url=url,
        commit_id=commit_id,
        request_sha256=request_sha256,
        reused=reused,
    )


def publish_one_review(
    client: GitHubClient,
    repository: str,
    pull_number: int,
    request: dict[str, Any],
    observed_head: str,
    pre_mutation_check: Callable[[], None],
) -> PublishedReview:
    owner, name = repository_parts(repository)
    prepared, digest = marked_request(request)
    marker = f"<!-- {PUBLICATION_MARKER} request-sha256={digest} -->"
    existing = find_existing_review(client, repository, pull_number, marker)
    if existing is not None:
        review_id = existing.get("id")
        if type(review_id) is not int or review_id < 1:
            raise PublicationFailure("PUBLICATION_EVIDENCE_INVALID")
        published = validate_review_readback(
            client,
            repository,
            pull_number,
            prepared,
            digest,
            review_id,
            observed_head,
            reused=True,
        )
        pre_mutation_check()
        return published

    pre_mutation_check()
    try:
        response = client.request(
            "POST", f"/repos/{owner}/{name}/pulls/{pull_number}/reviews", prepared
        )
    except GitHubApiError as error:
        if error.status == 422:
            raise InlineReviewRejected from error
        recovered = find_existing_review(client, repository, pull_number, marker)
        if recovered is None:
            raise PublicationFailure("PUBLICATION_MUTATION_AMBIGUOUS") from error
        review_id = recovered.get("id")
        if type(review_id) is not int or review_id < 1:
            raise PublicationFailure("PUBLICATION_EVIDENCE_INVALID") from error
        return validate_review_readback(
            client,
            repository,
            pull_number,
            prepared,
            digest,
            review_id,
            observed_head,
            reused=True,
        )
    review_id = response.get("id") if isinstance(response, dict) else None
    if type(review_id) is not int or review_id < 1:
        raise PublicationFailure("PUBLICATION_READBACK_FAILED")
    return validate_review_readback(
        client,
        repository,
        pull_number,
        prepared,
        digest,
        review_id,
        observed_head,
        reused=False,
    )


def expected_generation(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "head_sha": result.get("head_sha"),
        "base_ref": result.get("base_ref"),
        "base_sha": result.get("base_sha"),
    }


def receipt(
    *,
    status: str,
    repository: str,
    pull_number: int,
    run_id: int,
    result: dict[str, Any],
    observed: dict[str, Any] | None,
    actor: dict[str, Any] | None,
    mode: str,
    fallback_reason: str | None,
    published: PublishedReview | None,
) -> dict[str, Any]:
    return {
        "schema_version": "codex-review-publication/v1",
        "status": status,
        "repository": repository,
        "pull_number": pull_number,
        "actions_run_id": run_id,
        "expected_generation": expected_generation(result),
        "observed_generation": observed,
        "actor": actor,
        "event": "COMMENT",
        "mode": mode,
        "fallback_reason": fallback_reason,
        "review": (
            None
            if published is None
            else {
                "id": published.review_id,
                "url": published.url,
                "commit_id": published.commit_id,
                "request_sha256": published.request_sha256,
                "reused": published.reused,
            }
        ),
    }


def publish(
    *,
    result: dict[str, Any],
    request: dict[str, Any],
    summary_request: dict[str, Any],
    repository: str,
    run_id: int,
    token: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    pull_number = result.get("pull_number")
    planned = result.get("publication")
    mode = planned.get("mode") if isinstance(planned, dict) else "summary"
    fallback_reason = (
        planned.get("fallback_reason") if isinstance(planned, dict) else None
    )

    actor = None
    observed = None
    published = None
    try:
        if (
            type(pull_number) is not int
            or pull_number < 1
            or not isinstance(planned, dict)
            or mode not in {"inline", "summary"}
        ):
            raise PublicationFailure("PUBLICATION_IDENTITY_INVALID")
        validate_review_request(
            request,
            expected_head=result.get("head_sha"),
            allow_comments=mode == "inline",
            require_comments=mode == "inline",
        )
        validate_review_request(
            summary_request,
            expected_head=result.get("head_sha"),
            allow_comments=False,
            require_comments=False,
        )
        client = GitHubClient(token)
        try:
            actor = require_dancer_actor(client)
        except GitHubApiError as error:
            raise PublicationFailure("DANCER_AUTH_UNAVAILABLE") from error
        observed = current_generation(client, repository, pull_number)
        if planned.get("status") == "failed":
            raise PublicationFailure("COMMENT_HELPER_MISSING")

        target_request = request
        if not generation_is_current(result, observed):
            if not observed_state_is_safe_for_stale_summary(observed):
                raise PublicationFailure("STALE_BEFORE_PUBLICATION")
            fallback_reason = "STALE_BEFORE_PUBLICATION"
            mode = "summary"
            target_request = stale_summary_request(result, repository, run_id)

        def revalidate_before_mutation() -> None:
            if current_generation(client, repository, pull_number) != observed:
                raise PublicationFailure("STALE_BEFORE_PUBLICATION")

        def publish_summary_request(
            summary: dict[str, Any], summary_observed: dict[str, Any]
        ) -> PublishedReview:
            def revalidate_summary_before_mutation() -> None:
                if (
                    current_generation(client, repository, pull_number)
                    != summary_observed
                ):
                    raise PublicationFailure("STALE_BEFORE_PUBLICATION")

            try:
                return publish_one_review(
                    client,
                    repository,
                    pull_number,
                    summary,
                    summary_observed["head_sha"],
                    revalidate_summary_before_mutation,
                )
            except (GitHubApiError, InlineReviewRejected) as summary_error:
                raise PublicationFailure("SUMMARY_PUBLICATION_FAILED") from summary_error

        try:
            published = publish_one_review(
                client,
                repository,
                pull_number,
                target_request,
                observed["head_sha"],
                revalidate_before_mutation,
            )
        except PublicationFailure as error:
            if error.reason != "STALE_BEFORE_PUBLICATION":
                raise
            observed = current_generation(client, repository, pull_number)
            if not observed_state_is_safe_for_stale_summary(observed):
                raise
            mode = "summary"
            fallback_reason = "STALE_BEFORE_PUBLICATION"
            target_request = stale_summary_request(result, repository, run_id)
            published = publish_summary_request(target_request, observed)
        except InlineReviewRejected as error:
            if mode != "inline":
                raise PublicationFailure(
                    "SUMMARY_PUBLICATION_FAILED"
                ) from error
            observed = current_generation(client, repository, pull_number)
            mode = "summary"
            if generation_is_current(result, observed):
                fallback_reason = "GITHUB_422"
                target_request = summary_fallback_request(
                    summary_request, fallback_reason, omit_commit=False
                )
            elif observed_state_is_safe_for_stale_summary(observed):
                fallback_reason = "STALE_BEFORE_PUBLICATION"
                target_request = stale_summary_request(result, repository, run_id)
            else:
                raise PublicationFailure("STALE_BEFORE_PUBLICATION") from error
            published = publish_summary_request(target_request, observed)

        result = review_publication.record_publication(
            result,
            status="published",
            mode=mode,
            fallback_reason=fallback_reason,
            inline_comment_count=(len(request.get("comments", [])) if mode == "inline" else 0),
        )
        return result, receipt(
            status="published",
            repository=repository,
            pull_number=pull_number,
            run_id=run_id,
            result=result,
            observed=observed,
            actor=actor,
            mode=mode,
            fallback_reason=fallback_reason,
            published=published,
        )
    except (GitHubApiError, PublicationFailure) as error:
        reason = (
            error.reason
            if isinstance(error, PublicationFailure)
            else "PUBLICATION_STATE_LOOKUP_FAILED"
        )
        result = review_publication.record_publication(
            result,
            status="failed",
            mode=mode,
            fallback_reason=reason,
            inline_comment_count=0,
        )
        return result, receipt(
            status="failed",
            repository=repository,
            pull_number=pull_number,
            run_id=run_id,
            result=result,
            observed=observed,
            actor=actor,
            mode=mode,
            fallback_reason=reason,
            published=None,
        )


def command_publish(args: argparse.Namespace) -> None:
    result_path = pathlib.Path(args.result)
    result, publication_receipt = publish(
        result=load_json(result_path),
        request=load_json(pathlib.Path(args.request)),
        summary_request=load_json(pathlib.Path(args.summary_request)),
        repository=args.repository,
        run_id=args.run_id,
        token=os.environ.get("DANCER_GITHUB_TOKEN", ""),
    )
    write_json(result_path, result)
    write_json(pathlib.Path(args.receipt_output), publication_receipt)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(required=True)
    publish_command = commands.add_parser("publish")
    publish_command.add_argument("--result", required=True)
    publish_command.add_argument("--request", required=True)
    publish_command.add_argument("--summary-request", required=True)
    publish_command.add_argument("--repository", required=True)
    publish_command.add_argument("--run-id", required=True, type=int)
    publish_command.add_argument("--receipt-output", required=True)
    publish_command.set_defaults(handler=command_publish)
    return root


if __name__ == "__main__":
    arguments = parser().parse_args()
    arguments.handler(arguments)
