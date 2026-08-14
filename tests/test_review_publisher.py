from __future__ import annotations

import copy
import pathlib
import sys
import unittest
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import review_publication  # noqa: E402
import review_publisher  # noqa: E402


REPOSITORY = "happycatlabs/fable"
PULL_NUMBER = 205
RUN_ID = 123456789
HEAD_SHA = "a" * 40
BASE_SHA = "b" * 40
NEXT_HEAD_SHA = "c" * 40


def finding() -> dict:
    return {
        "severity": "BUG",
        "blocking": True,
        "file": "lib/example.ts",
        "start_line": 42,
        "line": 42,
        "title": "Wrong fallback",
        "body": "The fallback now returns the wrong value for signed-out users.",
        "fingerprint": "d" * 64,
    }


def result_fixture(findings: list[dict] | None = None) -> dict:
    findings = findings or []
    return {
        "schema_version": "codex-review-result/v3",
        "verdict": "blocking_findings" if findings else "clean",
        "pull_number": PULL_NUMBER,
        "head_sha": HEAD_SHA,
        "base_ref": "master",
        "base_sha": BASE_SHA,
        "state": "open",
        "review_scope": "source_context_v1",
        "coverage": {
            "complete": True,
            "truncated": False,
            "diff_bytes_included": 10,
            "source_context_bytes": 20,
        },
        "lookup_context": {"complete": True},
        "summary": "One concrete production regression." if findings else "No issues found.",
        "findings": findings,
        "blocking_count": len(findings),
        "non_blocking_count": 0,
        "finding_fingerprints": [item["fingerprint"] for item in findings],
        "workflow_revision": "e" * 40,
        "reviewer_revision": "reviewer",
        "error": None,
        "publication": {
            "status": "pending",
            "mode": "inline" if findings else "summary",
            "fallback_reason": None,
            "inline_comment_count": 0,
        },
    }


def publication_plan(findings: list[dict] | None = None):
    result = result_fixture(findings)
    comment_map = {
        "schema_version": review_publication.COMMENT_MAP_VERSION,
        "complete": True,
        "pull_number": PULL_NUMBER,
        "head_sha": HEAD_SHA,
        "base_ref": "master",
        "base_sha": BASE_SHA,
        "diff_sha256": "f" * 64,
        "files": {"lib/example.ts": [[40, 45]]},
    }
    return review_publication.plan_publication(
        result,
        comment_map,
        repository=REPOSITORY,
        run_id=RUN_ID,
    )


class FakeGitHubClient:
    def __init__(self):
        self.actor = {
            "login": review_publisher.EXPECTED_DANCER_LOGIN,
            "id": review_publisher.EXPECTED_DANCER_ACTOR_ID,
        }
        self.head_sha = HEAD_SHA
        self.base_sha = BASE_SHA
        self.reviews: list[dict] = []
        self.comments: dict[int, list[dict]] = {}
        self.post_count = 0
        self.inline_422_once = False
        self.accept_then_fail = False
        self.drop_comments_on_readback = False
        self.change_head_at_pull_read: int | None = None
        self.fail_commit_read_at: int | None = None
        self.fail_review_list = False
        self.fail_review_readback = False
        self.pull_reads = 0
        self.commit_reads = 0

    def request(self, method: str, path: str, payload=None):
        if method == "POST" and path == "/graphql":
            return {
                "data": {
                    "viewer": {
                        "login": self.actor["login"],
                        "databaseId": self.actor["id"],
                    }
                }
            }
        if method == "GET" and path == "/repos/happycatlabs/fable":
            return {"default_branch": "master"}
        if method == "GET" and path == "/repos/happycatlabs/fable/pulls/205":
            self.pull_reads += 1
            if (
                self.change_head_at_pull_read is not None
                and self.pull_reads >= self.change_head_at_pull_read
            ):
                self.head_sha = NEXT_HEAD_SHA
            return {
                "state": "open",
                "head": {"sha": self.head_sha},
                "base": {"ref": "master", "sha": self.base_sha},
            }
        if method == "GET" and path == "/repos/happycatlabs/fable/commits/master":
            self.commit_reads += 1
            if (
                self.fail_commit_read_at is not None
                and self.commit_reads >= self.fail_commit_read_at
            ):
                raise review_publisher.GitHubApiError(422, "lookup rejected")
            return {"sha": self.base_sha}
        if method == "GET" and "/reviews?" in path:
            if self.fail_review_list:
                raise review_publisher.GitHubApiError(422, "evidence rejected")
            page = int(path.rsplit("page=", 1)[1])
            return copy.deepcopy(self.reviews if page == 1 else [])
        if method == "POST" and path == "/repos/happycatlabs/fable/pulls/205/reviews":
            self.post_count += 1
            if self.inline_422_once and payload.get("comments"):
                self.inline_422_once = False
                raise review_publisher.GitHubApiError(422, "validation failed")
            review_id = 900 + self.post_count
            review = {
                "id": review_id,
                "state": "COMMENTED",
                "body": payload["body"],
                "commit_id": payload.get("commit_id", self.head_sha),
                "html_url": (
                    f"https://github.com/{REPOSITORY}/pull/{PULL_NUMBER}"
                    f"#pullrequestreview-{review_id}"
                ),
                "user": copy.deepcopy(self.actor),
            }
            self.reviews.append(review)
            self.comments[review_id] = [
                {
                    **copy.deepcopy(comment),
                    "pull_request_review_id": review_id,
                    "user": copy.deepcopy(self.actor),
                }
                for comment in payload.get("comments", [])
            ]
            if self.accept_then_fail:
                self.accept_then_fail = False
                raise review_publisher.GitHubApiError(0, "response lost")
            return copy.deepcopy(review)
        if method == "GET" and "/comments?" in path:
            review_id = int(path.split("/reviews/", 1)[1].split("/", 1)[0])
            page = int(path.rsplit("page=", 1)[1])
            if page > 1:
                return []
            if self.drop_comments_on_readback:
                return []
            return copy.deepcopy(self.comments[review_id])
        if method == "GET" and "/reviews/" in path:
            if self.fail_review_readback:
                raise review_publisher.GitHubApiError(422, "readback rejected")
            review_id = int(path.rsplit("/", 1)[1])
            return copy.deepcopy(
                next(review for review in self.reviews if review["id"] == review_id)
            )
        raise AssertionError(f"unexpected request: {method} {path}")


def publish_with(fake: FakeGitHubClient, findings: list[dict] | None = None):
    result, request, summary = publication_plan(findings)
    with patch.object(review_publisher, "GitHubClient", return_value=fake):
        return review_publisher.publish(
            result=result,
            request=request,
            summary_request=summary,
            repository=REPOSITORY,
            run_id=RUN_ID,
            token="short-lived-token",
        )


class DancerPublisherTests(unittest.TestCase):
    def test_non_comment_event_in_either_request_has_zero_authority(self):
        for target_name in ("request", "summary"):
            for event in ("APPROVE", "REQUEST_CHANGES"):
                with self.subTest(target=target_name, event=event):
                    fake = FakeGitHubClient()
                    result, request, summary = publication_plan([finding()])
                    target = request if target_name == "request" else summary
                    target["event"] = event

                    with patch.object(
                        review_publisher, "GitHubClient", return_value=fake
                    ):
                        published_result, receipt = review_publisher.publish(
                            result=result,
                            request=request,
                            summary_request=summary,
                            repository=REPOSITORY,
                            run_id=RUN_ID,
                            token="short-lived-token",
                        )

                    self.assertEqual(
                        published_result["publication"]["fallback_reason"],
                        "PUBLICATION_REQUEST_INVALID",
                    )
                    self.assertEqual(fake.post_count, 0)
                    self.assertIsNone(receipt["actor"])
                    self.assertIsNone(receipt["review"])

    def test_missing_dancer_authority_fails_without_mutation(self):
        result, request, summary = publication_plan()

        result, receipt = review_publisher.publish(
            result=result,
            request=request,
            summary_request=summary,
            repository=REPOSITORY,
            run_id=RUN_ID,
            token="",
        )

        self.assertEqual(result["publication"]["status"], "failed")
        self.assertEqual(
            result["publication"]["fallback_reason"], "DANCER_AUTH_UNAVAILABLE"
        )
        self.assertIsNone(receipt["actor"])
        self.assertIsNone(receipt["review"])

    def test_clean_summary_is_dancer_authored_and_read_back(self):
        fake = FakeGitHubClient()

        result, receipt = publish_with(fake)

        self.assertEqual(result["publication"]["status"], "published")
        self.assertEqual(result["publication"]["mode"], "summary")
        self.assertEqual(receipt["actor"], fake.actor)
        self.assertEqual(receipt["event"], "COMMENT")
        self.assertEqual(receipt["review"]["reused"], False)
        self.assertEqual(fake.post_count, 1)
        self.assertIn(review_publisher.PUBLICATION_MARKER, fake.reviews[0]["body"])

    def test_inline_finding_and_comment_are_both_dancer_authored(self):
        fake = FakeGitHubClient()

        result, receipt = publish_with(fake, [finding()])

        self.assertEqual(result["publication"]["mode"], "inline")
        self.assertEqual(result["publication"]["inline_comment_count"], 1)
        review_id = receipt["review"]["id"]
        self.assertEqual(fake.comments[review_id][0]["user"], fake.actor)
        self.assertNotIn(finding()["fingerprint"], fake.comments[review_id][0]["body"])

    def test_identical_request_reuses_exact_review_and_comment_readback(self):
        fake = FakeGitHubClient()
        result, request, summary = publication_plan([finding()])

        with patch.object(review_publisher, "GitHubClient", return_value=fake):
            first_result, first_receipt = review_publisher.publish(
                result=copy.deepcopy(result),
                request=request,
                summary_request=summary,
                repository=REPOSITORY,
                run_id=RUN_ID,
                token="short-lived-token",
            )
            second_result, second_receipt = review_publisher.publish(
                result=copy.deepcopy(result),
                request=request,
                summary_request=summary,
                repository=REPOSITORY,
                run_id=RUN_ID,
                token="short-lived-token",
            )

        self.assertEqual(first_result["publication"], second_result["publication"])
        self.assertEqual(first_receipt["review"]["id"], second_receipt["review"]["id"])
        self.assertTrue(second_receipt["review"]["reused"])
        self.assertEqual(fake.post_count, 1)

    def test_identical_request_revalidates_after_reuse_readback(self):
        fake = FakeGitHubClient()
        result, request, summary = publication_plan([finding()])

        with patch.object(review_publisher, "GitHubClient", return_value=fake):
            review_publisher.publish(
                result=copy.deepcopy(result),
                request=request,
                summary_request=summary,
                repository=REPOSITORY,
                run_id=RUN_ID,
                token="short-lived-token",
            )
            fake.change_head_at_pull_read = 4
            reused_result, reused_receipt = review_publisher.publish(
                result=copy.deepcopy(result),
                request=request,
                summary_request=summary,
                repository=REPOSITORY,
                run_id=RUN_ID,
                token="short-lived-token",
            )

        self.assertEqual(reused_result["publication"]["status"], "published")
        self.assertEqual(reused_result["publication"]["mode"], "summary")
        self.assertEqual(
            reused_result["publication"]["fallback_reason"],
            "STALE_BEFORE_PUBLICATION",
        )
        self.assertFalse(reused_receipt["review"]["reused"])
        self.assertEqual(fake.post_count, 2)

    def test_lost_post_response_recovers_without_second_mutation(self):
        fake = FakeGitHubClient()
        fake.accept_then_fail = True

        result, receipt = publish_with(fake)

        self.assertEqual(result["publication"]["status"], "published")
        self.assertTrue(receipt["review"]["reused"])
        self.assertEqual(fake.post_count, 1)

    def test_wrong_actor_fails_without_actions_or_human_fallback(self):
        fake = FakeGitHubClient()
        fake.actor = {"login": "github-actions[bot]", "id": 41898282}

        result, receipt = publish_with(fake)

        self.assertEqual(result["publication"]["status"], "failed")
        self.assertEqual(
            result["publication"]["fallback_reason"], "DANCER_ACTOR_MISMATCH"
        )
        self.assertIsNone(receipt["review"])
        self.assertEqual(fake.post_count, 0)

    def test_stale_generation_publishes_complete_unbound_summary(self):
        fake = FakeGitHubClient()
        fake.head_sha = NEXT_HEAD_SHA

        result, receipt = publish_with(fake, [finding()])

        self.assertEqual(result["publication"]["status"], "published")
        self.assertEqual(result["publication"]["mode"], "summary")
        self.assertEqual(
            result["publication"]["fallback_reason"], "STALE_BEFORE_PUBLICATION"
        )
        self.assertEqual(result["verdict"], "error")
        self.assertIn("Wrong fallback", fake.reviews[0]["body"])
        self.assertIn("> [!CAUTION]", fake.reviews[0]["body"])
        self.assertNotIn(finding()["fingerprint"], fake.reviews[0]["body"])
        self.assertEqual(receipt["review"]["commit_id"], NEXT_HEAD_SHA)

    def test_stale_clean_generation_never_publishes_a_clean_note(self):
        fake = FakeGitHubClient()
        fake.head_sha = NEXT_HEAD_SHA

        result, receipt = publish_with(fake)

        self.assertEqual(result["publication"]["status"], "published")
        self.assertEqual(
            result["publication"]["fallback_reason"], "STALE_BEFORE_PUBLICATION"
        )
        body = fake.reviews[0]["body"]
        self.assertIn("> [!CAUTION]", body)
        self.assertIn("no current clean conclusion is available", body)
        self.assertNotIn("> [!NOTE]", body)
        self.assertNotIn("No issues found.", body)
        self.assertEqual(receipt["review"]["commit_id"], NEXT_HEAD_SHA)

    def test_inline_422_revalidates_and_publishes_complete_summary_once(self):
        fake = FakeGitHubClient()
        fake.inline_422_once = True

        result, receipt = publish_with(fake, [finding()])

        self.assertEqual(result["publication"]["status"], "published")
        self.assertEqual(result["publication"]["mode"], "summary")
        self.assertEqual(result["publication"]["fallback_reason"], "GITHUB_422")
        self.assertEqual(fake.post_count, 2)
        self.assertIn("Wrong fallback", fake.reviews[0]["body"])
        self.assertEqual(receipt["mode"], "summary")

    def test_lookup_or_evidence_422_never_authorizes_a_summary_mutation(self):
        cases = ("state", "evidence")
        for case in cases:
            with self.subTest(case=case):
                fake = FakeGitHubClient()
                if case == "state":
                    fake.fail_commit_read_at = 2
                else:
                    fake.fail_review_list = True

                result, receipt = publish_with(fake, [finding()])

                self.assertEqual(result["publication"]["status"], "failed")
                self.assertEqual(fake.post_count, 0)
                self.assertIsNone(receipt["review"])

    def test_readback_422_fails_after_one_inline_mutation_without_fallback(self):
        fake = FakeGitHubClient()
        fake.fail_review_readback = True

        result, receipt = publish_with(fake, [finding()])

        self.assertEqual(result["publication"]["status"], "failed")
        self.assertEqual(fake.post_count, 1)
        self.assertEqual(len(fake.reviews[0].get("body", "")) > 0, True)
        self.assertEqual(len(fake.reviews), 1)
        self.assertIsNone(receipt["review"])

    def test_missing_inline_comment_readback_fails_closed(self):
        fake = FakeGitHubClient()
        fake.drop_comments_on_readback = True

        result, receipt = publish_with(fake, [finding()])

        self.assertEqual(result["publication"]["status"], "failed")
        self.assertEqual(
            result["publication"]["fallback_reason"], "PUBLICATION_READBACK_FAILED"
        )
        self.assertIsNone(receipt["review"])

    def test_generation_change_during_evidence_read_uses_stale_summary(self):
        fake = FakeGitHubClient()
        fake.change_head_at_pull_read = 2

        result, receipt = publish_with(fake)

        self.assertEqual(result["publication"]["status"], "published")
        self.assertEqual(result["publication"]["mode"], "summary")
        self.assertEqual(
            result["publication"]["fallback_reason"], "STALE_BEFORE_PUBLICATION"
        )
        self.assertEqual(fake.post_count, 1)
        self.assertIn("> [!CAUTION]", fake.reviews[0]["body"])
        self.assertNotIn("> [!NOTE]", fake.reviews[0]["body"])
        self.assertIsNotNone(receipt["review"])


if __name__ == "__main__":
    unittest.main()
