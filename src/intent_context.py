from __future__ import annotations

import hashlib
import json
import pathlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

MAX_CONTEXT_BYTES = 50_000
MAX_PROVIDER_RESPONSE_BYTES = 1_000_000
MAX_CONTEXT_AGE_SECONDS = 3_600
TRUSTED_OWNER_ACTOR_ID = 266_699_010
OWNER_MARKER_PREFIX = "<!-- fable-pr-owner/v1\n"
OWNER_MARKER_SUFFIX = "\n-->"
TICKET_PATTERN = re.compile(r"^FABLE-[1-9][0-9]*$")
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
MANIFEST_KEYS = {
    "complete",
    "truncated",
    "pull_number",
    "head_sha",
    "base_ref",
    "base_sha",
    "ticket_identifier",
    "team_key",
    "issue_updated_at",
    "collected_at_epoch",
    "bytes_included",
    "sha256",
}
OWNER_MARKER_KEYS = {
    "schemaVersion",
    "sequence",
    "writeNonce",
    "repository",
    "pullNumber",
    "taskId",
    "dispositionVersion",
    "linearTicket",
    "ownedHeadSha",
    "baseRef",
    "baseSha",
    "baseRefreshCount",
    "verification",
    "remediationHistory",
    "handledRecoveryKeys",
    "lastDisposition",
    "terminalState",
}


class IntentContextError(ValueError):
    code = "TICKET_CONTEXT_INVALID"


class IntentContextMissingError(IntentContextError):
    code = "TICKET_CONTEXT_MISSING"


class IntentContextAuthError(IntentContextError):
    code = "TICKET_CONTEXT_AUTH_MISSING"


class IntentContextGraphQLError(IntentContextError):
    code = "TICKET_CONTEXT_GRAPHQL_ERROR"


class IntentContextTeamError(IntentContextError):
    code = "TICKET_CONTEXT_TEAM_MISMATCH"


class IntentContextTruncatedError(IntentContextError):
    code = "TICKET_CONTEXT_TRUNCATED"


class IntentContextStaleError(IntentContextError):
    code = "TICKET_CONTEXT_STALE"


def _binding_tuple(value: dict[str, Any]) -> tuple[Any, ...]:
    return (
        value.get("pull_number"),
        value.get("head_sha"),
        value.get("base_ref"),
        value.get("base_sha"),
    )


def _graphql_data(payload: Any, provider: str) -> dict[str, Any]:
    if (
        not isinstance(payload, dict)
        or payload.get("errors")
        or not isinstance(payload.get("data"), dict)
    ):
        raise IntentContextGraphQLError(f"{provider} GraphQL lookup failed")
    return payload["data"]


def _request_json(
    url: str,
    *,
    headers: dict[str, str],
    data: bytes,
    timeout: int = 20,
) -> Any:
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
    except (OSError, urllib.error.URLError) as error:
        raise IntentContextGraphQLError("provider request failed") from error
    if len(raw) > MAX_PROVIDER_RESPONSE_BYTES:
        raise IntentContextTruncatedError("provider response exceeded byte limit")
    try:
        return json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IntentContextGraphQLError("provider response is malformed") from error


def collect_owner_comments(
    output_path: pathlib.Path,
    *,
    repository: str,
    pull_number: int,
    github_token: str,
    request_json=_request_json,
) -> list[dict[str, Any]]:
    if repository != "happycatlabs/fable" or pull_number < 1 or not github_token:
        raise IntentContextMissingError("trusted owner lookup input is invalid")
    owner, repo = repository.split("/", 1)
    query = """
    query TrustedOwnerComments(
      $owner: String!,
      $repo: String!,
      $number: Int!,
      $after: String
    ) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $number) {
          comments(first: 100, after: $after) {
            nodes {
              body
              lastEditedAt
              author {
                ... on User { databaseId }
                ... on Bot { databaseId }
              }
              editor {
                ... on User { databaseId }
                ... on Bot { databaseId }
              }
              userContentEdits(first: 100) {
                nodes {
                  editor {
                    ... on User { databaseId }
                    ... on Bot { databaseId }
                  }
                }
                pageInfo { hasNextPage }
              }
            }
            pageInfo { hasNextPage endCursor }
          }
        }
      }
    }
    """
    comments: list[dict[str, Any]] = []
    after: str | None = None

    def actor_id(value: Any) -> int:
        database_id = value.get("databaseId") if isinstance(value, dict) else None
        return database_id if type(database_id) is int and database_id > 0 else 0

    while True:
        payload = request_json(
            "https://api.github.com/graphql",
            headers={
                "Authorization": f"Bearer {github_token}",
                "Content-Type": "application/json",
                "User-Agent": "codex-review-workflow",
            },
            data=json.dumps(
                {
                    "query": query,
                    "variables": {
                        "owner": owner,
                        "repo": repo,
                        "number": pull_number,
                        "after": after,
                    },
                },
                separators=(",", ":"),
            ).encode(),
        )
        data = _graphql_data(payload, "GitHub")
        repository_data = data.get("repository")
        pull = (
            repository_data.get("pullRequest")
            if isinstance(repository_data, dict)
            else None
        )
        connection = pull.get("comments") if isinstance(pull, dict) else None
        if (
            not isinstance(connection, dict)
            or not isinstance(connection.get("nodes"), list)
            or not isinstance(connection.get("pageInfo"), dict)
        ):
            raise IntentContextMissingError("trusted owner comments are unavailable")
        for raw_comment in connection["nodes"]:
            edits = (
                raw_comment.get("userContentEdits")
                if isinstance(raw_comment, dict)
                else None
            )
            if (
                not isinstance(raw_comment, dict)
                or not isinstance(edits, dict)
                or not isinstance(edits.get("nodes"), list)
                or not isinstance(edits.get("pageInfo"), dict)
            ):
                raise IntentContextError("owner comment history is invalid")
            comments.append(
                {
                    "actor_id": actor_id(raw_comment.get("author")),
                    "editor_id": (
                        None
                        if raw_comment.get("lastEditedAt") is None
                        else actor_id(raw_comment.get("editor"))
                    ),
                    "edit_actor_ids": [
                        actor_id(edit.get("editor"))
                        if isinstance(edit, dict)
                        else 0
                        for edit in edits["nodes"]
                    ],
                    "edits_complete": edits["pageInfo"].get("hasNextPage") is not True,
                    "body": (
                        raw_comment["body"]
                        if isinstance(raw_comment.get("body"), str)
                        else ""
                    ),
                }
            )
        if len(comments) > 250:
            raise IntentContextTruncatedError("owner comment limit exceeded")
        page_info = connection["pageInfo"]
        if page_info.get("hasNextPage") is not True:
            break
        after = page_info.get("endCursor")
        if not isinstance(after, str) or not after:
            raise IntentContextGraphQLError("owner comment pagination is invalid")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(comments, indent=2) + "\n", encoding="utf-8")
    return comments


def extract_owner_ticket(comments: Any, binding: dict[str, Any]) -> str:
    if not isinstance(comments, list):
        raise IntentContextMissingError("owner comments are missing")
    candidates = [
        comment
        for comment in comments
        if isinstance(comment, dict)
        and comment.get("actor_id") == TRUSTED_OWNER_ACTOR_ID
        and isinstance(comment.get("body"), str)
        and OWNER_MARKER_PREFIX in comment["body"]
    ]
    if len(candidates) != 1:
        raise IntentContextMissingError("owner marker is missing or ambiguous")
    comment = candidates[0]
    if (
        comment.get("edits_complete") is not True
        or comment.get("editor_id") not in {None, TRUSTED_OWNER_ACTOR_ID}
        or not isinstance(comment.get("edit_actor_ids"), list)
        or any(
            actor_id != TRUSTED_OWNER_ACTOR_ID
            for actor_id in comment["edit_actor_ids"]
        )
    ):
        raise IntentContextError("owner marker edit history is untrusted")

    body = comment["body"]
    if body.count(OWNER_MARKER_PREFIX) != 1:
        raise IntentContextError("owner marker count is invalid")
    start = body.index(OWNER_MARKER_PREFIX) + len(OWNER_MARKER_PREFIX)
    end = body.find(OWNER_MARKER_SUFFIX, start)
    if end == -1:
        raise IntentContextError("owner marker boundaries are invalid")
    try:
        marker = json.loads(body[start:end])
    except json.JSONDecodeError as error:
        raise IntentContextError("owner marker JSON is invalid") from error

    ticket = marker.get("linearTicket") if isinstance(marker, dict) else None
    if (
        not isinstance(marker, dict)
        or set(marker) != OWNER_MARKER_KEYS
        or marker.get("schemaVersion") != "fable-pr-owner/v1"
        or marker.get("repository") != "happycatlabs/fable"
        or marker.get("dispositionVersion") != "fable-pr-disposition/v1"
        or type(marker.get("sequence")) is not int
        or marker["sequence"] < 1
        or not isinstance(marker.get("writeNonce"), str)
        or UUID_PATTERN.fullmatch(marker["writeNonce"]) is None
        or not isinstance(marker.get("taskId"), str)
        or UUID_PATTERN.fullmatch(marker["taskId"]) is None
        or marker.get("pullNumber") != binding["pull_number"]
        or marker.get("ownedHeadSha") != binding["head_sha"]
        or marker.get("baseRef") != binding["base_ref"]
        or marker.get("baseSha") != binding["base_sha"]
        or not isinstance(ticket, str)
        or TICKET_PATTERN.fullmatch(ticket) is None
    ):
        raise IntentContextError("owner marker generation is invalid")
    return ticket


def build_intent_context(
    ticket_identifier: str,
    team_key: str,
    linear_payload: Any,
    binding: dict[str, Any],
    output_path: pathlib.Path,
    *,
    collected_at_epoch: int | None = None,
) -> dict[str, Any]:
    if (
        TICKET_PATTERN.fullmatch(ticket_identifier) is None
        or re.fullmatch(r"[A-Z][A-Z0-9_-]{1,19}", team_key) is None
    ):
        raise IntentContextError("trusted ticket selector is invalid")
    issue = _graphql_data(linear_payload, "Linear").get("issue")
    if not isinstance(issue, dict):
        raise IntentContextMissingError("Linear issue is missing")
    team = issue.get("team")
    state = issue.get("state")
    comments = issue.get("comments")
    if issue.get("identifier") != ticket_identifier:
        raise IntentContextError("Linear issue identifier mismatched")
    if not isinstance(team, dict) or team.get("key") != team_key:
        raise IntentContextTeamError("Linear team mismatched")
    if (
        not isinstance(state, dict)
        or not isinstance(state.get("name"), str)
        or not isinstance(state.get("type"), str)
        or not isinstance(comments, dict)
        or not isinstance(comments.get("nodes"), list)
        or not isinstance(comments.get("pageInfo"), dict)
    ):
        raise IntentContextError("Linear issue fields are invalid")
    if comments["pageInfo"].get("hasNextPage") is True:
        raise IntentContextTruncatedError("Linear comments are truncated")

    bounded_comments = []
    for comment in comments["nodes"]:
        if (
            not isinstance(comment, dict)
            or not isinstance(comment.get("body"), str)
            or not isinstance(comment.get("createdAt"), str)
            or not isinstance(comment.get("updatedAt"), str)
        ):
            raise IntentContextError("Linear comment is invalid")
        bounded_comments.append(
            {
                "body": comment["body"],
                "created_at": comment["createdAt"],
                "updated_at": comment["updatedAt"],
            }
        )
    intent = {
        "title": issue.get("title"),
        "description": issue.get("description"),
        "status": {"name": state["name"], "type": state["type"]},
        "comments": bounded_comments,
    }
    if (
        not isinstance(intent["title"], str)
        or not isinstance(intent["description"], str)
        or not isinstance(issue.get("updatedAt"), str)
    ):
        raise IntentContextError("Linear intent fields are invalid")
    intent_bytes = json.dumps(intent, sort_keys=True, separators=(",", ":")).encode()
    if len(intent_bytes) > MAX_CONTEXT_BYTES:
        raise IntentContextTruncatedError("Linear intent byte limit exceeded")

    manifest = {
        "complete": True,
        "truncated": False,
        "pull_number": binding["pull_number"],
        "head_sha": binding["head_sha"],
        "base_ref": binding["base_ref"],
        "base_sha": binding["base_sha"],
        "ticket_identifier": ticket_identifier,
        "team_key": team_key,
        "issue_updated_at": issue["updatedAt"],
        "collected_at_epoch": (
            int(time.time()) if collected_at_epoch is None else collected_at_epoch
        ),
        "bytes_included": len(intent_bytes),
        "sha256": hashlib.sha256(intent_bytes).hexdigest(),
    }
    result = {"manifest": manifest, "intent": intent}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def collect_linear_intent(
    comments_path: pathlib.Path,
    binding: dict[str, Any],
    output_path: pathlib.Path,
    *,
    team_key: str,
    client_id: str,
    client_secret: str,
    request_json=_request_json,
) -> dict[str, Any]:
    if not client_id or not client_secret:
        raise IntentContextAuthError("Linear credentials are missing")
    comments = json.loads(comments_path.read_text(encoding="utf-8", errors="strict"))
    ticket = extract_owner_ticket(comments, binding)
    token_payload = request_json(
        "https://api.linear.app/oauth/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "read",
            }
        ).encode(),
    )
    token = token_payload.get("access_token") if isinstance(token_payload, dict) else None
    if not isinstance(token, str) or not token:
        raise IntentContextAuthError("Linear token response is invalid")
    query = """
    query TrustedReviewIntent($identifier: String!) {
      issue(id: $identifier) {
        identifier
        title
        description
        updatedAt
        state { name type }
        team { key }
        comments(first: 50) {
          nodes { body createdAt updatedAt }
          pageInfo { hasNextPage }
        }
      }
    }
    """
    linear_payload = request_json(
        "https://api.linear.app/graphql",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        data=json.dumps(
            {"query": query, "variables": {"identifier": ticket}},
            separators=(",", ":"),
        ).encode(),
    )
    return build_intent_context(
        ticket, team_key, linear_payload, binding, output_path
    )


def load_intent_context(
    path: pathlib.Path,
    binding: dict[str, Any],
    *,
    now_epoch: int | None = None,
) -> tuple[dict[str, Any], str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IntentContextError("ticket context is unreadable") from error
    if (
        not isinstance(data, dict)
        or set(data) != {"manifest", "intent"}
        or not isinstance(data.get("manifest"), dict)
        or set(data["manifest"]) != MANIFEST_KEYS
        or not isinstance(data.get("intent"), dict)
    ):
        raise IntentContextError("ticket context shape is invalid")
    manifest = data["manifest"]
    if _binding_tuple(manifest) != _binding_tuple(binding):
        raise IntentContextStaleError("ticket context binding is stale")
    if manifest.get("complete") is not True or manifest.get("truncated") is not False:
        raise IntentContextTruncatedError("ticket context is incomplete")
    collected_at = manifest.get("collected_at_epoch")
    current = int(time.time()) if now_epoch is None else now_epoch
    if type(collected_at) is not int or collected_at < 0:
        raise IntentContextError("ticket context time is invalid")
    if current - collected_at < 0 or current - collected_at > MAX_CONTEXT_AGE_SECONDS:
        raise IntentContextStaleError("ticket context is stale")

    intent_bytes = json.dumps(
        data["intent"], sort_keys=True, separators=(",", ":")
    ).encode()
    if (
        len(intent_bytes) != manifest.get("bytes_included")
        or hashlib.sha256(intent_bytes).hexdigest() != manifest.get("sha256")
        or not isinstance(manifest.get("ticket_identifier"), str)
        or TICKET_PATTERN.fullmatch(manifest["ticket_identifier"]) is None
        or not isinstance(manifest.get("team_key"), str)
        or not manifest["team_key"]
    ):
        raise IntentContextError("ticket context hash is invalid")
    rendered = json.dumps(
        {
            "ticket_identifier": manifest["ticket_identifier"],
            "team_key": manifest["team_key"],
            **data["intent"],
        },
        indent=2,
        sort_keys=True,
    )
    block = (
        "<<<BEGIN UNTRUSTED EXACT-TICKET INTENT "
        f"ticket={manifest['ticket_identifier']} team={manifest['team_key']}>>>\n"
        f"{rendered}\n"
        "<<<END UNTRUSTED EXACT-TICKET INTENT>>>"
    )
    return manifest, block, path.read_text(encoding="utf-8", errors="strict")
