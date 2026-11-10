#!/usr/bin/env python3
"""
Claude API implementation for code review.
"""

import os
import logging
import time
import json
from typing import Dict, Any, Optional
import requests

from pr_reviewer.llm.base import BaseLLM

logger = logging.getLogger(__name__)

# Model families that reject temperature/top_p/top_k (400 invalid_request_error)
# and run adaptive thinking instead. Claude Fable 5 / Mythos 5 additionally run
# thinking unconditionally and never accept an explicit `thinking` param.
# "claude-opus-5" also covers claude-opus-5-5, "claude-haiku-5" haiku-5-5.
_NO_SAMPLING_PARAMS_PREFIXES = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-5",
)


def _supports_sampling_params(model_name: str) -> bool:
    name = model_name.lower()
    return not name.startswith(_NO_SAMPLING_PARAMS_PREFIXES)


class ClaudeLLM(BaseLLM):
    """Claude API implementation for code review"""

    def __init__(self, model_config: Dict[str, Any]):
        """Initialize Claude API client

        Args:
            model_config: Model configuration parameters
        """
        super().__init__(model_config)

        # Get configuration
        self.api_key = model_config.get("api_key") or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError("Anthropic API key must be provided in model_config or as ANTHROPIC_API_KEY environment variable")

        self.model_name = model_config.get("model_name", "claude-opus-5-5")
        self.max_tokens = model_config.get("max_tokens", 4096)
        self.temperature = model_config.get("temperature", 0.3)
        self.supports_sampling_params = _supports_sampling_params(self.model_name)
        self.system_prompt = model_config.get("system_prompt",
                                            "You are an expert code reviewer. Review the code changes and provide constructive feedback.")

        # Determine API endpoint
        self.api_base = model_config.get("api_base", "https://api.anthropic.com/v1/messages")
        self.api_version = model_config.get("api_version", "2023-06-01")

        # Setup API headers
        self.headers = {
            "x-api-key": self.api_key,
            "anthropic-version": self.api_version,
            "content-type": "application/json",
        }

        logger.info(f"Claude API client initialized with model {self.model_name}")
    
    def analyze_code(self, prompt: str, file_path: str, context: Optional[Dict[str, Any]] = None) -> str:
        """Analyze code changes and provide feedback via Claude API
        
        Args:
            prompt: Full prompt including code diff and additional context
            file_path: Path of the file being analyzed
            context: Optional context information about the file and PR
            
        Returns:
            Analysis results as a string
        """
        # Determine file type from extension
        file_extension = os.path.splitext(file_path)[1][1:] if '.' in file_path else ''
        
        # Construct the messages - the prompt already contains the full context and instructions
        messages = [
            {
                "role": "user",
                "content": f"Review code for file: {file_path}\nLanguage: {file_extension}\n\n{prompt}"
            }
        ]
        
        # Enhance system prompt with accuracy directives
        accuracy_prompt = self.system_prompt + (
            " ACCURACY IS CRITICAL. Do not make definitive claims unless you are absolutely certain. "
            "Avoid statements like 'never used' or 'unused' unless you've verified the entire codebase. "
            "Instead use phrases like 'appears unused in this diff', 'I don't see usage in the provided code', "
            "or 'consider checking if this is used elsewhere'. When unsure, be explicit about your uncertainty. "
            "It's better to be tentative and accurate than confident and wrong."
        )
        
        payload = {
            "model": self.model_name,
            "messages": messages,
            "system": accuracy_prompt,
            "max_tokens": self.max_tokens,
        }

        # Claude Fable 5 / Mythos 5 and the Opus 4.7+/Sonnet 5 family reject
        # temperature/top_p/top_k outright (400 invalid_request_error).
        if self.supports_sampling_params:
            payload["temperature"] = self.temperature
            if "top_p" in self.model_config:
                payload["top_p"] = self.model_config["top_p"]
            if "top_k" in self.model_config:
                payload["top_k"] = self.model_config["top_k"]

        self.last_finish_reason = None
        try:
            logger.info(f"Sending request to Claude API ({len(prompt)} chars)...")

            # Implement retry logic
            max_retries = 3
            retry_delay = 1  # seconds (reduced from 2)
            # Fable 5/Mythos 5 run thinking unconditionally and can take
            # minutes on a single request; give those extra headroom.
            request_timeout = 180 if not self.supports_sampling_params else 30

            for attempt in range(max_retries):
                try:
                    response = requests.post(
                        self.api_base,
                        headers=self.headers,
                        json=payload,
                        timeout=request_timeout
                    )

                    # Check for successful response
                    response.raise_for_status()

                    # Parse the response
                    result = response.json()
                    self.last_finish_reason = result.get("stop_reason")

                    if result.get("stop_reason") == "refusal":
                        logger.warning(f"Claude API refused to review {file_path}: {result.get('stop_details')}")
                        return "Claude declined to review this file (safety refusal). No issues reported."

                    # Extract the content from the response
                    if "content" in result and result["content"]:
                        for content_block in result["content"]:
                            if content_block.get("type") == "text":
                                return content_block.get("text", "")

                        # If we didn't find any text content blocks, return a generic message
                        return "No text content found in Claude API response."

                    # Return full response as a fallback
                    return json.dumps(result, indent=2)

                except requests.exceptions.RequestException as e:
                    if attempt < max_retries - 1:
                        logger.warning(f"API request failed (attempt {attempt+1}/{max_retries}): {str(e)}. Retrying in {retry_delay} seconds...")
                        time.sleep(retry_delay)
                        retry_delay *= 2  # Exponential backoff
                    else:
                        raise
            
        except Exception as e:
            logger.error(f"Error calling Claude API: {str(e)}")
            return f"Error: Failed to generate review via Claude API: {str(e)}"