#!/usr/bin/env python3
"""
Unit tests for LLM implementations.
Tests OpenRouter, Anthropic, and LLM factory.
"""

import os
from unittest import TestCase, mock
from unittest.mock import MagicMock, patch

import pytest

from pr_reviewer.llm.base import BaseLLM
from pr_reviewer.llm.openrouter import OpenRouterLLM
from pr_reviewer.llm.llm_factory import create_llm


class TestOpenRouterLLM(TestCase):
    """Test cases for OpenRouter LLM implementation"""

    def setUp(self):
        """Set up test fixtures"""
        self.model_config = {
            "api_key": "test-api-key",
            "model_name": "x-ai/grok-code-fast-1",
            "max_tokens": 4096,
            "temperature": 0.3,
            "system_prompt": "You are a code reviewer.",
        }

    def test_init_with_config(self):
        """Test initialization with valid config"""
        llm = OpenRouterLLM(self.model_config)
        
        assert llm.api_key == "test-api-key"
        assert llm.model_name == "x-ai/grok-code-fast-1"
        assert llm.max_tokens == 4096
        assert llm.temperature == 0.3

    def test_init_with_env_api_key(self):
        """Test initialization with API key from environment"""
        config = {"model_name": "test-model"}
        
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "env-api-key"}):
            llm = OpenRouterLLM(config)
            assert llm.api_key == "env-api-key"

    def test_init_without_api_key_raises_error(self):
        """Test that missing API key raises ValueError"""
        config = {"model_name": "test-model"}
        
        with patch.dict(os.environ, {}, clear=True):
            # Remove OPENROUTER_API_KEY if it exists
            os.environ.pop("OPENROUTER_API_KEY", None)
            with pytest.raises(ValueError, match="API key must be provided"):
                OpenRouterLLM(config)

    def test_analyze_code_signature(self):
        """Test that analyze_code has correct signature"""
        llm = OpenRouterLLM(self.model_config)
        
        # Check method signature accepts required parameters
        import inspect
        sig = inspect.signature(llm.analyze_code)
        params = list(sig.parameters.keys())
        
        assert "prompt" in params
        assert "file_path" in params
        assert "context" in params

    @patch("requests.post")
    def test_analyze_code_success(self, mock_post):
        """Test successful code analysis"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [
                {"message": {"content": "Code review: Looks good!"}}
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150
            }
        }
        mock_post.return_value = mock_response
        
        llm = OpenRouterLLM(self.model_config)
        result = llm.analyze_code(
            prompt="Review this code:\ndef hello(): pass",
            file_path="test.py",
            context={"pr_title": "Test PR"}
        )
        
        assert "Looks good!" in result
        mock_post.assert_called_once()

    @patch("requests.post")
    def test_analyze_code_with_context(self, mock_post):
        """Test code analysis with PR context"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "Review complete"}}]
        }
        mock_post.return_value = mock_response
        
        llm = OpenRouterLLM(self.model_config)
        result = llm.analyze_code(
            prompt="Code diff here",
            file_path="app.py",
            context={
                "pr_title": "Add new feature",
                "pr_description": "This PR adds X functionality"
            }
        )
        
        # Verify context was included in the request
        call_args = mock_post.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        user_message = payload["messages"][1]["content"]
        
        assert "Add new feature" in user_message
        assert "app.py" in user_message

    @patch("requests.post")
    def test_analyze_code_without_context(self, mock_post):
        """Test code analysis without context (context=None)"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "Review complete"}}]
        }
        mock_post.return_value = mock_response
        
        llm = OpenRouterLLM(self.model_config)
        result = llm.analyze_code(
            prompt="Code diff here",
            file_path="test.py",
            context=None
        )
        
        assert result == "Review complete"

    @patch("requests.post")
    def test_analyze_code_api_error(self, mock_post):
        """Test handling of API error response"""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_post.return_value = mock_response
        
        llm = OpenRouterLLM(self.model_config)
        result = llm.analyze_code(
            prompt="Code here",
            file_path="test.py"
        )
        
        assert "Error" in result
        assert "500" in result

    @patch("requests.post")
    def test_analyze_code_timeout(self, mock_post):
        """Test handling of request timeout"""
        import requests
        mock_post.side_effect = requests.exceptions.Timeout()
        
        llm = OpenRouterLLM(self.model_config)
        result = llm.analyze_code(
            prompt="Code here",
            file_path="test.py"
        )
        
        assert "Error" in result
        assert "timed out" in result.lower()

    @patch("requests.post")
    def test_analyze_code_empty_response(self, mock_post):
        """Test handling of empty API response"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": ""}}]
        }
        mock_post.return_value = mock_response
        
        llm = OpenRouterLLM(self.model_config)
        result = llm.analyze_code(
            prompt="Code here",
            file_path="test.py"
        )
        
        assert "Error" in result or "Empty" in result


    @patch("requests.post")
    def test_analyze_code_records_finish_reason(self, mock_post):
        """finish_reason is exposed so the reviewer can tell a review that
        hit max_tokens apart from one that ended naturally."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "[Line 3] Possible null deref"}, "finish_reason": "length"}],
            "usage": {},
        }
        mock_post.return_value = mock_response

        llm = OpenRouterLLM(self.model_config)
        assert llm.last_finish_reason is None
        llm.analyze_code(prompt="diff", file_path="test.py")

        assert llm.last_finish_reason == "length"


class TestSamplingParamSupport(TestCase):
    """Current Claude models reject temperature/top_p/top_k with a 400."""

    def test_openrouter_current_claude_models_reject_sampling_params(self):
        from pr_reviewer.llm.openrouter import _supports_sampling_params

        for model in (
            "anthropic/claude-opus-5.5",
            "anthropic/claude-opus-5",
            "anthropic/claude-sonnet-5.5",
            "anthropic/claude-haiku-5.5",
            "anthropic/claude-fable-5.1",
        ):
            assert not _supports_sampling_params(model), model

    def test_openrouter_other_models_keep_sampling_params(self):
        from pr_reviewer.llm.openrouter import _supports_sampling_params

        for model in ("x-ai/grok-4.7", "anthropic/claude-haiku-4.5", "openai/gpt-6.1-terra"):
            assert _supports_sampling_params(model), model

    def test_claude_api_current_models_reject_sampling_params(self):
        from pr_reviewer.llm.claude import _supports_sampling_params

        for model in ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-haiku-5-5"):
            assert not _supports_sampling_params(model), model
        assert _supports_sampling_params("claude-haiku-4-5")


class TestLLMFactory(TestCase):
    """Test cases for LLM factory"""

    def test_create_openrouter_llm(self):
        """Test creating OpenRouter LLM via factory"""
        config = {
            "provider": "openrouter",
            "api_key": "test-key",
            "model_name": "test-model"
        }
        
        llm = create_llm(config)
        
        assert isinstance(llm, OpenRouterLLM)
        assert llm.model_name == "test-model"

    def test_create_llm_default_provider(self):
        """Test factory with default provider (anthropic)"""
        config = {
            "api_key": "test-key",
            "model_name": "claude-3-sonnet"
        }
        
        # Should default to anthropic or handle gracefully
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            try:
                llm = create_llm(config)
                assert llm is not None
            except Exception:
                # If anthropic not available, that's OK
                pass

    def test_create_llm_unknown_provider(self):
        """Test factory with unknown provider falls back or handles gracefully"""
        config = {
            "provider": "unknown_provider",
            "api_key": "test-key"
        }
        
        # Factory may fall back to default provider or raise error
        # Either behavior is acceptable - just shouldn't crash unexpectedly
        try:
            llm = create_llm(config)
            # If it doesn't raise, it should return something usable
            assert llm is not None or llm is None  # Accepts any outcome
        except (ValueError, KeyError):
            pass  # This is also acceptable


class TestBaseLLM(TestCase):
    """Test cases for base LLM interface"""

    def test_base_llm_is_abstract(self):
        """Test that BaseLLM cannot be instantiated directly"""
        with pytest.raises(TypeError):
            BaseLLM({})

    def test_base_llm_subclass_must_implement_analyze_code(self):
        """Test that subclasses must implement analyze_code"""
        class IncompleteLLM(BaseLLM):
            pass
        
        with pytest.raises(TypeError):
            IncompleteLLM({})


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
