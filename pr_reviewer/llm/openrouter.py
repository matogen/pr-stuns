#!/usr/bin/env python3
"""
OpenRouter LLM implementation for code review.
Supports various models via OpenRouter API (Grok, Claude, GPT, etc.)
"""

import logging
import os
import time
from typing import Any, Dict

import requests

from pr_reviewer.llm.base import BaseLLM

logger = logging.getLogger(__name__)

# Transient HTTP statuses worth retrying. Other 4xx errors (404 model gone,
# 401 bad key, 400 bad request) are permanent and fail immediately.
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
MAX_RETRIES = 3

# Claude model families that run adaptive thinking unconditionally and reject
# non-default temperature/top_p/top_k (mirrors
# pr_reviewer.llm.claude._NO_SAMPLING_PARAMS_PREFIXES, adapted for
# OpenRouter's "anthropic/<model>" naming, which uses dots: claude-opus-4.8).
# "claude-opus-5" also matches claude-opus-5.5, "claude-haiku-5" haiku-5.5.
_NO_SAMPLING_PARAMS_SUBSTRINGS = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-4-7",
    "claude-opus-4.7",
    "claude-opus-4-8",
    "claude-opus-4.8",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-5",
)


def _supports_sampling_params(model_name: str) -> bool:
    name = model_name.lower()
    return not any(s in name for s in _NO_SAMPLING_PARAMS_SUBSTRINGS)


class OpenRouterLLM(BaseLLM):
    """LLM implementation using OpenRouter API."""

    def __init__(self, model_config: Dict[str, Any]):
        """Initialize the OpenRouter LLM client.

        Args:
            model_config: Model configuration parameters
        """
        super().__init__(model_config)

        self.api_key = model_config.get("api_key") or os.environ.get("OPENROUTER_API_KEY")
        if not self.api_key:
            raise ValueError(
                "OpenRouter API key must be provided in model_config or as OPENROUTER_API_KEY environment variable"
            )

        self.model_name = model_config.get("model_name", "x-ai/grok-4.7")
        self.max_tokens = model_config.get("max_tokens", 4096)
        self.temperature = model_config.get("temperature", 0.3)
        self.supports_sampling_params = _supports_sampling_params(self.model_name)
        self.system_prompt = model_config.get(
            "system_prompt",
            "You are an expert code reviewer. Provide concise, actionable feedback.",
        )

        self.api_base = "https://openrouter.ai/api/v1/chat/completions"

        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/matogen/pr-stuns",
            "X-Title": "PR Stuns",
        }

        logger.info(f"OpenRouter LLM client initialized with model {self.model_name}")

    def analyze_code(self, prompt: str, file_path: str, context: dict = None) -> str:
        """Analyze code and return review comments.

        Args:
            prompt: Full prompt including code diff and additional context
            file_path: Path of the file being analyzed
            context: Optional context information about the file and PR

        Returns:
            Review comments and suggestions
        """
        # Build user message with context if provided
        user_content = prompt
        if context:
            if context.get("pr_title"):
                user_content = f"PR: {context['pr_title']}\n\n{user_content}"
            if context.get("pr_description"):
                user_content = f"{user_content}\n\nPR Description: {context['pr_description']}"

        messages = [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": f"Please review the following code changes for file '{file_path}':\n\n{user_content}",
            },
        ]

        payload = {
            "model": self.model_name,
            "messages": messages,
            "max_tokens": self.max_tokens,
        }

        # Sonnet 5+ / Opus 4.7+ / Haiku 5.5 / Fable 5 / Mythos 5 reject non-default
        # temperature/top_p/top_k outright (400 invalid_request_error).
        if self.supports_sampling_params:
            payload["temperature"] = self.temperature
        else:
            # These models run adaptive thinking unconditionally, and thinking
            # tokens are drawn from the same budget as the visible reply. On
            # OpenRouter, Anthropic's reasoning budget must be set explicitly
            # via reasoning.max_tokens (effort presets aren't honored for
            # Anthropic) or a large/complex file can burn the entire
            # self.max_tokens budget on invisible reasoning and return empty
            # content — see the "Empty response" warning below. Capping
            # reasoning here guarantees headroom for the actual review text,
            # as long as self.max_tokens comfortably exceeds this cap.
            payload["reasoning"] = {"max_tokens": 2000}

        last_error = "Unknown error"
        self.last_finish_reason = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = requests.post(
                    self.api_base,
                    headers=self.headers,
                    json=payload,
                    timeout=120,
                )

                if response.status_code != 200:
                    error_msg = f"OpenRouter API error: {response.status_code} - {response.text}"
                    logger.error(error_msg)
                    if response.status_code not in RETRYABLE_STATUS:
                        # Permanent failure (e.g. deprecated/unknown model, bad
                        # key) — retrying cannot help, so fail fast.
                        return f"Error: {error_msg}"
                    last_error = error_msg
                else:
                    result = response.json()

                    if "choices" not in result or len(result["choices"]) == 0:
                        logger.error(f"Unexpected OpenRouter response format: {result}")
                        return "Error: Unexpected response format from OpenRouter API"

                    choice = result["choices"][0]
                    content = choice.get("message", {}).get("content", "")
                    self.last_finish_reason = choice.get("finish_reason")

                    if not content:
                        finish_reason = choice.get("finish_reason")
                        usage = result.get("usage", {})
                        logger.warning(
                            f"Empty response from OpenRouter API (finish_reason={finish_reason}, "
                            f"completion_tokens={usage.get('completion_tokens', 0)}, "
                            f"reasoning_tokens={usage.get('completion_tokens_details', {}).get('reasoning_tokens', 'n/a')})"
                        )
                        last_error = "Empty response from OpenRouter API"
                    else:
                        usage = result.get("usage", {})
                        if usage:
                            logger.info(
                                f"OpenRouter usage - prompt: {usage.get('prompt_tokens', 0)}, "
                                f"completion: {usage.get('completion_tokens', 0)}, "
                                f"total: {usage.get('total_tokens', 0)}"
                            )
                        if self.last_finish_reason == "length":
                            logger.warning(
                                f"Review for {file_path} hit max_tokens ({self.max_tokens}) and is truncated"
                            )
                        return content

            except requests.exceptions.Timeout:
                logger.warning(f"OpenRouter request timed out (attempt {attempt}/{MAX_RETRIES})")
                last_error = "Request timed out"
            except requests.exceptions.RequestException as e:
                logger.warning(f"OpenRouter request failed (attempt {attempt}/{MAX_RETRIES}): {e}")
                last_error = f"Failed to generate review via OpenRouter API: {str(e)}"
            except Exception as e:
                logger.error(f"Unexpected error in OpenRouter LLM: {e}")
                return f"Error: {str(e)}"

            if attempt < MAX_RETRIES:
                backoff = 2 ** attempt
                logger.info(f"Retrying OpenRouter request in {backoff}s...")
                time.sleep(backoff)

        return f"Error: {last_error}"
