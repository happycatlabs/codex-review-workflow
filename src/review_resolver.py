from __future__ import annotations

import argparse
import base64
import copy
import io
import json
import os
import pathlib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from typing import Any

import review_publication
import review_publisher
import review_resolution
import review_contract


API_ROOT = "https://api.github.com"
REQUEST_TIMEOUT_SECONDS = 30
MAX_THREAD_PAGES = 10
MAX_ARTIFACT_PAGES = 3
PAGE_SIZE = 100
MAX_ARTIFACT_BYTES = 4_000_000
MAX_FILE_BYTES = 100_000
GRAPHQL_DANCER_LOGIN = review_publisher.EXPECTED_DANCER_LOGIN.removesuffix("[bot]")
THREAD_QUERY = """
query CodexResolutionThreads($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number
      state
      headRefOid
      baseRefName
      baseRefOid
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          startLine
          originalLine
          originalStartLine
          diffSide
          startDiffSide
          originalDiffSide
          subjectType
          comments(first: 2) {
            totalCount
            pageInfo { hasNextPage }
            nodes {
              id
              fullDatabaseId
              author { login }
              replyTo { id }
            }
          }
        }
      }
    }
  }
}
"""
THREAD_READBACK_QUERY = """
query CodexResolutionThread($threadId: ID!) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      id
      isResolved
      isOutdated
      path
      line
      startLine
      originalLine
      originalStartLine
      diffSide
      startDiffSide
      originalDiffSide
      subjectType
      viewerCanResolve
      resolvedBy {
        login
        ... on Bot { databaseId }
      }
      comments(first: 2) {
        totalCount
        pageInfo { hasNextPage }
        nodes {
          id
          fullDatabaseId
          author { login }
          replyTo { id }
        }
      }
    }
  }
}
"""
RESOLVE_MUTATION = """
mutation CodexResolveReviewThread($threadId: ID!, $clientMutationId: String!) {
  resolveReviewThread(input: {threadId: $threadId, clientMutationId: $clientMutationId}) {
    clientMutationId
    thread {
      id
      isResolved
      resolvedBy {
        login
        ... on Bot { databaseId }
      }
    }
  }
}
"""


class ResolutionFailure(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


GitHubApiError = review_publisher.GitHubApiError


class _CredentialStrippingRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        redirected = super().redirect_request(
            request, fp, code, message, headers, new_url
        )
        if redirected is None:
            return None
        old_host = urllib.parse.urlsplit(request.full_url).hostname
        parsed = urllib.parse.urlsplit(new_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise urllib.error.HTTPError(
                new_url, code, "unsafe artifact redirect", headers, fp
            )
        if parsed.hostname != old_host:
            redirected.remove_header("Authorization")
        return redirected


class GitHubClient(review_publisher.GitHubClient):
    def request_bytes(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> bytes:
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
            opener = urllib.request.build_opener(_CredentialStrippingRedirect())
            with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return response.read(MAX_ARTIFACT_BYTES + 1)
        except urllib.error.HTTPError as error:
            error.read()
            raise GitHubApiError(error.code, "artifact download failed") from error
        except OSError as error:
            raise GitHubApiError(0, "artifact download failed") from error

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        response = self.request(
            "POST", "/graphql", {"query": query, "variables": variables}
        )
        if (
            not isinstance(response, dict)
            or response.get("errors")
            or not isinstance(response.get("data"), dict)
        ):
            raise ResolutionFailure("RESOLUTION_GRAPHQL_INVALID")
        return response["data"]


def _repository_parts(repository: str) -> tuple[str, str]:
    try:
        return review_publisher.repository_parts(repository)
    except review_publisher.PublicationFailure as error:
        raise ResolutionFailure("RESOLUTION_IDENTITY_INVALID") from error


def _full_database_id(value: Any) -> int | None:
    if isinstance(value, str) and value.isdigit():
        parsed = int(value)
        return parsed if parsed > 0 else None
    if type(value) is int and value > 0:
        return value
    return None


def normalize_thread(thread: Any) -> dict[str, Any] | None:
    if not isinstance(thread, dict):
        return None
    comments = thread.get("comments")
    nodes = comments.get("nodes") if isinstance(comments, dict) else None
    if (
        not isinstance(thread.get("id"), str)
        or not thread["id"]
        or thread.get("isResolved") is not False
        or type(thread.get("isOutdated")) is not bool
        or thread.get("subjectType") != "LINE"
        or thread.get("diffSide") not in {"RIGHT", None}
        or thread.get("originalDiffSide") not in {"RIGHT", None}
        or not isinstance(thread.get("path"), str)
        or review_publication.safe_relative_path(thread["path"]) != thread["path"]
        or not isinstance(comments, dict)
        or comments.get("totalCount") != 1
        or comments.get("pageInfo", {}).get("hasNextPage") is not False
        or not isinstance(nodes, list)
        or len(nodes) != 1
    ):
        return None
    comment = nodes[0]
    comment_id = _full_database_id(comment.get("fullDatabaseId"))
    if (
        comment_id is None
        or not isinstance(comment.get("id"), str)
        or not comment["id"]
        or comment.get("replyTo") is not None
        or comment.get("author") != {"login": GRAPHQL_DANCER_LOGIN}
    ):
        return None
    line = thread.get("line")
    if type(line) is not int or line < 1:
        line = thread.get("originalLine")
    if type(line) is not int or line < 1:
        return None
    start_line = thread.get("startLine")
    if start_line is None:
        start_line = thread.get("originalStartLine")
    start_side = thread.get("startDiffSide")
    if start_side is None and start_line is not None:
        start_side = thread.get("originalDiffSide")
    if (start_line is None) != (start_side is None) or (
        start_line is not None
        and (
            type(start_line) is not int
            or start_line < 1
            or start_line > line
            or start_side != "RIGHT"
        )
    ):
        return None
    return {
        "id": thread["id"],
        "is_resolved": False,
        "is_outdated": thread["isOutdated"],
        "path": thread["path"],
        "line": line,
        "start_line": start_line,
        "side": "RIGHT",
        "start_side": start_side,
        "subject_type": "LINE",
        "comment": {"id": comment["id"], "database_id": comment_id},
    }


def _raw_thread_exclusion_reason(thread: Any) -> str:
    if not isinstance(thread, dict):
        return "unsupported_thread_shape"
    comments = thread.get("comments")
    if isinstance(comments, dict) and comments.get("totalCount") != 1:
        return "thread_has_replies"
    return "not_single_root_modern_dancer"


def _record_exclusion(
    excluded: dict[str, int],
    observations: list[dict[str, Any]],
    thread: Any,
    reason: str,
) -> None:
    excluded[reason] = excluded.get(reason, 0) + 1
    if (
        isinstance(thread, dict)
        and isinstance(thread.get("id"), str)
        and thread["id"]
        and len(thread["id"]) <= 256
        and thread.get("isResolved") is False
    ):
        observations.append(
            {"thread_id": thread["id"], "reason": reason, "is_resolved": False}
        )


def list_threads(
    client: GitHubClient, repository: str, pull_number: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    owner, name = _repository_parts(repository)
    after = None
    collected: list[dict[str, Any]] = []
    pull_identity = None
    for page in range(MAX_THREAD_PAGES):
        data = client.graphql(
            THREAD_QUERY,
            {"owner": owner, "name": name, "number": pull_number, "after": after},
        )
        repo = data.get("repository")
        pull = repo.get("pullRequest") if isinstance(repo, dict) else None
        connection = pull.get("reviewThreads") if isinstance(pull, dict) else None
        if not isinstance(pull, dict) or not isinstance(connection, dict):
            raise ResolutionFailure("RESOLUTION_GRAPHQL_INVALID")
        identity = {
            "state": pull.get("state"),
            "head_sha": pull.get("headRefOid"),
            "base_ref": pull.get("baseRefName"),
            "base_sha": pull.get("baseRefOid"),
        }
        if pull_identity is None:
            pull_identity = identity
        elif identity != pull_identity:
            raise ResolutionFailure("RESOLUTION_GENERATION_DRIFT")
        nodes = connection.get("nodes")
        page_info = connection.get("pageInfo")
        if not isinstance(nodes, list) or not isinstance(page_info, dict):
            raise ResolutionFailure("RESOLUTION_GRAPHQL_INVALID")
        collected.extend(nodes)
        if page_info.get("hasNextPage") is False:
            return pull_identity, collected
        after = page_info.get("endCursor")
        if not isinstance(after, str) or not after:
            raise ResolutionFailure("RESOLUTION_GRAPHQL_INVALID")
    raise ResolutionFailure("RESOLUTION_THREAD_EVIDENCE_LIMIT_EXCEEDED")


def read_thread_node(client: GitHubClient, thread_id: str) -> dict[str, Any] | None:
    data = client.graphql(THREAD_READBACK_QUERY, {"threadId": thread_id})
    node = data.get("node")
    return node if isinstance(node, dict) else None


def read_thread(client: GitHubClient, thread_id: str) -> dict[str, Any] | None:
    return normalize_thread(read_thread_node(client, thread_id))


def graphql_dancer_actor(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("login") == GRAPHQL_DANCER_LOGIN
        and value.get("databaseId") == review_publisher.EXPECTED_DANCER_ACTOR_ID
    )


def _artifact_pair(raw: bytes) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if len(raw) > MAX_ARTIFACT_BYTES:
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            files: dict[str, bytes] = {}
            extracted_bytes = 0
            for info in archive.infolist():
                name = pathlib.PurePosixPath(info.filename)
                if (
                    info.is_dir()
                    or name.is_absolute()
                    or ".." in name.parts
                    or info.file_size > MAX_ARTIFACT_BYTES
                    or info.flag_bits & 0x1
                    or (info.external_attr >> 16) & 0o170000 == 0o120000
                ):
                    return None
                if name.name not in {
                    "codex-review-result.json",
                    "publication-receipt.json",
                }:
                    return None
                if name.name in files:
                    return None
                extracted_bytes += info.file_size
                if extracted_bytes > MAX_ARTIFACT_BYTES:
                    return None
                files[name.name] = archive.read(info)
        if set(files) != {"codex-review-result.json", "publication-receipt.json"}:
            return None
        result = json.loads(files["codex-review-result.json"].decode("utf-8"))
        receipt = json.loads(files["publication-receipt.json"].decode("utf-8"))
    except (zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError, OSError):
        return None
    return result, receipt


def _workflow_revision(run: Any) -> str | None:
    if not isinstance(run, dict):
        return None
    referenced = run.get("referenced_workflows")
    if not isinstance(referenced, list):
        return None
    matches = [
        item
        for item in referenced
        if isinstance(item, dict)
        and isinstance(item.get("sha"), str)
        and review_publisher.SHA_PATTERN.fullmatch(item["sha"])
        and item.get("path")
        == f"{review_contract.EXPECTED_WORKFLOW_PATH}@{item['sha']}"
    ]
    return matches[0]["sha"] if len(matches) == 1 else None


def _run_is_exact(
    run: Any,
    *,
    repository: str,
    run_id: int,
    head_sha: str,
    workflow_sha: str,
    caller_path: str | None,
    current: bool,
) -> bool:
    if not isinstance(run, dict):
        return False
    repository_value = run.get("repository")
    status_ok = (
        run.get("status") in {"queued", "in_progress", "completed"}
        if current
        else run.get("status") == "completed"
    )
    conclusion_ok = current or run.get("conclusion") in {"success", "failure"}
    return (
        run.get("id") == run_id
        and run.get("event") == "pull_request_target"
        and run.get("head_sha") == head_sha
        and status_ok
        and conclusion_ok
        and isinstance(repository_value, dict)
        and repository_value.get("full_name") == repository
        and isinstance(run.get("path"), str)
        and bool(run["path"])
        and (caller_path is None or run["path"] == caller_path)
        and _workflow_revision(run) == workflow_sha
    )


def collect_provenance(
    client: GitHubClient,
    repository: str,
    pull_number: int,
    current_run_id: int,
    current_result: dict[str, Any],
    current_receipt: dict[str, Any],
    workflow_sha: str,
) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
    owner, name = _repository_parts(repository)
    current_run = client.request(
        "GET", f"/repos/{owner}/{name}/actions/runs/{current_run_id}"
    )
    if not _run_is_exact(
        current_run,
        repository=repository,
        run_id=current_run_id,
        head_sha=current_result["head_sha"],
        workflow_sha=workflow_sha,
        caller_path=None,
        current=True,
    ):
        raise ResolutionFailure("RESOLUTION_CURRENT_RUN_PROVENANCE_INVALID")
    caller_path = current_run["path"]
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for page in range(1, MAX_ARTIFACT_PAGES + 1):
        response = client.request(
            "GET",
            f"/repos/{owner}/{name}/actions/artifacts"
            f"?name=codex-review-result&per_page={PAGE_SIZE}&page={page}",
        )
        artifacts = response.get("artifacts") if isinstance(response, dict) else None
        if not isinstance(artifacts, list):
            raise ResolutionFailure("RESOLUTION_ARTIFACT_EVIDENCE_INVALID")
        for artifact in artifacts:
            workflow_run = artifact.get("workflow_run") if isinstance(artifact, dict) else None
            artifact_id = artifact.get("id") if isinstance(artifact, dict) else None
            if (
                not isinstance(artifact, dict)
                or artifact.get("name") != "codex-review-result"
                or artifact.get("expired") is not False
                or type(artifact_id) is not int
                or artifact_id < 1
                or not isinstance(workflow_run, dict)
                or workflow_run.get("id") == current_run_id
            ):
                continue
            try:
                pair = _artifact_pair(
                    client.request_bytes(
                        "GET",
                        f"/repos/{owner}/{name}/actions/artifacts/{artifact_id}/zip",
                    )
                )
            except GitHubApiError:
                continue
            if pair is not None:
                result, receipt = pair
                prior_run_id = workflow_run.get("id")
                if receipt.get("actions_run_id") != prior_run_id:
                    continue
                prior_run = client.request(
                    "GET", f"/repos/{owner}/{name}/actions/runs/{prior_run_id}"
                )
                prior_workflow_sha = result.get("workflow_revision")
                if (
                    not isinstance(prior_workflow_sha, str)
                    or review_publisher.SHA_PATTERN.fullmatch(prior_workflow_sha)
                    is None
                    or not _run_is_exact(
                        prior_run,
                        repository=repository,
                        run_id=prior_run_id,
                        head_sha=result.get("head_sha"),
                        workflow_sha=prior_workflow_sha,
                        caller_path=caller_path,
                        current=False,
                    )
                    or workflow_run.get("head_sha") not in {None, result.get("head_sha")}
                ):
                    continue
                pairs.append((result, receipt))
        if len(artifacts) < PAGE_SIZE:
            break
        if page == MAX_ARTIFACT_PAGES:
            raise ResolutionFailure("RESOLUTION_ARTIFACT_EVIDENCE_LIMIT_EXCEEDED")
    proven = []
    seen_pairs: set[tuple[str, str]] = set()
    for result, receipt in pairs:
        try:
            result, receipt, request = review_resolution.validate_inline_provenance(
                result,
                receipt,
                repository=repository,
                pull_number=pull_number,
            )
        except review_resolution.ResolutionContractError:
            continue
        pair_identity = (
            review_resolution.canonical_sha256(result),
            review_resolution.canonical_sha256(receipt),
        )
        if pair_identity in seen_pairs:
            continue
        seen_pairs.add(pair_identity)
        proven.append((result, receipt, request))
    return proven


def _modern_comment_snapshot(comment: Any) -> dict[str, Any] | None:
    if not isinstance(comment, dict):
        return None
    user = comment.get("user")
    if (
        type(comment.get("id")) is not int
        or comment["id"] < 1
        or not isinstance(comment.get("node_id"), str)
        or type(comment.get("pull_request_review_id")) is not int
        or comment.get("in_reply_to_id") is not None
        or not review_publisher.actor_matches(user)
        or not isinstance(comment.get("body"), str)
        or review_publication.safe_relative_path(comment.get("path"))
        != comment.get("path")
        or not isinstance(comment.get("commit_id"), str)
        or review_publisher.SHA_PATTERN.fullmatch(comment["commit_id"]) is None
    ):
        return None
    line = comment.get("line")
    side = comment.get("side")
    if type(line) is not int or line < 1:
        line = comment.get("original_line")
        side = comment.get("original_side")
    if type(line) is not int or line < 1 or side != "RIGHT":
        return None
    start_line = comment.get("start_line")
    start_side = comment.get("start_side")
    if start_line is None:
        start_line = comment.get("original_start_line")
    if start_side is None and start_line is not None:
        start_side = comment.get("original_side")
    if (start_line is None) != (start_side is None) or (
        start_line is not None
        and (
            type(start_line) is not int
            or start_line < 1
            or start_line > line
            or start_side != "RIGHT"
        )
    ):
        return None
    return {
        "id": comment["id"],
        "node_id": comment["node_id"],
        "review_id": comment["pull_request_review_id"],
        "body": comment["body"],
        "path": comment["path"],
        "line": line,
        "side": "RIGHT",
        "start_line": start_line,
        "start_side": start_side,
        "commit_id": comment["commit_id"],
        "original_commit_id": comment.get("original_commit_id"),
        "actor": {
            "login": review_publisher.EXPECTED_DANCER_LOGIN,
            "id": review_publisher.EXPECTED_DANCER_ACTOR_ID,
        },
    }


def _published_review_is_exact(
    client: GitHubClient,
    *,
    repository: str,
    pull_number: int,
    receipt: dict[str, Any],
    request: dict[str, Any],
) -> bool:
    prepared, digest = review_publisher.marked_request(request)
    if digest != receipt["review"]["request_sha256"]:
        return False
    try:
        owner, name = _repository_parts(repository)
        review_id = receipt["review"]["id"]
        review = client.request(
            "GET", f"/repos/{owner}/{name}/pulls/{pull_number}/reviews/{review_id}"
        )
        if (
            not isinstance(review, dict)
            or review.get("id") != review_id
            or review.get("state") != "COMMENTED"
            or review.get("body") != prepared["body"]
            or review.get("commit_id") != request["commit_id"]
            or review.get("html_url") != receipt["review"]["url"]
            or not review_publisher.actor_matches(review.get("user"))
        ):
            return False
        references = review_publisher.paginated_list(
            client,
            f"/repos/{owner}/{name}/pulls/{pull_number}/reviews/{review_id}/comments",
            max_pages=review_publisher.MAX_COMMENT_PAGES,
        )
        expected_comments = request.get("comments", [])
        if len(references) != len(expected_comments):
            return False
        seen_ids: set[int] = set()
        for expected, reference in zip(expected_comments, references):
            comment_id = reference.get("id")
            if (
                type(comment_id) is not int
                or comment_id < 1
                or comment_id in seen_ids
                or reference.get("pull_request_review_id") != review_id
            ):
                return False
            seen_ids.add(comment_id)
            actual = _modern_comment_snapshot(
                client.request(
                    "GET", f"/repos/{owner}/{name}/pulls/comments/{comment_id}"
                )
            )
            if (
                actual is None
                or actual["review_id"] != review_id
                or actual["commit_id"] != request["commit_id"]
                or actual["original_commit_id"] != request["commit_id"]
                or review_publisher.expected_comment(expected)
                != {
                    "path": actual["path"],
                    "line": actual["line"],
                    "side": actual["side"],
                    "start_line": actual["start_line"],
                    "start_side": actual["start_side"],
                    "body": actual["body"],
                }
            ):
                return False
    except (GitHubApiError, review_publisher.PublicationFailure):
        return False
    return True


def _matching_finding(
    rest: dict[str, Any],
    provenance: tuple[dict[str, Any], dict[str, Any], dict[str, Any]],
) -> dict[str, Any] | None:
    result, receipt, request = provenance
    if (
        rest["review_id"] != receipt["review"]["id"]
        or rest["commit_id"] != result["head_sha"]
        or rest["original_commit_id"] != result["head_sha"]
    ):
        return None
    matches = []
    for finding, expected in zip(result["findings"], request["comments"]):
        actual = {
            "path": rest["path"],
            "line": rest["line"],
            "side": rest["side"],
            "start_line": rest["start_line"],
            "start_side": rest["start_side"],
            "body": rest["body"],
        }
        if review_publisher.expected_comment(expected) == actual:
            matches.append(finding)
    return matches[0] if len(matches) == 1 else None


def _current_file(
    client: GitHubClient, repository: str, path: str, head_sha: str
) -> dict[str, Any]:
    owner, name = _repository_parts(repository)
    encoded_path = urllib.parse.quote(path, safe="/")
    encoded_ref = urllib.parse.quote(head_sha, safe="")
    try:
        response = client.request(
            "GET",
            f"/repos/{owner}/{name}/contents/{encoded_path}?ref={encoded_ref}",
        )
    except GitHubApiError as error:
        if error.status == 404:
            return {"status": "missing", "path": path, "head_sha": head_sha}
        raise
    if (
        not isinstance(response, dict)
        or response.get("type") != "file"
        or response.get("path") != path
        or response.get("encoding") != "base64"
        or type(response.get("size")) is not int
        or response["size"] < 0
        or response["size"] > MAX_FILE_BYTES
        or not isinstance(response.get("content"), str)
    ):
        raise ResolutionFailure("RESOLUTION_SOURCE_INVALID")
    try:
        encoded = "".join(response["content"].split())
        raw = base64.b64decode(encoded, validate=True)
        content = raw.decode("utf-8", errors="strict")
    except (ValueError, UnicodeDecodeError) as error:
        raise ResolutionFailure("RESOLUTION_SOURCE_INVALID") from error
    if len(raw) != response["size"] or len(raw) > MAX_FILE_BYTES:
        raise ResolutionFailure("RESOLUTION_SOURCE_INVALID")
    return {"status": "file", "path": path, "head_sha": head_sha, "content": content}


def prepare(
    *,
    repository: str,
    pull_number: int,
    run_id: int,
    workflow_sha: str,
    current_result: dict[str, Any],
    current_receipt: dict[str, Any],
    token: str,
) -> dict[str, Any]:
    current_result, current_receipt = review_resolution.validate_publication_pair(
        current_result,
        current_receipt,
        repository=repository,
        pull_number=pull_number,
        require_inline=False,
        expected_run_id=run_id,
        expected_workflow_sha=workflow_sha,
    )
    client = GitHubClient(token)
    observed = review_publisher.current_generation(client, repository, pull_number)
    if not review_publisher.generation_is_current(current_result, observed):
        raise ResolutionFailure("RESOLUTION_GENERATION_DRIFT")
    _validate_live_publication(
        client, repository, pull_number, current_result, current_receipt
    )
    pull_identity, threads = list_threads(client, repository, pull_number)
    if pull_identity != {
        "state": "OPEN",
        "head_sha": current_result["head_sha"],
        "base_ref": current_result["base_ref"],
        "base_sha": current_result["base_sha"],
    }:
        raise ResolutionFailure("RESOLUTION_GENERATION_DRIFT")
    provenance = collect_provenance(
        client,
        repository,
        pull_number,
        run_id,
        current_result,
        current_receipt,
        workflow_sha,
    )
    provenance_by_review: dict[int, list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]] = {}
    for item in provenance:
        provenance_by_review.setdefault(item[1]["review"]["id"], []).append(item)
    owner, name = _repository_parts(repository)
    excluded: dict[str, int] = {}
    observations: list[dict[str, Any]] = []
    candidates = []
    file_evidence: dict[str, dict[str, Any]] = {}
    reviews: dict[int, bool] = {}
    for raw_thread in threads:
        thread = normalize_thread(raw_thread)
        if thread is None:
            _record_exclusion(
                excluded,
                observations,
                raw_thread,
                _raw_thread_exclusion_reason(raw_thread),
            )
            continue
        comment_id = thread["comment"]["database_id"]
        try:
            comment = client.request(
                "GET", f"/repos/{owner}/{name}/pulls/comments/{comment_id}"
            )
        except GitHubApiError:
            _record_exclusion(
                excluded, observations, raw_thread, "exact_comment_unavailable"
            )
            continue
        rest = _modern_comment_snapshot(comment)
        if (
            rest is None
            or rest["id"] != comment_id
            or rest["node_id"] != thread["comment"]["id"]
            or rest["path"] != thread["path"]
            or rest["line"] != thread["line"]
            or rest["start_line"] != thread["start_line"]
        ):
            _record_exclusion(
                excluded, observations, raw_thread, "exact_comment_mismatch"
            )
            continue
        matches = []
        for item in provenance_by_review.get(rest["review_id"], []):
            finding = _matching_finding(rest, item)
            if finding is None:
                continue
            if rest["review_id"] not in reviews:
                reviews[rest["review_id"]] = _published_review_is_exact(
                    client,
                    repository=repository,
                    pull_number=pull_number,
                    receipt=item[1],
                    request=item[2],
                )
            if reviews[rest["review_id"]]:
                matches.append((item, finding))
        if len(matches) != 1:
            _record_exclusion(
                excluded,
                observations,
                raw_thread,
                "provenance_missing_or_ambiguous",
            )
            continue
        (prior_result, prior_receipt, _), finding = matches[0]
        path = finding["file"]
        if path not in file_evidence:
            try:
                file_evidence[path] = _current_file(
                    client, repository, path, current_result["head_sha"]
                )
            except (GitHubApiError, ResolutionFailure):
                _record_exclusion(
                    excluded,
                    observations,
                    raw_thread,
                    "current_source_unavailable",
                )
                continue
        candidates.append(
            {
                "thread_id": thread["id"],
                "thread_snapshot": thread,
                "comment_snapshot": rest,
                "provenance": {
                    "actions_run_id": prior_receipt["actions_run_id"],
                    "review_id": prior_receipt["review"]["id"],
                    "request_sha256": prior_receipt["review"]["request_sha256"],
                    "result_sha256": review_resolution.canonical_sha256(prior_result),
                    "receipt_sha256": review_resolution.canonical_sha256(prior_receipt),
                    "fingerprint": finding["fingerprint"],
                    "result": copy.deepcopy(prior_result),
                    "receipt": copy.deepcopy(prior_receipt),
                },
                "prior_finding": copy.deepcopy(finding),
                "current_evidence": copy.deepcopy(file_evidence[path]),
            }
        )
    packet = review_resolution.build_candidate_packet(
        repository=repository,
        pull_number=pull_number,
        run_id=run_id,
        workflow_sha=workflow_sha,
        current_result=current_result,
        current_receipt=current_receipt,
        candidates=candidates,
        excluded=excluded,
        observations=observations,
    )
    return packet


def _current_request(
    result: dict[str, Any], receipt: dict[str, Any], repository: str
) -> dict[str, Any]:
    if result["publication"]["mode"] == "inline":
        return review_resolution.inline_request(
            result, repository=repository, run_id=receipt["actions_run_id"]
        )
    return {
        "body": review_publication.complete_review_body(
            result,
            result["publication"].get("fallback_reason"),
            repository,
            receipt["actions_run_id"],
        ),
        "event": "COMMENT",
        "commit_id": result["head_sha"],
    }


def _validate_live_publication(
    client: GitHubClient,
    repository: str,
    pull_number: int,
    result: dict[str, Any],
    receipt: dict[str, Any],
) -> None:
    request = _current_request(result, receipt, repository)
    if not _published_review_is_exact(
        client,
        repository=repository,
        pull_number=pull_number,
        receipt=receipt,
        request=request,
    ):
        raise ResolutionFailure("RESOLUTION_CURRENT_RECEIPT_INVALID")


def _prove_candidate_live(
    client: GitHubClient,
    repository: str,
    pull_number: int,
    candidate: dict[str, Any],
    *,
    require_can_resolve: bool,
) -> None:
    node = read_thread_node(client, candidate["thread_id"])
    if require_can_resolve and (
        not isinstance(node, dict) or node.get("viewerCanResolve") is not True
    ):
        raise ResolutionFailure("RESOLUTION_THREAD_NOT_RESOLVABLE")
    thread = normalize_thread(node)
    if thread != candidate.get("thread_snapshot"):
        raise ResolutionFailure("RESOLUTION_THREAD_DRIFT")
    owner, name = _repository_parts(repository)
    rest = _modern_comment_snapshot(
        client.request(
            "GET",
            f"/repos/{owner}/{name}/pulls/comments/"
            f"{candidate['comment_snapshot']['id']}",
        )
    )
    if rest != candidate.get("comment_snapshot"):
        raise ResolutionFailure("RESOLUTION_THREAD_DRIFT")
    provenance = candidate.get("provenance")
    if not isinstance(provenance, dict):
        raise ResolutionFailure("RESOLUTION_PROVENANCE_INVALID")
    result, receipt, request = review_resolution.validate_inline_provenance(
        provenance.get("result"),
        provenance.get("receipt"),
        repository=repository,
        pull_number=pull_number,
    )
    if (
        review_resolution.canonical_sha256(result) != provenance.get("result_sha256")
        or review_resolution.canonical_sha256(receipt)
        != provenance.get("receipt_sha256")
        or _matching_finding(rest, (result, receipt, request))
        != candidate.get("prior_finding")
    ):
        raise ResolutionFailure("RESOLUTION_PROVENANCE_INVALID")
    if not _published_review_is_exact(
        client,
        repository=repository,
        pull_number=pull_number,
        receipt=receipt,
        request=request,
    ):
        raise ResolutionFailure("RESOLUTION_PROVENANCE_INVALID")


def _receipt(
    *,
    status: str,
    repository: str,
    pull_number: int,
    run_id: int,
    packet: dict[str, Any],
    plan: dict[str, Any],
    actor: dict[str, Any] | None,
    observed: dict[str, Any] | None,
    results: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    error: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": review_resolution.RECEIPT_VERSION,
        "status": status,
        "repository": repository,
        "pull_number": pull_number,
        "actions_run_id": run_id,
        "packet_sha256": review_resolution.canonical_sha256(packet),
        "plan_sha256": review_resolution.canonical_sha256(plan),
        "expected_generation": packet.get("current_generation"),
        "observed_generation": observed,
        "current_publication": packet.get("current_publication"),
        "actor": actor,
        "candidate_count": packet.get("candidate_count", 0),
        "resolve_count": sum(
            item.get("decision") in review_resolution.RESOLVE_DECISIONS
            for item in plan.get("decisions", [])
        ),
        "results": results,
        "observations": observations,
        "error": error,
    }


def _sanitized_result(
    decision: dict[str, Any],
    candidate: dict[str, Any],
    *,
    current_head_sha: str,
    mutation: str,
    is_resolved: bool,
    client_mutation_id: str | None = None,
) -> dict[str, Any]:
    provenance = candidate["provenance"]
    return {
        "thread_id": decision["thread_id"],
        "prior_fingerprint": provenance["fingerprint"],
        "prior_head_sha": provenance["result"]["head_sha"],
        "current_head_sha": current_head_sha,
        "decision": decision["decision"],
        "mutation": mutation,
        "client_mutation_id": client_mutation_id,
        "is_resolved": is_resolved,
    }


def apply(
    *,
    repository: str,
    pull_number: int,
    run_id: int,
    current_result: dict[str, Any],
    current_receipt: dict[str, Any],
    packet: dict[str, Any],
    plan: dict[str, Any],
    read_token: str,
    dancer_token: str,
) -> dict[str, Any]:
    actor = None
    observed = None
    results: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    try:
        review_resolution.validate_publication_pair(
            current_result,
            current_receipt,
            repository=repository,
            pull_number=pull_number,
            require_inline=False,
            expected_run_id=run_id,
            expected_workflow_sha=packet.get("workflow_revision"),
        )
        candidates_list = packet.get("candidates")
        packet_observations = packet.get("observations")
        if (
            packet.get("schema_version")
            != review_resolution.CANDIDATE_PACKET_VERSION
            or plan.get("schema_version") != review_resolution.PLAN_VERSION
            or plan.get("packet_sha256")
            != review_resolution.canonical_sha256(packet)
            or packet.get("current_publication", {}).get("result_sha256")
            != review_resolution.canonical_sha256(current_result)
            or packet.get("current_publication", {}).get("receipt_sha256")
            != review_resolution.canonical_sha256(current_receipt)
            or plan.get("current_generation") != packet.get("current_generation")
            or plan.get("current_publication") != packet.get("current_publication")
            or packet.get("repository") != repository
            or packet.get("pull_number") != pull_number
            or packet.get("actions_run_id") != run_id
            or not isinstance(candidates_list, list)
            or len(candidates_list) > review_resolution.MAX_CANDIDATES
            or packet.get("candidate_count") != len(candidates_list)
            or not isinstance(packet_observations, list)
        ):
            raise ResolutionFailure("RESOLUTION_PLAN_INVALID")
        if packet.get("status") == "overflow" or plan.get("status") == "overflow":
            if (
                packet.get("status") != "overflow"
                or plan.get("status") != "overflow"
                or candidates_list
                or plan.get("decisions") != []
                or type(packet.get("overflow_count")) is not int
                or packet["overflow_count"] <= review_resolution.MAX_CANDIDATES
            ):
                raise ResolutionFailure("RESOLUTION_PLAN_INVALID")
            return _receipt(
                status="overflow",
                repository=repository,
                pull_number=pull_number,
                run_id=run_id,
                packet=packet,
                plan=plan,
                actor=None,
                observed=None,
                results=[],
                observations=[],
                error="RESOLUTION_CANDIDATE_LIMIT_EXCEEDED",
            )
        if any(
            not isinstance(item, dict) or not isinstance(item.get("thread_id"), str)
            for item in candidates_list
        ):
            raise ResolutionFailure("RESOLUTION_PLAN_INVALID")
        candidates = {item["thread_id"]: item for item in candidates_list}
        if len(candidates) != len(candidates_list):
            raise ResolutionFailure("RESOLUTION_PLAN_INVALID")
        decisions = plan.get("decisions")
        if (
            not isinstance(decisions, list)
            or any(not isinstance(item, dict) for item in decisions)
            or {item.get("thread_id") for item in decisions} != set(candidates)
            or any(
                item.get("decision") not in review_resolution.DECISIONS
                or item.get("candidate_sha256")
                != review_resolution.canonical_sha256(candidates[item["thread_id"]])
                or (
                    candidates[item["thread_id"]].get("deterministic_decision")
                    is not None
                    and item.get("decision")
                    != candidates[item["thread_id"]]["deterministic_decision"]
                )
                for item in decisions
            )
        ):
            raise ResolutionFailure("RESOLUTION_PLAN_INVALID")
        if not decisions and not packet_observations:
            return _receipt(
                status="completed",
                repository=repository,
                pull_number=pull_number,
                run_id=run_id,
                packet=packet,
                plan=plan,
                actor=None,
                observed=None,
                results=[],
                observations=[],
                error=None,
            )
        read_client = GitHubClient(read_token)
        observed = review_publisher.current_generation(
            read_client, repository, pull_number
        )
        if not review_publisher.generation_is_current(current_result, observed):
            raise ResolutionFailure("RESOLUTION_GENERATION_DRIFT")
        _validate_live_publication(
            read_client, repository, pull_number, current_result, current_receipt
        )
        for candidate in candidates.values():
            _prove_candidate_live(
                read_client,
                repository,
                pull_number,
                candidate,
                require_can_resolve=False,
            )
        for item in packet_observations:
            if (
                not isinstance(item, dict)
                or set(item) != {"thread_id", "reason", "is_resolved"}
                or item.get("is_resolved") is not False
            ):
                raise ResolutionFailure("RESOLUTION_PLAN_INVALID")
            try:
                node = read_thread_node(read_client, item["thread_id"])
                resolved = (
                    node.get("isResolved")
                    if isinstance(node, dict)
                    and node.get("id") == item["thread_id"]
                    and type(node.get("isResolved")) is bool
                    else None
                )
            except (GitHubApiError, ResolutionFailure):
                resolved = None
            observations.append(
                {
                    "thread_id": item["thread_id"],
                    "reason": item["reason"],
                    "is_resolved": resolved,
                }
            )
        has_resolution = any(
            item["decision"] in review_resolution.RESOLVE_DECISIONS
            for item in decisions
        )
        if not has_resolution:
            results = [
                _sanitized_result(
                    item,
                    candidates[item["thread_id"]],
                    current_head_sha=current_result["head_sha"],
                    mutation="untouched",
                    is_resolved=False,
                )
                for item in decisions
            ]
            return _receipt(
                status="completed",
                repository=repository,
                pull_number=pull_number,
                run_id=run_id,
                packet=packet,
                plan=plan,
                actor=None,
                observed=observed,
                results=results,
                observations=observations,
                error=None,
            )
        client = GitHubClient(dancer_token)
        try:
            actor = review_publisher.require_dancer_actor(client)
        except (review_publisher.PublicationFailure, GitHubApiError) as error:
            raise ResolutionFailure("RESOLUTION_DANCER_AUTH_INVALID") from error
        dancer_observed = review_publisher.current_generation(
            client, repository, pull_number
        )
        if dancer_observed != observed:
            raise ResolutionFailure("RESOLUTION_GENERATION_DRIFT")
        _validate_live_publication(
            client, repository, pull_number, current_result, current_receipt
        )
        for decision in decisions:
            candidate = candidates[decision["thread_id"]]
            _prove_candidate_live(
                client,
                repository,
                pull_number,
                candidate,
                require_can_resolve=(
                    decision["decision"] in review_resolution.RESOLVE_DECISIONS
                ),
            )
        for decision in decisions:
            thread_id = decision["thread_id"]
            if decision["decision"] not in review_resolution.RESOLVE_DECISIONS:
                results.append(
                    _sanitized_result(
                        decision,
                        candidates[thread_id],
                        current_head_sha=current_result["head_sha"],
                        mutation="untouched",
                        is_resolved=False,
                    )
                )
                continue
            _validate_live_publication(
                client, repository, pull_number, current_result, current_receipt
            )
            _prove_candidate_live(
                client,
                repository,
                pull_number,
                candidates[thread_id],
                require_can_resolve=True,
            )
            if review_publisher.current_generation(
                client, repository, pull_number
            ) != observed:
                raise ResolutionFailure("RESOLUTION_GENERATION_DRIFT")
            client_mutation_id = review_resolution.canonical_sha256(
                {
                    "schema_version": review_resolution.RECEIPT_VERSION,
                    "repository": repository,
                    "pull_number": pull_number,
                    "actions_run_id": run_id,
                    "thread_id": thread_id,
                    "candidate_sha256": decision["candidate_sha256"],
                    "decision": decision["decision"],
                }
            )
            mutation_status = "resolved"
            try:
                data = client.graphql(
                    RESOLVE_MUTATION,
                    {
                        "threadId": thread_id,
                        "clientMutationId": client_mutation_id,
                    },
                )
                payload = data.get("resolveReviewThread")
                mutated = payload.get("thread") if isinstance(payload, dict) else None
                if (
                    not isinstance(payload, dict)
                    or payload.get("clientMutationId") != client_mutation_id
                    or not isinstance(mutated, dict)
                    or mutated.get("id") != thread_id
                    or mutated.get("isResolved") is not True
                    or not graphql_dancer_actor(mutated.get("resolvedBy"))
                ):
                    raise ResolutionFailure("RESOLUTION_MUTATION_INVALID")
            except GitHubApiError as error:
                if error.status not in {0, 500, 502, 503, 504}:
                    raise ResolutionFailure("RESOLUTION_MUTATION_REJECTED") from error
                mutation_status = "ambiguous_response"
            except ResolutionFailure:
                mutation_status = "ambiguous_response"
            node = read_thread_node(client, thread_id)
            if (
                not isinstance(node, dict)
                or node.get("id") != thread_id
                or node.get("isResolved") is not True
                or not graphql_dancer_actor(node.get("resolvedBy"))
            ):
                raise ResolutionFailure("RESOLUTION_READBACK_FAILED")
            unresolved_shape = copy.deepcopy(node)
            unresolved_shape["isResolved"] = False
            if normalize_thread(unresolved_shape) != candidates[thread_id][
                "thread_snapshot"
            ]:
                raise ResolutionFailure("RESOLUTION_READBACK_FAILED")
            if review_publisher.current_generation(
                client, repository, pull_number
            ) != observed:
                raise ResolutionFailure("RESOLUTION_GENERATION_DRIFT")
            results.append(
                _sanitized_result(
                    decision,
                    candidates[thread_id],
                    current_head_sha=current_result["head_sha"],
                    mutation=mutation_status,
                    is_resolved=True,
                    client_mutation_id=client_mutation_id,
                )
            )
        return _receipt(
            status="completed",
            repository=repository,
            pull_number=pull_number,
            run_id=run_id,
            packet=packet,
            plan=plan,
            actor=actor,
            observed=observed,
            results=results,
            observations=observations,
            error=None,
        )
    except (
        GitHubApiError,
        ResolutionFailure,
        review_publisher.GitHubApiError,
        review_publisher.PublicationFailure,
        review_resolution.ResolutionContractError,
    ) as error:
        reason = getattr(error, "reason", "RESOLUTION_FAILED")
        return _receipt(
            status="failed",
            repository=repository,
            pull_number=pull_number,
            run_id=run_id,
            packet=packet,
            plan=plan,
            actor=actor,
            observed=observed,
            results=results,
            observations=observations,
            error=reason,
        )


def command_prepare(args: argparse.Namespace) -> None:
    current_result = review_resolution.load_json(pathlib.Path(args.current_result))
    current_receipt = review_resolution.load_json(pathlib.Path(args.current_receipt))
    packet = prepare(
        repository=args.repository,
        pull_number=args.pull_number,
        run_id=args.run_id,
        workflow_sha=args.workflow_sha,
        current_result=current_result,
        current_receipt=current_receipt,
        token=os.environ.get("GH_TOKEN", ""),
    )
    review_resolution.write_json(pathlib.Path(args.output), packet)


def command_apply(args: argparse.Namespace) -> None:
    receipt = apply(
        repository=args.repository,
        pull_number=args.pull_number,
        run_id=args.run_id,
        current_result=review_resolution.load_json(pathlib.Path(args.current_result)),
        current_receipt=review_resolution.load_json(pathlib.Path(args.current_receipt)),
        packet=review_resolution.load_json(pathlib.Path(args.packet)),
        plan=review_resolution.load_json(pathlib.Path(args.plan)),
        read_token=os.environ.get("GH_TOKEN", ""),
        dancer_token=os.environ.get("DANCER_GITHUB_TOKEN", ""),
    )
    review_resolution.write_json(pathlib.Path(args.receipt_output), receipt)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(required=True)
    prepare_command = commands.add_parser("prepare")
    prepare_command.add_argument("--repository", required=True)
    prepare_command.add_argument("--pull-number", required=True, type=int)
    prepare_command.add_argument("--run-id", required=True, type=int)
    prepare_command.add_argument("--workflow-sha", required=True)
    prepare_command.add_argument("--current-result", required=True)
    prepare_command.add_argument("--current-receipt", required=True)
    prepare_command.add_argument("--output", required=True)
    prepare_command.set_defaults(handler=command_prepare)
    apply_command = commands.add_parser("apply")
    apply_command.add_argument("--repository", required=True)
    apply_command.add_argument("--pull-number", required=True, type=int)
    apply_command.add_argument("--run-id", required=True, type=int)
    apply_command.add_argument("--current-result", required=True)
    apply_command.add_argument("--current-receipt", required=True)
    apply_command.add_argument("--packet", required=True)
    apply_command.add_argument("--plan", required=True)
    apply_command.add_argument("--receipt-output", required=True)
    apply_command.set_defaults(handler=command_apply)
    return root


if __name__ == "__main__":
    arguments = parser().parse_args()
    arguments.handler(arguments)
