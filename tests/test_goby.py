#!/usr/bin/env python3
"""
Unit tests for the Goby ticket-context client.
"""

from unittest import TestCase
from unittest.mock import MagicMock, patch

import pytest

from pr_reviewer.clients.goby import GobyClient


def _ok_response(payload):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = payload
    response.raise_for_status = MagicMock()
    return response


class TestGobyClient(TestCase):
    @patch("pr_reviewer.clients.goby.requests.get")
    def test_get_task_context_parses_fields(self, mock_get):
        mock_get.return_value = _ok_response({
            "key": "OPS-42",
            "title": "Add router API notification proxy",
            "description": "Must forward events.",
            "status": {"id": "abc", "name": "In Progress", "semantic": "in_progress"},
            "labels": [{"id": "1", "name": "backend", "color": "#000"}, {"id": "2", "name": "bug", "color": "#f00"}],
            "url": "https://your-team.goby.chat/t/OPS-42",
        })

        client = GobyClient("https://your-team.goby.chat", "gk_pub_secret")
        result = client.get_task_context("OPS-42")

        self.assertEqual(result["key"], "OPS-42")
        self.assertEqual(result["summary"], "Add router API notification proxy")
        self.assertEqual(result["description"], "Must forward events.")
        self.assertEqual(result["status"], "In Progress")
        self.assertEqual(result["issue_type"], "backend, bug")
        self.assertEqual(result["source"], "goby")
        self.assertEqual(result["url"], "https://your-team.goby.chat/t/OPS-42")

    @patch("pr_reviewer.clients.goby.requests.get")
    def test_sends_bearer_header_to_v1_task_url(self, mock_get):
        mock_get.return_value = _ok_response({"key": "OPS-42", "title": "t", "description": None})

        GobyClient("https://your-team.goby.chat", "gk_pub_secret").get_task_context("OPS-42")

        args, kwargs = mock_get.call_args
        self.assertEqual(args[0], "https://your-team.goby.chat/api/v1/tasks/OPS-42")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer gk_pub_secret")

    @patch("pr_reviewer.clients.goby.requests.get")
    def test_accepts_base_url_that_already_includes_api_v1(self, mock_get):
        mock_get.return_value = _ok_response({"key": "OPS-42", "title": "t", "description": None})

        GobyClient("https://your-team.goby.chat/api/v1/", "gk").get_task_context("OPS-42")

        args, _ = mock_get.call_args
        self.assertEqual(args[0], "https://your-team.goby.chat/api/v1/tasks/OPS-42")

    @patch("pr_reviewer.clients.goby.requests.get")
    def test_null_description_and_missing_status_become_empty_strings(self, mock_get):
        mock_get.return_value = _ok_response({"key": "OPS-42", "title": "t", "description": None, "status": None})

        result = GobyClient("https://your-team.goby.chat", "gk").get_task_context("OPS-42")

        self.assertEqual(result["description"], "")
        self.assertEqual(result["status"], "")
        self.assertEqual(result["issue_type"], "")

    @patch("pr_reviewer.clients.goby.requests.get")
    def test_returns_none_on_404(self, mock_get):
        response = MagicMock()
        response.status_code = 404
        mock_get.return_value = response

        self.assertIsNone(GobyClient("https://your-team.goby.chat", "gk").get_task_context("NOPE-1"))

    @patch("pr_reviewer.clients.goby.requests.get")
    def test_returns_none_on_request_error(self, mock_get):
        import requests

        mock_get.side_effect = requests.RequestException("boom")

        self.assertIsNone(GobyClient("https://your-team.goby.chat", "gk").get_task_context("OPS-42"))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
