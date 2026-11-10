#!/usr/bin/env python3
"""
Tests for configuration module.
"""

import os
import tempfile
from unittest import TestCase

import pytest
import yaml

from pr_reviewer.config import ConfigManager, create_sample_config


class TestConfig(TestCase):
    """Test cases for configuration manager"""
    
    def test_load_config(self) -> None:
        """Test loading a valid configuration"""
        # Create a temporary config file
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
            yaml.dump({
                'repositories': [
                    {
                        'type': 'github',
                        'owner': 'test',
                        'repo': 'test-repo',
                        'token': 'test-token'
                    }
                ],
                'llm': {
                    'model_name': 'test-model',
                    'temperature': 0.5
                }
            }, f)
        
        try:
            # Load the config
            config = ConfigManager(f.name)
            
            # Check repositories
            repos = config.get_repositories()
            assert len(repos) == 1
            assert repos[0]['type'] == 'github'
            assert repos[0]['owner'] == 'test'
            
            # Check LLM config
            llm_config = config.get_llm_config()
            assert llm_config['model_name'] == 'test-model'
            assert llm_config['temperature'] == 0.5
            
            # Check comment template
            template = config.get_comment_template()
            assert '{remarks}' in template
        finally:
            # Clean up
            os.unlink(f.name)
    
    def test_missing_config_file(self) -> None:
        """Test error when config file is missing"""
        with pytest.raises(FileNotFoundError):
            ConfigManager('nonexistent_config.yaml')
    
    def test_invalid_config(self) -> None:
        """Test error when config is invalid"""
        # Create a temporary config file with missing required fields
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
            yaml.dump({
                'repositories': []
                # Missing 'llm' field
            }, f)
        
        try:
            # Attempt to load invalid config
            with pytest.raises(ValueError):
                ConfigManager(f.name)
        finally:
            # Clean up
            os.unlink(f.name)
    
    def test_create_sample_config(self) -> None:
        """Test creating a sample configuration"""
        temp_file = os.path.join(tempfile.gettempdir(), 'test_sample_config.yaml')
        
        try:
            # Create sample config
            create_sample_config(temp_file)
            
            # Verify file exists
            assert os.path.exists(temp_file)
            
            # Load and check the config
            with open(temp_file, 'r') as f:
                config = yaml.safe_load(f)
            
            # Check required sections
            assert 'repositories' in config
            assert 'llm' in config
            assert 'comment_template' in config
            
            # Check repositories
            assert len(config['repositories']) > 0
            assert 'github' in [repo.get('type', '') for repo in config['repositories']]
        finally:
            # Clean up
            if os.path.exists(temp_file):
                os.unlink(temp_file)

class TestGobyConfig(TestCase):
    """UserStore/ConfigManager expose named Goby instances from users.yml,
    mirroring the Jira ones."""

    def _write_users(self, tmp_dir, content):
        path = os.path.join(tmp_dir, "users.yml")
        with open(path, "w") as f:
            f.write(content)
        return path

    def test_get_goby_config_returns_named_instance(self):
        from pr_reviewer.config.users import UserStore
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = self._write_users(tmp_dir, (
                "users: {}\n"
                "goby:\n"
                "  default:\n"
                "    base_url: 'https://your-team.goby.chat'\n"
                "    api_key: 'gk_a_b'\n"
                "  ops:\n"
                "    base_url: 'https://your-team.goby.chat'\n"
                "    api_key: 'gk_ops_secret'\n"
            ))
            store = UserStore(path)

        self.assertEqual(store.get_goby_config()["api_key"], "gk_a_b")
        self.assertEqual(store.get_goby_config("ops")["api_key"], "gk_ops_secret")

    def test_get_goby_config_returns_empty_dict_for_unknown_or_missing(self):
        from pr_reviewer.config.users import UserStore
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = self._write_users(tmp_dir, "users: {}\n")
            store = UserStore(path)

        self.assertEqual(store.get_goby_config(), {})
        self.assertEqual(store.get_goby_config("nope"), {})
        self.assertEqual(UserStore(None).get_goby_config(), {})
