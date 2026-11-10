#!/usr/bin/env python3
"""
Unit tests for the Jira ticket-context integration.
"""

from unittest import TestCase
from unittest.mock import MagicMock, patch

import pytest

from pr_reviewer.clients.jira import JiraClient, extract_issue_key, _adf_to_text


class TestExtractIssueKey(TestCase):
    def test_extracts_key_from_typical_branch_names(self):
        cases = [
            ("feature/era-256-notification-proxy", "ERA-256"),
            ("ERA-256-notification-proxy", "ERA-256"),
            ("bugfix/ABC-1", "ABC-1"),
            ("release/v2", None),
            ("main", None),
            ("deps/bump-lodash-1.2.3", "LODASH-1"),  # inherent ambiguity, documented in the function
        ]
        for branch, expected in cases:
            self.assertEqual(extract_issue_key(branch), expected, f"branch={branch!r}")

    def test_none_and_empty_branch(self):
        self.assertIsNone(extract_issue_key(None))
        self.assertIsNone(extract_issue_key(""))


class TestAdfToText(TestCase):
    def test_flattens_nested_text_nodes(self):
        adf = {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Implement the"}]},
                {"type": "paragraph", "content": [{"type": "text", "text": "notification proxy."}]},
            ],
        }
        self.assertEqual(_adf_to_text(adf), "Implement the notification proxy.")

    def test_handles_none_and_plain_string(self):
        self.assertEqual(_adf_to_text(None), "")
        self.assertEqual(_adf_to_text("already plain text"), "already plain text")


class TestJiraClient(TestCase):
    @patch("pr_reviewer.clients.jira.requests.get")
    def test_get_issue_context_parses_fields(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "fields": {
                "summary": "Add router API notification proxy",
                "description": {
                    "type": "doc",
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Must forward events."}]}],
                },
                "status": {"name": "In Progress"},
                "issuetype": {"name": "Story"},
            }
        }
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        client = JiraClient("https://your-org.atlassian.net", "bot@example.com", "token123")
        result = client.get_issue_context("ERA-256")

        self.assertEqual(result["key"], "ERA-256")
        self.assertEqual(result["summary"], "Add router API notification proxy")
        self.assertEqual(result["description"], "Must forward events.")
        self.assertEqual(result["status"], "In Progress")
        mock_get.assert_called_once()
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["auth"], ("bot@example.com", "token123"))

    @patch("pr_reviewer.clients.jira.requests.get")
    def test_get_issue_context_returns_none_on_404(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        client = JiraClient("https://your-org.atlassian.net", "bot@example.com", "token123")
        self.assertIsNone(client.get_issue_context("NOPE-1"))

    @patch("pr_reviewer.clients.jira.requests.get")
    def test_get_issue_context_returns_none_on_request_error(self, mock_get):
        import requests

        mock_get.side_effect = requests.RequestException("boom")

        client = JiraClient("https://your-org.atlassian.net", "bot@example.com", "token123")
        self.assertIsNone(client.get_issue_context("ERA-256"))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
