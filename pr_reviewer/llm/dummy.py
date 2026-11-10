#!/usr/bin/env python3
"""
Dummy LLM implementation for testing purposes.
"""

import os
import logging
from typing import Dict, Any, Optional

from pr_reviewer.llm.base import BaseLLM

logger = logging.getLogger(__name__)


class DummyLLM(BaseLLM):
    """Dummy LLM implementation that returns a fixed response"""
    
    def __init__(self, model_config: Dict[str, Any]):
        """Initialize Dummy LLM
        
        Args:
            model_config: Model configuration parameters
        """
        super().__init__(model_config)
        self.response = model_config.get("dummy_response", 
                                        "I've reviewed this file but couldn't provide detailed feedback. Please consider reviewing these changes manually.")
        logger.info("Initialized DummyLLM for testing")
        logger.info(f"Dummy response configured (length: {len(self.response)})")
    
    def analyze_code(self, prompt: str, file_path: str, context: Optional[Dict[str, Any]] = None) -> str:
        """Analyze code changes and provide feedback
        
        Args:
            prompt: Full prompt including code diff and additional context
            file_path: Path of the file being analyzed
            context: Optional context information about the file and PR
            
        Returns:
            Fixed dummy response
        """
        # Log the request but return a fixed response
        logger.info(f"DummyLLM received request to analyze {file_path}")
        logger.info(f"Returning dummy response ({len(self.response)} chars)")
        
        # If context is provided, log that we received enhanced context
        if context and context.get("file_context"):
            file_context = context["file_context"]
            context_items = [k for k, v in file_context.items() if v]
            logger.info(f"Received enhanced context with: {', '.join(context_items)}")
            
        return self.response