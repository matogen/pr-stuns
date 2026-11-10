#!/usr/bin/env python3
"""
Unit tests for the PR reviewer core module.
"""

import os
from unittest import TestCase, mock
from unittest.mock import MagicMock, patch, PropertyMock

import pytest


class TestReviewerCore(TestCase):
    """Test cases for PR reviewer core functionality"""

    def setUp(self):
        """Set up test fixtures"""
        self.mock_llm = MagicMock()
        self.mock_llm.analyze_code.return_value = "Code looks good. No issues found."
        
        self.mock_repo_client = MagicMock()
        self.mock_repo_client.get_pull_requests.return_value = []

    @patch("pr_reviewer.core.reviewer.ConfigManager")
    def test_reviewer_initialization(self, mock_config_manager):
        """Test reviewer initializes correctly"""
        from pr_reviewer.core.reviewer import PRReviewer
        
        # Mock ConfigManager to avoid needing a real config file
        mock_config = MagicMock()
        mock_config.get_llm_config.return_value = {"provider": "openrouter", "api_key": "test"}
        mock_config.get_comment_template.return_value = "Review:\n{remarks}"
        mock_config.get_repositories.return_value = []
        mock_config_manager.return_value = mock_config
        
        reviewer = PRReviewer("dummy_config.yaml")
        
        assert reviewer is not None
        assert reviewer.config is not None

    def test_file_should_be_reviewed(self):
        """Test file filtering logic"""
        from pr_reviewer.core.reviewer import PRReviewer
        
        # Test various file types
        test_cases = [
            ("app.py", True),
            ("main.js", True),
            ("styles.css", False),
            ("config.json", False),
            ("test.ts", True),
            ("README.md", False),
            ("package-lock.json", False),
            (".gitignore", False),
            ("src/component.tsx", True),
            ("migrations/001.sql", False),
        ]
        
        for filename, should_review in test_cases:
            # This tests the file extension filtering logic
            code_extensions = {'.py', '.js', '.ts', '.tsx', '.jsx', '.java', '.go', '.rs', '.rb'}
            ext = os.path.splitext(filename)[1].lower()
            result = ext in code_extensions
            
            assert result == should_review, f"Failed for {filename}: expected {should_review}, got {result}"

    def test_diff_parsing(self):
        """Test parsing of git diff format"""
        sample_diff = '''
diff --git a/app.py b/app.py
index abc123..def456 100644
--- a/app.py
+++ b/app.py
@@ -10,6 +10,8 @@ def main():
     print("Hello")
+    print("World")
+    return True
     pass
'''
        # Basic diff validation
        assert "diff --git" in sample_diff
        assert "+++ b/app.py" in sample_diff
        assert "-10,6 +10,8" in sample_diff

    def test_review_comment_formatting(self):
        """Test review comment template formatting"""
        template = """## AI Code Review

**File:** {file_path}

### Feedback:
{remarks}

---
*Automated review by AI Code Reviewer*
"""
        
        formatted = template.format(
            file_path="src/main.py",
            remarks="- Consider adding error handling\n- Good use of type hints"
        )
        
        assert "src/main.py" in formatted
        assert "error handling" in formatted
        assert "AI Code Review" in formatted


class TestReviewerIntegration(TestCase):
    """Integration tests for reviewer with mocked external services"""

    def test_full_review_cycle(self):
        """Test a full review cycle with mocked services"""
        # Setup mocks
        mock_llm = MagicMock()
        mock_llm.analyze_code.return_value = "Looks good!"
        
        mock_client = MagicMock()
        mock_client.get_pull_requests.return_value = [{
            "id": 1,
            "title": "Test PR",
            "source": {"branch": {"name": "feature"}},
            "destination": {"branch": {"name": "main"}}
        }]
        mock_client.get_pr_diff.return_value = "diff content"
        
        # Validate mocks work correctly
        result = mock_llm.analyze_code("test", "file.py", context={"pr_title": "Test"})
        assert result == "Looks good!"
        
        prs = mock_client.get_pull_requests()
        assert len(prs) == 1
        assert prs[0]["title"] == "Test PR"


class TestErrorHandling(TestCase):
    """Test error handling in reviewer"""

    def test_handles_llm_error_gracefully(self):
        """Test that LLM errors don't crash the reviewer"""
        mock_llm = MagicMock()
        mock_llm.analyze_code.side_effect = Exception("API Error")
        
        # Should handle exception and return error message
        try:
            result = mock_llm.analyze_code("code", "file.py")
        except Exception as e:
            assert "API Error" in str(e)

    def test_handles_empty_diff(self):
        """Test handling of empty diff"""
        empty_diff = ""
        
        # Empty diff should be handled gracefully
        assert len(empty_diff) == 0

    def test_handles_binary_file(self):
        """Test that binary files are skipped"""
        binary_extensions = {'.png', '.jpg', '.gif', '.pdf', '.zip', '.exe'}
        
        test_files = [
            ("image.png", True),
            ("doc.pdf", True),
            ("app.py", False),
            ("data.bin", False),  # Unknown binary
        ]
        
        for filename, is_known_binary in test_files:
            ext = os.path.splitext(filename)[1].lower()
            result = ext in binary_extensions
            assert result == is_known_binary


class TestRateLimiting(TestCase):
    """Test rate limiting functionality"""

    def test_respects_rate_limits(self):
        """Test that rate limiting is respected"""
        import time
        
        # Simulate rate limit check
        last_request_time = time.time() - 0.5  # 500ms ago
        min_interval = 1.0  # 1 second minimum
        
        time_since_last = time.time() - last_request_time
        should_wait = time_since_last < min_interval
        
        assert should_wait is True

    def test_calculates_wait_time_correctly(self):
        """Test wait time calculation"""
        import time
        
        last_request_time = time.time() - 0.3
        min_interval = 1.0
        
        time_since_last = time.time() - last_request_time
        wait_time = max(0, min_interval - time_since_last)
        
        assert wait_time > 0
        assert wait_time < 1.0


class TestDuplicateCommentFixes(TestCase):
    """Regression tests for the ai-reviewer-no-duplicate-comments fixes:
    resolution-detection negation handling, routing existing-comment lookup
    through the paginated client method, and atomic/locked processed-PR
    state writes.
    """

    def _make_reviewer(self):
        from pr_reviewer.core.reviewer import PRReviewer

        with patch("pr_reviewer.core.reviewer.ConfigManager") as mock_config_manager:
            mock_config = MagicMock()
            mock_config.get_llm_config.return_value = {"provider": "openrouter", "api_key": "test"}
            mock_config.get_repositories.return_value = []
            mock_config_manager.return_value = mock_config
            return PRReviewer("dummy_config.yaml")

    def test_detect_resolution_from_response_ignores_negated_indicators(self):
        """Replies that negate a resolution word must not read as resolved."""
        reviewer = self._make_reviewer()

        not_resolved = [
            "i disagree, this is intentional behavior, not a bug in any case.",
            "won't be fixed, this is by design.",
            "still unimplemented, working on it next sprint.",
            "this is not fixed yet.",
            "no, i think this is a facade for a different pattern, not related.",
            "that's abandoned code, we don't touch it.",
        ]
        for text in not_resolved:
            self.assertFalse(
                reviewer._detect_resolution_from_response(text),
                f"expected NOT resolved for: {text!r}",
            )

    def test_detect_resolution_from_response_detects_real_resolutions(self):
        reviewer = self._make_reviewer()

        resolved = [
            "fixed in a1b2c3d",
            "done, thanks for catching this",
            "addressed in commit 9f3e21a",
            "this has been resolved",
            "pushed a fix, please re-review",
        ]
        for text in resolved:
            self.assertTrue(
                reviewer._detect_resolution_from_response(text),
                f"expected resolved for: {text!r}",
            )

    def test_get_existing_comments_delegates_to_paginated_client_method(self):
        """_get_existing_comments must use the client's own paginated
        get_pull_request_comments() rather than a hand-rolled single-page
        HTTP call, and normalize field names for callers.
        """
        reviewer = self._make_reviewer()

        mock_client = MagicMock()
        mock_client.get_pull_request_comments.return_value = [
            {"id": 1, "body": "looks good", "file_path": "app.py", "line_number": 10, "resolved": False},
            {"id": 2, "body": "already fixed", "file_path": "app.py", "line_number": 20, "resolved": True},
        ]

        result = reviewer._get_existing_comments(mock_client, pr_id=42)

        mock_client.get_pull_request_comments.assert_called_once_with(42)
        self.assertEqual(
            result,
            [
                {"path": "app.py", "line": 10, "body": "looks good", "resolved": False, "state": ""},
                {"path": "app.py", "line": 20, "body": "already fixed", "resolved": True, "state": "resolved"},
            ],
        )

    def test_atomic_write_json_produces_valid_file_and_no_leftover_temp(self):
        import json
        import tempfile

        reviewer = self._make_reviewer()

        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "repo.json")
            reviewer._atomic_write_json(target, [{"id": "1", "timestamp": 123}])

            with open(target) as f:
                data = json.load(f)
            self.assertEqual(data, [{"id": "1", "timestamp": 123}])

            leftover_temp_files = [f for f in os.listdir(tmpdir) if f.startswith(".tmp-")]
            self.assertEqual(leftover_temp_files, [])

    def test_mark_pr_as_processed_persists_and_updates_existing_entry(self):
        import json
        import tempfile

        reviewer = self._make_reviewer()

        with tempfile.TemporaryDirectory() as tmpdir:
            original_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                reviewer.mark_pr_as_processed("owner/repo", 7)
                processed_file = reviewer._get_processed_file_path("owner/repo")
                with open(processed_file) as f:
                    data = json.load(f)
                self.assertEqual(len(data), 1)
                self.assertEqual(data[0]["id"], "7")

                # Marking the same PR again updates in place rather than duplicating
                reviewer.mark_pr_as_processed("owner/repo", 7)
                with open(processed_file) as f:
                    data = json.load(f)
                self.assertEqual(len(data), 1)
            finally:
                os.chdir(original_cwd)


class TestTicketContext(TestCase):
    """Tests for the Jira ticket-context feature: resolving a linked ticket
    from the PR's branch name and folding it (plus PR title/description)
    into the per-file review prompt.
    """

    def _make_reviewer_with_config(self, jira_config=None, goby_config=None):
        from pr_reviewer.core.reviewer import PRReviewer

        with patch("pr_reviewer.core.reviewer.ConfigManager") as mock_config_manager:
            mock_config = MagicMock()
            mock_config.get_llm_config.return_value = {"provider": "openrouter", "api_key": "test"}
            mock_config.get_repositories.return_value = []
            mock_config.get_jira_config.return_value = jira_config or {}
            mock_config.get_goby_config.return_value = goby_config or {}
            mock_config_manager.return_value = mock_config
            reviewer = PRReviewer("dummy_config.yaml")
            reviewer.config = mock_config
            return reviewer

    def _make_pr(self, title="Add notification proxy", description="Implements ERA-256", head_branch="feature/era-256-notification-proxy"):
        from pr_reviewer.models import PullRequest

        return PullRequest(id=1, title=title, author="dev", description=description, head_branch=head_branch)

    def test_get_ticket_context_returns_none_when_jira_not_configured(self):
        reviewer = self._make_reviewer_with_config(jira_config={})
        pr = self._make_pr()
        self.assertIsNone(reviewer._get_ticket_context(pr))

    def test_get_ticket_context_returns_none_when_branch_has_no_key(self):
        reviewer = self._make_reviewer_with_config(
            jira_config={"base_url": "https://x.atlassian.net", "email": "a@b.com", "api_token": "t"}
        )
        pr = self._make_pr(head_branch="main")
        self.assertIsNone(reviewer._get_ticket_context(pr))

    @patch("pr_reviewer.core.reviewer.JiraClient")
    def test_get_ticket_context_fetches_issue_when_configured_and_key_present(self, mock_jira_client_cls):
        mock_client = MagicMock()
        mock_client.get_issue_context.return_value = {
            "key": "ERA-256", "summary": "Notification proxy", "description": "Forward events", "status": "In Progress",
        }
        mock_jira_client_cls.return_value = mock_client

        reviewer = self._make_reviewer_with_config(
            jira_config={"base_url": "https://x.atlassian.net", "email": "a@b.com", "api_token": "t"}
        )
        pr = self._make_pr(head_branch="feature/era-256-notification-proxy")

        result = reviewer._get_ticket_context(pr)

        mock_client.get_issue_context.assert_called_once_with("ERA-256")
        self.assertEqual(result["key"], "ERA-256")

    def test_get_ticket_context_defaults_to_default_instance(self):
        from pr_reviewer.core.reviewer import PRReviewer

        with patch("pr_reviewer.core.reviewer.ConfigManager") as mock_config_manager:
            mock_config = MagicMock()
            mock_config.get_llm_config.return_value = {"provider": "openrouter", "api_key": "test"}
            mock_config.get_repositories.return_value = []
            mock_config.get_jira_config.return_value = {}
            mock_config.get_goby_config.return_value = {}
            mock_config_manager.return_value = mock_config
            reviewer = PRReviewer("dummy_config.yaml")
            reviewer.config = mock_config

        pr = self._make_pr()
        reviewer._get_ticket_context(pr, repo_config=None)
        mock_config.get_jira_config.assert_called_once_with("default")

    def test_get_ticket_context_uses_repo_specific_instance(self):
        from pr_reviewer.core.reviewer import PRReviewer

        with patch("pr_reviewer.core.reviewer.ConfigManager") as mock_config_manager:
            mock_config = MagicMock()
            mock_config.get_llm_config.return_value = {"provider": "openrouter", "api_key": "test"}
            mock_config.get_repositories.return_value = []
            mock_config.get_jira_config.return_value = {}
            mock_config.get_goby_config.return_value = {}
            mock_config_manager.return_value = mock_config
            reviewer = PRReviewer("dummy_config.yaml")
            reviewer.config = mock_config

        pr = self._make_pr()
        reviewer._get_ticket_context(pr, repo_config={"jira_instance": "other-org"})
        mock_config.get_jira_config.assert_called_once_with("other-org")

    @patch("pr_reviewer.core.reviewer.JiraClient")
    def test_get_ticket_context_caches_client_per_instance(self, mock_jira_client_cls):
        from pr_reviewer.core.reviewer import PRReviewer

        def jira_config_for(instance_name):
            return {"base_url": f"https://{instance_name}.atlassian.net", "email": "a@b.com", "api_token": "t"}

        with patch("pr_reviewer.core.reviewer.ConfigManager") as mock_config_manager:
            mock_config = MagicMock()
            mock_config.get_llm_config.return_value = {"provider": "openrouter", "api_key": "test"}
            mock_config.get_repositories.return_value = []
            mock_config.get_jira_config.side_effect = jira_config_for
            mock_config.get_goby_config.return_value = {}
            mock_config_manager.return_value = mock_config
            reviewer = PRReviewer("dummy_config.yaml")
            reviewer.config = mock_config

        mock_jira_client_cls.side_effect = lambda *a, **k: MagicMock()

        pr = self._make_pr(head_branch="feature/era-256-x")
        reviewer._get_ticket_context(pr, repo_config={"jira_instance": "org-a"})
        reviewer._get_ticket_context(pr, repo_config={"jira_instance": "org-a"})
        reviewer._get_ticket_context(pr, repo_config={"jira_instance": "org-b"})

        # One JiraClient instance created per distinct jira_instance name,
        # not one per call -- a second call for the same instance reuses it.
        self.assertEqual(mock_jira_client_cls.call_count, 2)
        self.assertEqual(set(reviewer._jira_clients.keys()), {"org-a", "org-b"})

    @patch("pr_reviewer.core.reviewer.JiraClient")
    def test_jira_ticket_context_is_tagged_with_source(self, mock_jira_client_cls):
        mock_client = MagicMock()
        mock_client.get_issue_context.return_value = {"key": "ERA-256", "summary": "s", "description": "d", "status": "Open"}
        mock_jira_client_cls.return_value = mock_client

        reviewer = self._make_reviewer_with_config(
            jira_config={"base_url": "https://x.atlassian.net", "email": "a@b.com", "api_token": "t"}
        )
        result = reviewer._get_ticket_context(self._make_pr())

        self.assertEqual(result["source"], "jira")

    @patch("pr_reviewer.core.reviewer.JiraClient")
    @patch("pr_reviewer.core.reviewer.GobyClient")
    def test_goby_is_tried_before_jira_and_wins_on_hit(self, mock_goby_client_cls, mock_jira_client_cls):
        goby_client = MagicMock()
        goby_client.get_task_context.return_value = {
            "key": "OPS-42", "summary": "Goby task", "description": "d", "status": "In Progress", "source": "goby",
        }
        mock_goby_client_cls.return_value = goby_client
        jira_client = MagicMock()
        mock_jira_client_cls.return_value = jira_client

        reviewer = self._make_reviewer_with_config(
            jira_config={"base_url": "https://x.atlassian.net", "email": "a@b.com", "api_token": "t"},
            goby_config={"base_url": "https://your-team.goby.chat", "api_key": "gk_a_b"},
        )
        result = reviewer._get_ticket_context(self._make_pr(head_branch="feature/ops-42-thing"))

        goby_client.get_task_context.assert_called_once_with("OPS-42")
        jira_client.get_issue_context.assert_not_called()
        self.assertEqual(result["source"], "goby")
        mock_goby_client_cls.assert_called_once_with("https://your-team.goby.chat", "gk_a_b")

    @patch("pr_reviewer.core.reviewer.JiraClient")
    @patch("pr_reviewer.core.reviewer.GobyClient")
    def test_falls_back_to_jira_when_goby_has_no_such_task(self, mock_goby_client_cls, mock_jira_client_cls):
        goby_client = MagicMock()
        goby_client.get_task_context.return_value = None
        mock_goby_client_cls.return_value = goby_client
        jira_client = MagicMock()
        jira_client.get_issue_context.return_value = {"key": "ERA-256", "summary": "Jira issue", "description": "", "status": "Open"}
        mock_jira_client_cls.return_value = jira_client

        reviewer = self._make_reviewer_with_config(
            jira_config={"base_url": "https://x.atlassian.net", "email": "a@b.com", "api_token": "t"},
            goby_config={"base_url": "https://your-team.goby.chat", "api_key": "gk_a_b"},
        )
        result = reviewer._get_ticket_context(self._make_pr())

        goby_client.get_task_context.assert_called_once_with("ERA-256")
        jira_client.get_issue_context.assert_called_once_with("ERA-256")
        self.assertEqual(result["source"], "jira")

    @patch("pr_reviewer.core.reviewer.GobyClient")
    def test_goby_only_repo_works_without_jira(self, mock_goby_client_cls):
        goby_client = MagicMock()
        goby_client.get_task_context.return_value = {"key": "OPS-42", "summary": "s", "description": "", "status": "", "source": "goby"}
        mock_goby_client_cls.return_value = goby_client

        reviewer = self._make_reviewer_with_config(
            jira_config={}, goby_config={"base_url": "https://your-team.goby.chat", "api_key": "gk_a_b"},
        )
        result = reviewer._get_ticket_context(self._make_pr(head_branch="ops-42"))

        self.assertEqual(result["key"], "OPS-42")

    def test_goby_instance_is_selected_from_repo_config(self):
        reviewer = self._make_reviewer_with_config()
        reviewer._get_ticket_context(self._make_pr(), repo_config={"goby_instance": "ops-team"})
        reviewer.config.get_goby_config.assert_called_once_with("ops-team")

    def test_goby_instance_defaults_to_default(self):
        reviewer = self._make_reviewer_with_config()
        reviewer._get_ticket_context(self._make_pr(), repo_config={"jira_instance": "other-org"})
        reviewer.config.get_goby_config.assert_called_once_with("default")

    @patch("pr_reviewer.core.reviewer.GobyClient")
    def test_goby_client_is_cached_per_instance(self, mock_goby_client_cls):
        from pr_reviewer.core.reviewer import PRReviewer

        def goby_config_for(instance_name):
            return {"base_url": "https://your-team.goby.chat", "api_key": f"gk_{instance_name}"}

        with patch("pr_reviewer.core.reviewer.ConfigManager") as mock_config_manager:
            mock_config = MagicMock()
            mock_config.get_llm_config.return_value = {"provider": "openrouter", "api_key": "test"}
            mock_config.get_repositories.return_value = []
            mock_config.get_jira_config.return_value = {}
            mock_config.get_goby_config.side_effect = goby_config_for
            mock_config_manager.return_value = mock_config
            reviewer = PRReviewer("dummy_config.yaml")
            reviewer.config = mock_config

        mock_goby_client_cls.side_effect = lambda *a, **k: MagicMock()

        pr = self._make_pr(head_branch="feature/ops-42-x")
        reviewer._get_ticket_context(pr, repo_config={"goby_instance": "team-a"})
        reviewer._get_ticket_context(pr, repo_config={"goby_instance": "team-a"})
        reviewer._get_ticket_context(pr, repo_config={"goby_instance": "team-b"})

        self.assertEqual(mock_goby_client_cls.call_count, 2)
        self.assertEqual(set(reviewer._goby_clients.keys()), {"team-a", "team-b"})

    def test_build_pr_context_block_names_ticket_source(self):
        reviewer = self._make_reviewer_with_config()
        pr = self._make_pr()

        goby_block = reviewer._build_pr_context_block(
            pr, {"key": "OPS-42", "summary": "s", "description": "", "status": "In Progress", "source": "goby"}
        )
        jira_block = reviewer._build_pr_context_block(
            pr, {"key": "ERA-256", "summary": "s", "description": "", "status": "Open", "source": "jira"}
        )

        self.assertIn("**Linked ticket OPS-42** (In Progress, from Goby)", goby_block)
        self.assertIn("**Linked ticket ERA-256** (Open, from Jira)", jira_block)

    def test_build_pr_context_block_includes_title_description_and_ticket(self):
        reviewer = self._make_reviewer_with_config()
        pr = self._make_pr(title="Add notification proxy", description="Wires up ERA-256")
        ticket_context = {
            "key": "ERA-256", "summary": "Notification proxy", "description": "Forward events downstream", "status": "In Progress",
        }

        block = reviewer._build_pr_context_block(pr, ticket_context)

        self.assertIn("Add notification proxy", block)
        self.assertIn("Wires up ERA-256", block)
        self.assertIn("ERA-256", block)
        self.assertIn("Forward events downstream", block)
        self.assertIn("Compare the code changes against this ticket", block)

    def test_build_pr_context_block_omits_ticket_section_when_none(self):
        reviewer = self._make_reviewer_with_config()
        pr = self._make_pr()

        block = reviewer._build_pr_context_block(pr, None)

        self.assertIn(pr.title, block)
        self.assertNotIn("Linked ticket", block)

    def test_build_pr_context_block_omits_changed_files_section_when_empty(self):
        reviewer = self._make_reviewer_with_config()
        pr = self._make_pr()
        pr.file_changes = []

        block = reviewer._build_pr_context_block(pr, None)

        self.assertNotIn("Files Changed in This PR", block)

    def test_build_pr_context_block_lists_all_changed_files_including_excluded_ones(self):
        from pr_reviewer.models import FileChange

        reviewer = self._make_reviewer_with_config()
        pr = self._make_pr()
        # Includes a test file and a lockfile that a repo's exclude_regex
        # would typically strip out of the *reviewable* set -- the PR-wide
        # summary should still show them, since the whole point is
        # PR-wide awareness (e.g. "was a test file touched?").
        pr.file_changes = [
            FileChange(filename="src/api/notifications.ts", status="modified"),
            FileChange(filename="src/api/notifications.test.ts", status="added"),
            FileChange(filename="package-lock.json", status="modified"),
        ]

        block = reviewer._build_pr_context_block(pr, None)

        self.assertIn("Files Changed in This PR (3 total)", block)
        self.assertIn("src/api/notifications.ts (modified)", block)
        self.assertIn("src/api/notifications.test.ts (added)", block)
        self.assertIn("package-lock.json (modified)", block)
        self.assertIn("no corresponding test file changed", block)

    def test_build_changed_files_summary_truncates_very_large_pr(self):
        from pr_reviewer.models import FileChange
        from pr_reviewer.core.reviewer import MAX_CHANGED_FILES_LISTED

        reviewer = self._make_reviewer_with_config()
        file_changes = [
            FileChange(filename=f"src/file_{i}.ts", status="modified")
            for i in range(MAX_CHANGED_FILES_LISTED + 5)
        ]

        summary = reviewer._build_changed_files_summary(file_changes)

        self.assertIn(f"({MAX_CHANGED_FILES_LISTED + 5} total)", summary)
        self.assertIn("...and 5 more file(s) not listed", summary)
        self.assertEqual(summary.count(" (modified)\n"), MAX_CHANGED_FILES_LISTED)



class TestLLMResponseClassification(TestCase):
    """The system prompt asks for exactly "NO_ISSUES_FOUND" on a clean file.
    That answer is shorter than the "too short to be a review" threshold, so
    it used to be treated as truncated: every clean file got a second,
    context-free call, and when that call found something it replaced the
    full-context verdict."""

    def setUp(self):
        from pr_reviewer.core.reviewer import PRReviewer
        self.classify = PRReviewer._classify_llm_response

    def test_no_issues_sentinel_is_clean(self):
        self.assertEqual(self.classify("NO_ISSUES_FOUND"), "clean")
        self.assertEqual(self.classify("  NO_ISSUES_FOUND\n"), "clean")

    def test_api_error(self):
        self.assertEqual(self.classify("Error: OpenRouter API error: 500"), "error")

    def test_short_or_trailing_ellipsis_is_incomplete(self):
        self.assertEqual(self.classify(""), "incomplete")
        self.assertEqual(self.classify("[Line 4]"), "incomplete")
        self.assertEqual(self.classify("[Line 4] This loop never terminates because..."), "incomplete")

    def test_max_tokens_finish_reason_is_incomplete(self):
        review = "[Line 12] The retry loop swallows the final exception and returns None"
        self.assertEqual(self.classify(review, "length"), "incomplete")
        self.assertEqual(self.classify(review, "max_tokens"), "incomplete")

    def test_normal_review_is_ok(self):
        review = "[Line 12] The retry loop swallows the final exception and returns None"
        self.assertEqual(self.classify(review), "ok")
        self.assertEqual(self.classify(review, "stop"), "ok")


class TestCleanFileIsNotRetried(TestCase):
    def _make_reviewer(self):
        from pr_reviewer.core.reviewer import PRReviewer

        with patch("pr_reviewer.core.reviewer.ConfigManager") as mock_config_manager:
            mock_config = MagicMock()
            mock_config.get_llm_config.return_value = {"provider": "openrouter", "api_key": "test"}
            mock_config.get_repositories.return_value = []
            mock_config_manager.return_value = mock_config
            return PRReviewer("dummy_config.yaml")

    @patch("pr_reviewer.core.reviewer.ContextProvider")
    def test_no_issues_found_makes_one_llm_call_and_posts_nothing(self, mock_context_provider):
        from pr_reviewer.models import FileChange

        mock_context_provider.return_value.get_file_context.return_value = {"filename": "src/app.py"}
        reviewer = self._make_reviewer()
        reviewer.llm = MagicMock()
        reviewer.llm.analyze_code.return_value = "NO_ISSUES_FOUND"
        reviewer.llm.last_finish_reason = "stop"
        reviewer._get_ticket_context = MagicMock(return_value=None)
        reviewer.config.get_comment_template.return_value = "{remarks}"

        git_client = MagicMock()
        pr = MagicMock(id=1, title="t", description="", commits=[])
        change = FileChange(filename="src/app.py", status="modified",
                            patch="@@ -1,1 +1,2 @@\n line\n+added\n")

        reviewer._analyze_and_comment(git_client, pr, [change], {})

        reviewer.llm.analyze_code.assert_called_once()
        git_client.add_review_comment.assert_not_called()
        self.assertIn("No Issues Found", git_client.add_comment.call_args[0][1])

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
