#!/usr/bin/env python3
"""
Unit tests for the PR flow diagram feature.
"""

import base64
import json
import zlib
from unittest import TestCase
from unittest.mock import MagicMock

from pr_reviewer.core import flow_diagram
from pr_reviewer.core.flow_diagram import (
    build_flow_diagram_prompt,
    build_flow_diagram_section,
    build_image_url,
    extract_mermaid,
    generate_flow_diagram_section,
    resolve_flow_diagram_config,
)
from pr_reviewer.models import FileChange

DIAGRAM = 'flowchart TD\n  A["Start"] --> B{"Valid?"}\n  B -->|"yes"| C["Save"]\n  B -->|"no"| D["Reject"]'


def _changes():
    return [
        FileChange(filename="app/auth.py", status="modified", patch="@@ -1 +1 @@\n-old\n+new"),
        FileChange(filename="logo.png", status="added", patch=None),
    ]


class TestResolveConfig(TestCase):
    def test_disabled_by_default(self):
        self.assertFalse(resolve_flow_diagram_config(None, {})["enabled"])

    def test_non_dict_global_config_is_ignored(self):
        # e.g. a MagicMock config in other tests, or a malformed YAML value
        self.assertFalse(resolve_flow_diagram_config(MagicMock(), None)["enabled"])
        self.assertFalse(resolve_flow_diagram_config("yes", None)["enabled"])

    def test_global_block_applies_with_defaults(self):
        config = resolve_flow_diagram_config({"enabled": True}, {})
        self.assertTrue(config["enabled"])
        self.assertEqual(config["mode"], "image")
        self.assertEqual(config["renderer_url"], "https://mermaid.ink")

    def test_repo_overrides_global(self):
        self.assertFalse(resolve_flow_diagram_config({"enabled": True}, {"flow_diagram": False})["enabled"])
        config = resolve_flow_diagram_config(
            {"enabled": False, "mode": "image"}, {"flow_diagram": {"enabled": True, "mode": "mermaid"}}
        )
        self.assertTrue(config["enabled"])
        self.assertEqual(config["mode"], "mermaid")


class TestPrompt(TestCase):
    def test_prompt_contains_context_and_diffs_only_for_patched_files(self):
        prompt = build_flow_diagram_prompt("**PR title:** Fix login\n", _changes(), 12)
        self.assertIn("Fix login", prompt)
        self.assertIn("app/auth.py", prompt)
        self.assertIn("+new", prompt)
        self.assertNotIn("logo.png", prompt)
        self.assertIn("at most 12 nodes", prompt)
        self.assertIn("NO_DIAGRAM", prompt)

    def test_oversized_file_diff_is_truncated(self):
        big = FileChange(filename="big.py", status="modified", patch="x" * 50000)
        prompt = build_flow_diagram_prompt("", [big], 25)
        self.assertIn("(diff truncated)", prompt)
        self.assertLess(len(prompt), 20000)


class TestExtractMermaid(TestCase):
    def test_extracts_fenced_block(self):
        self.assertEqual(extract_mermaid(f"Here you go:\n```mermaid\n{DIAGRAM}\n```\nDone."), DIAGRAM)

    def test_accepts_unfenced_diagram(self):
        self.assertEqual(extract_mermaid(DIAGRAM), DIAGRAM)

    def test_rejects_non_diagrams(self):
        for response in (None, "", "NO_DIAGRAM", "Error: API request failed",
                         "The code looks fine.", "```mermaid\nflowchart TD\n  A[\"alone\"]\n```",
                         "```mermaid\nsequenceDiagram\n  A->>B: hi\n```"):
            self.assertIsNone(extract_mermaid(response), response)


class TestRendering(TestCase):
    def test_image_url_round_trips_the_diagram(self):
        url = build_image_url(DIAGRAM, "https://diagrams.example.com/")
        prefix = "https://diagrams.example.com/img/pako:"
        self.assertTrue(url.startswith(prefix))
        self.assertTrue(url.endswith("?type=png"))
        encoded = url[len(prefix):-len("?type=png")]
        state = json.loads(zlib.decompress(base64.urlsafe_b64decode(encoded)))
        self.assertEqual(state["code"], DIAGRAM)

    def test_oversized_diagram_gives_no_image(self):
        import random
        random.seed(1)
        noisy = "flowchart TD\n" + "\n".join(
            f'  N{i}["{random.getrandbits(256):x}"] --> N{i + 1}' for i in range(400)
        )
        self.assertIsNone(build_image_url(noisy))
        self.assertIsNone(build_flow_diagram_section(noisy, mode="image"))

    def test_image_section_is_a_markdown_image(self):
        section = build_flow_diagram_section(DIAGRAM, mode="image")
        self.assertIn("## Flow of the changed code", section)
        self.assertIn("![Flow diagram of the changed code](https://mermaid.ink/img/pako:", section)

    def test_mermaid_section_is_a_code_block(self):
        section = build_flow_diagram_section(DIAGRAM, mode="mermaid")
        self.assertIn(f"```mermaid\n{DIAGRAM}\n```", section)
        self.assertNotIn("mermaid.ink", section)


class TestGenerate(TestCase):
    def setUp(self):
        self.llm = MagicMock()
        self.llm.analyze_code.return_value = f"```mermaid\n{DIAGRAM}\n```"
        self.enabled = resolve_flow_diagram_config({"enabled": True}, None)

    def test_disabled_makes_no_llm_call(self):
        config = resolve_flow_diagram_config(None, None)
        self.assertIsNone(generate_flow_diagram_section(self.llm, "", _changes(), config))
        self.llm.analyze_code.assert_not_called()

    def test_no_patches_makes_no_llm_call(self):
        changes = [FileChange(filename="logo.png", status="added", patch=None)]
        self.assertIsNone(generate_flow_diagram_section(self.llm, "", changes, self.enabled))
        self.llm.analyze_code.assert_not_called()

    def test_enabled_returns_image_section(self):
        section = generate_flow_diagram_section(self.llm, "ctx", _changes(), self.enabled)
        self.assertIn("![Flow diagram of the changed code](", section)
        self.llm.analyze_code.assert_called_once()

    def test_model_declining_or_failing_returns_none(self):
        self.llm.analyze_code.return_value = "NO_DIAGRAM"
        self.assertIsNone(generate_flow_diagram_section(self.llm, "", _changes(), self.enabled))
        self.llm.analyze_code.side_effect = RuntimeError("boom")
        self.assertIsNone(generate_flow_diagram_section(self.llm, "", _changes(), self.enabled))


class TestSummaryCommentIntegration(TestCase):
    """The diagram ends up in the summary comment the reviewer posts."""

    def _run_review(self, flow_diagram_config):
        from unittest.mock import patch
        from pr_reviewer.core.reviewer import PRReviewer
        from pr_reviewer.models import PullRequest

        with patch("pr_reviewer.core.reviewer.ConfigManager") as mock_config_manager:
            mock_config = MagicMock()
            mock_config.get_llm_config.return_value = {"model_name": "dummy"}
            mock_config.get_repositories.return_value = []
            mock_config.get_jira_config.return_value = {}
            mock_config.get_goby_config.return_value = {}
            mock_config.get_flow_diagram_config.return_value = flow_diagram_config
            mock_config.get_comment_template.return_value = "### AI Code Review\n\n{remarks}"
            mock_config_manager.return_value = mock_config
            reviewer = PRReviewer("dummy_config.yaml")
            reviewer.config = mock_config

        reviewer.llm = MagicMock()
        reviewer.llm.last_finish_reason = "stop"
        reviewer.llm.analyze_code.side_effect = lambda prompt, file_path, **kwargs: (
            f"```mermaid\n{DIAGRAM}\n```" if file_path == "flow_diagram" else "NO_ISSUES_FOUND"
        )
        git_client = MagicMock()
        git_client.get_pull_request_comments.return_value = []
        git_client.get_file_content.return_value = "new\n"
        pr = PullRequest(id=7, title="Fix login", author="dev", description="", head_branch="main")

        with patch("pr_reviewer.core.reviewer.time.sleep"):
            reviewer._analyze_and_comment(git_client, pr, _changes(), {}, repo_config={"type": "bitbucket"})
        return git_client.add_comment.call_args[0][1]

    def test_enabled_adds_diagram_image_to_summary(self):
        body = self._run_review({"enabled": True})
        self.assertIn("## Flow of the changed code", body)
        self.assertIn("![Flow diagram of the changed code](https://mermaid.ink/img/pako:", body)

    def test_disabled_leaves_summary_untouched(self):
        self.assertNotIn("Flow of the changed code", self._run_review(None))
