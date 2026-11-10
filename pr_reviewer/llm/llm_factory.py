#!/usr/bin/env python3
"""
Factory for creating LLM instances based on configuration.
"""

import importlib
import logging
from typing import Dict, Any, Optional

from pr_reviewer.llm.base import BaseLLM

logger = logging.getLogger(__name__)


def create_llm(model_config: Dict[str, Any]) -> BaseLLM:
    """Factory function to create an LLM instance based on configuration
    
    Args:
        model_config: Model configuration dictionary
        
    Returns:
        An initialized LLM instance (uses DummyLLM if required dependencies are missing)
        
    Raises:
        ValueError: If the model type is not supported
    """
    model_name = model_config.get("model_name", "claude-opus-5-5")
    provider = model_config.get("provider", "").lower()
    
    # Check if it's explicitly requesting the dummy model
    if model_name.lower() == "dummy":
        from pr_reviewer.llm.dummy import DummyLLM
        return DummyLLM(model_config)
    
    # Check for OpenRouter provider (supports Grok, Claude, GPT, etc. via OpenRouter)
    if provider == "openrouter" or "openrouter" in model_config.get("api_base", ""):
        try:
            import requests
            from pr_reviewer.llm.openrouter import OpenRouterLLM
            logger.info(f"Using OpenRouter with model: {model_name}")
            return OpenRouterLLM(model_config)
        except ImportError as e:
            logger.warning(f"Failed to create OpenRouter LLM due to missing dependencies: {e}")
            from pr_reviewer.llm.dummy import DummyLLM
            model_config["dummy_response"] = (
                "**Missing Dependencies Warning**\n\n"
                "This is a placeholder response because required dependencies for OpenRouter "
                "are not installed. Please install: pip install requests"
            )
            return DummyLLM(model_config)
    
    # Check for Claude API models
    if "claude" in model_name.lower():
        try:
            import requests
            from pr_reviewer.llm.claude import ClaudeLLM
            return ClaudeLLM(model_config)
        except ImportError as e:
            logger.warning(f"Failed to create Claude API client due to missing dependencies: {e}")
            logger.warning("Falling back to DummyLLM for testing. For Claude API, please install required dependencies: pip install requests")
            from pr_reviewer.llm.dummy import DummyLLM
            model_config["dummy_response"] = (
                "**Missing Dependencies Warning**\n\n"
                "This is a placeholder response because required dependencies for Claude API "
                "are not installed. For actual code review, please install the dependencies with:\n\n"
                "```bash\npip install requests\n```"
            )
            return DummyLLM(model_config)
    
    # Default to Claude API as the preferred option
    try:
        import requests
        from pr_reviewer.llm.claude import ClaudeLLM
        # Override model name to ensure it's a valid Claude model
        if "claude" not in model_name.lower():
            logger.info(f"Model '{model_name}' not recognized, defaulting to Claude API with 'claude-opus-5-5'")
            model_config["model_name"] = "claude-opus-5-5"
        return ClaudeLLM(model_config)
    except ImportError as e:
        logger.warning(f"Failed to create Claude API client due to missing dependencies: {e}")
        logger.warning("Falling back to DummyLLM for testing. For Claude API, please install required dependencies: pip install requests")
        from pr_reviewer.llm.dummy import DummyLLM
        model_config["dummy_response"] = (
            "**Missing Dependencies Warning**\n\n"
            "This is a placeholder response because required dependencies "
            "are not installed. For Claude API review, install 'requests'."
        )
        return DummyLLM(model_config)