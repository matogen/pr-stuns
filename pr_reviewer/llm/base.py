#!/usr/bin/env python3
"""
Base interface for LLM (Large Language Model) implementations.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional


class BaseLLM(ABC):
    """Abstract base class for LLM implementations"""
    
    def __init__(self, model_config: Dict[str, Any]):
        """Initialize LLM with configuration
        
        Args:
            model_config: Model configuration parameters
        """
        self.model_config = model_config
        # Why the most recent analyze_code() response ended ("stop",
        # "length", "max_tokens", ...), or None if unknown. Lets callers
        # spot a review that was cut off at the output limit.
        self.last_finish_reason: Optional[str] = None
    
    @abstractmethod
    def analyze_code(self, prompt: str, file_path: str, context: Optional[Dict[str, Any]] = None) -> str:
        """Analyze code changes and provide feedback
        
        Args:
            prompt: Full prompt including code diff and additional context
            file_path: Path of the file being analyzed
            context: Optional context information about the file and PR
            
        Returns:
            Analysis results as a string
        """
        pass