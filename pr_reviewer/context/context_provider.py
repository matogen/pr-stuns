#!/usr/bin/env python3
"""
Context provider for code review enhancement.
Provides additional context about files and code relationships.
"""

import os
import logging
from typing import Dict, List, Any, Optional, Tuple
import re

from pr_reviewer.clients.base import BaseRepoClient
from pr_reviewer.utils.logging import logger

class ContextProvider:
    """Provides additional context for code review"""
    
    def __init__(self, git_client: BaseRepoClient):
        """Initialize ContextProvider
        
        Args:
            git_client: Repository client
        """
        self.git_client = git_client
        
    def get_file_context(self, filename: str, pr_head_commit: str, pr_id: Optional[int] = None) -> Dict[str, Any]:
        """Get contextual information for a file
        
        Args:
            filename: Path to the file
            pr_head_commit: PR head commit SHA
            pr_id: Optional PR ID to prioritize PR branch content for new files
            
        Returns:
            Dictionary with contextual information
        """
        context = {
            "filename": filename,
            "commit": pr_head_commit
        }
        
        # Always use the PR head commit when available (needed for new files)
        # Ensure we have a valid commit SHA - don't let it become None/empty
        ref_to_use = pr_head_commit if pr_head_commit and pr_head_commit.strip() else None
        
        # Add debug logging to understand why ref becomes None
        if not ref_to_use:
            logger.warning(f"pr_head_commit is invalid for {filename}: '{pr_head_commit}' (type: {type(pr_head_commit)})")
        
        logger.info(f"Getting context for file {filename} using ref {ref_to_use}, PR ID: {pr_id}")
        
        # Get full file content - pass PR ID to prioritize PR branch in Bitbucket client
        try:
            # The client implementation should handle trying various sources in appropriate order
            logger.info(f"Calling get_file_content with filename={filename}, ref={ref_to_use}, pr_id={pr_id}")
            content = self.git_client.get_file_content(filename, ref_to_use, pr_id)
            if content:
                context["full_content"] = content
                logger.info(f"Successfully retrieved content for {filename} ({len(content)} characters)")
            else:
                logger.warning(f"Empty content returned for {filename}")
                context["full_content"] = None
        except Exception as e:
            logger.warning(f"Failed to get full content for {filename}: {e}")
            context["full_content"] = None
            
        # If we couldn't get content and this appears to be a new file in the PR,
        # log this as it's important for debugging PR branch access issues
        if context["full_content"] is None and pr_head_commit:
            logger.warning(f"Could not retrieve content for {filename} with PR head commit {pr_head_commit}. "
                          f"This might be a new file in the PR branch or there might be issues accessing the PR branch.")
            
        # Get imports and related files
        try:
            if context["full_content"]:
                # If we have content directly, we can parse it without making an additional API call
                file_ext = os.path.splitext(filename)[1].lower()
                if file_ext == '.py':
                    context["imports"] = self._parse_python_imports(context["full_content"], filename, ref_to_use)
                elif file_ext in ['.js', '.jsx', '.ts', '.tsx']:
                    context["imports"] = self._parse_js_imports(context["full_content"], filename, ref_to_use)
                # Add other parsing as needed
                else:
                    # Fallback to API call
                    context["imports"] = self.get_related_imports(filename, ref_to_use, pr_id)
            else:
                # No content, try API call
                context["imports"] = self.get_related_imports(filename, ref_to_use, pr_id)
        except Exception as e:
            logger.warning(f"Failed to extract imports for {filename}: {e}")
            context["imports"] = []
            
        # Get class definitions if it's a class
        try:
            if context["full_content"]:
                # Use the content we already have
                context["class_info"] = self.get_class_info_from_content(
                    context["full_content"], filename)
            else:
                # Try API call
                context["class_info"] = self.get_class_info(filename, ref_to_use, pr_id)
        except Exception as e:
            logger.warning(f"Failed to extract class info for {filename}: {e}")
            context["class_info"] = {}
            
        # Get related files
        try:
            related_files = self.git_client.find_related_files(filename, ref_to_use)
            context["related_files"] = related_files
        except Exception as e:
            logger.warning(f"Failed to find related files for {filename}: {e}")
            context["related_files"] = []
            
        # Get file history (this might not exist for new files)
        try:
            history = self.git_client.get_file_history(filename, limit=5)
            context["history"] = history
        except Exception as e:
            logger.warning(f"Failed to get file history for {filename}: {e}")
            context["history"] = []
            
        # Log how much context we were able to gather
        context_summary = {
            "has_content": context["full_content"] is not None,
            "imports_count": len(context.get("imports", [])),
            "has_class_info": bool(context.get("class_info")),
            "related_files_count": len(context.get("related_files", [])),
            "history_count": len(context.get("history", []))
        }
        logger.info(f"Context gathered for {filename}: {context_summary}")
            
        return context
        
    def _parse_python_imports(self, content: str, filename: str, ref: Optional[str] = None) -> List[Dict[str, str]]:
        """Parse Python imports directly from content without API call
        
        Args:
            content: File content
            filename: Path to the file
            
        Returns:
            List of import information
        """
        imports = []
        
        # Match import statements using regex
        import_pattern = re.compile(r'^import\s+([\w\.]+)(?:\s+as\s+(\w+))?', re.MULTILINE)
        from_import_pattern = re.compile(r'^from\s+([\w\.]+)\s+import\s+(.+)$', re.MULTILINE)
        
        # Process 'import x' statements
        for match in import_pattern.finditer(content):
            module_name = match.group(1)
            alias = match.group(2)
            
            imports.append({
                "type": "import",
                "module": module_name,
                "alias": alias,
            })
            
        # Process 'from x import y' statements
        for match in from_import_pattern.finditer(content):
            module_name = match.group(1)
            imported_names = [name.strip() for name in match.group(2).split(',')]
            
            for name in imported_names:
                # Handle 'as' aliases in the imported names
                if ' as ' in name:
                    original, alias = name.split(' as ')
                    imports.append({
                        "type": "from_import",
                        "module": module_name,
                        "name": original.strip(),
                        "alias": alias.strip(),
                    })
                else:
                    imports.append({
                        "type": "from_import",
                        "module": module_name,
                        "name": name.strip(),
                        "alias": None,
                    })
                    
        return imports
        
    def _parse_js_imports(self, content: str, filename: str, ref: Optional[str] = None) -> List[Dict[str, str]]:
        """Parse JavaScript/TypeScript imports directly from content without API call
        
        Args:
            content: File content
            filename: Path to the file
            
        Returns:
            List of import information
        """
        imports = []
        
        # Match ES6 import statements
        import_pattern = re.compile(r'import\s+(?:{([^}]+)}\s+from\s+)?[\'"]([^\'"]+)[\'"]', re.MULTILINE)
        
        for match in import_pattern.finditer(content):
            imported_names = match.group(1)
            module_path = match.group(2)
            
            # Skip external modules before processing
            if self._should_skip_external_module(module_path):
                continue
            
            if imported_names:
                # Handle named imports like: import { x, y as z } from 'module'
                for named_import in imported_names.split(','):
                    named_import = named_import.strip()
                    
                    if ' as ' in named_import:
                        original, alias = named_import.split(' as ')
                        imports.append({
                            "type": "named_import",
                            "module": module_path,
                            "name": original.strip(),
                            "alias": alias.strip(),
                        })
                    else:
                        imports.append({
                            "type": "named_import",
                            "module": module_path,
                            "name": named_import,
                            "alias": None,
                        })
            else:
                # Handle default import like: import x from 'module'
                imports.append({
                    "type": "default_import",
                    "module": module_path,
                })
                
        # Also match require statements
        require_pattern = re.compile(r'(?:const|let|var)\s+(?:(\w+)|{([^}]+)})\s*=\s*require\([\'"]([^\'"]+)[\'"]\)', re.MULTILINE)
        
        for match in require_pattern.finditer(content):
            default_import = match.group(1)
            named_imports = match.group(2)
            module_path = match.group(3)
            
            # Skip external modules before processing
            if self._should_skip_external_module(module_path):
                continue
            
            if default_import:
                imports.append({
                    "type": "require_default",
                    "module": module_path,
                    "name": default_import,
                })
            elif named_imports:
                for named_import in named_imports.split(','):
                    named_import = named_import.strip()
                    
                    if ' as ' in named_import or ':' in named_import:
                        # Handle both ES6-style and CommonJS-style renaming
                        parts = re.split(r'\s+as\s+|:', named_import, 1)
                        if len(parts) == 2:
                            original, alias = parts
                            imports.append({
                                "type": "require_named",
                                "module": module_path,
                                "name": original.strip(),
                                "alias": alias.strip(),
                            })
                    else:
                        imports.append({
                            "type": "require_named",
                            "module": module_path,
                            "name": named_import,
                            "alias": None,
                        })
                        
        return imports
        
    def get_class_info_from_content(self, content: str, filename: str) -> Dict[str, Any]:
        """Extract class definition information directly from content without API call
        
        Args:
            content: File content
            filename: File path
            
        Returns:
            Dictionary with class information
        """
        file_ext = os.path.splitext(filename)[1].lower()
        
        if file_ext == '.py':
            return self._get_python_class_info(content, filename)
        elif file_ext in ['.java', '.kt']:
            return self._get_java_class_info(content, filename)
        elif file_ext in ['.js', '.jsx', '.ts', '.tsx']:
            return self._get_js_class_info(content, filename)
            
        return {}
    
    def get_related_imports(self, filename: str, commit_sha: str, pr_id: Optional[int] = None) -> List[Dict[str, str]]:
        """Extract information about imports and related files
        
        Args:
            filename: Path to the file
            commit_sha: Commit SHA
            pr_id: Optional PR ID to prioritize PR branch content for new files
            
        Returns:
            List of import information dictionaries
        """
        file_ext = os.path.splitext(filename)[1].lower()
        
        # Handle based on file type
        if file_ext == '.py':
            return self._get_python_imports(filename, commit_sha, pr_id)
        elif file_ext in ['.js', '.jsx', '.ts', '.tsx']:
            return self._get_js_imports(filename, commit_sha, pr_id)
        elif file_ext in ['.java', '.kt']:
            return self._get_java_imports(filename, commit_sha, pr_id)
        elif file_ext in ['.go']:
            return self._get_go_imports(filename, commit_sha, pr_id)
        elif file_ext in ['.rb']:
            return self._get_ruby_imports(filename, commit_sha, pr_id)
        elif file_ext in ['.php']:
            return self._get_php_imports(filename, commit_sha, pr_id)
        
        # Default empty result for unsupported file types
        return []
    
    def get_class_info(self, filename: str, commit_sha: str, pr_id: Optional[int] = None) -> Dict[str, Any]:
        """Extract class definition information
        
        Args:
            filename: Path to the file
            commit_sha: Commit SHA
            pr_id: Optional PR ID to prioritize PR branch content for new files
            
        Returns:
            Dictionary with class information
        """
        file_ext = os.path.splitext(filename)[1].lower()
        
        # Try to get file content
        try:
            content = self.git_client.get_file_content(filename, commit_sha, pr_id)
            if not content:
                logger.warning(f"Empty content returned for file {filename} in get_class_info")
                return {}
                
            # Use our content-based parser
            return self.get_class_info_from_content(content, filename)
        except Exception as e:
            logger.warning(f"Couldn't get file content for class analysis: {e}")
            return {}
        
    def _get_python_imports(self, filename: str, commit_sha: str, pr_id: Optional[int] = None) -> List[Dict[str, str]]:
        """Extract Python imports
        
        Args:
            filename: Path to the file
            commit_sha: Commit SHA
            pr_id: Optional PR ID to prioritize PR branch content for new files
            
        Returns:
            List of import information
        """
        imports = []
        
        try:
            content = self.git_client.get_file_content(filename, commit_sha, pr_id)
            
            # Check for empty content
            if not content:
                logger.warning(f"Empty content returned for Python import analysis in {filename}")
                return imports
            
            # Match import statements using regex
            import_pattern = re.compile(r'^import\s+([\w\.]+)(?:\s+as\s+(\w+))?', re.MULTILINE)
            from_import_pattern = re.compile(r'^from\s+([\w\.]+)\s+import\s+(.+)$', re.MULTILINE)
            
            # Process 'import x' statements
            for match in import_pattern.finditer(content):
                module_name = match.group(1)
                alias = match.group(2)
                
                imports.append({
                    "type": "import",
                    "module": module_name,
                    "alias": alias,
                })
                
            # Process 'from x import y' statements
            for match in from_import_pattern.finditer(content):
                module_name = match.group(1)
                imported_names = [name.strip() for name in match.group(2).split(',')]
                
                for name in imported_names:
                    # Handle 'as' aliases in the imported names
                    if ' as ' in name:
                        original, alias = name.split(' as ')
                        imports.append({
                            "type": "from_import",
                            "module": module_name,
                            "name": original.strip(),
                            "alias": alias.strip(),
                        })
                    else:
                        imports.append({
                            "type": "from_import",
                            "module": module_name,
                            "name": name.strip(),
                            "alias": None,
                        })
        except Exception as e:
            logger.warning(f"Error analyzing Python imports for {filename}: {e}")
            
        return imports
    
    def _get_python_class_info(self, content: str, filename: str) -> Dict[str, Any]:
        """Extract Python class information
        
        Args:
            content: File content
            filename: File path
            
        Returns:
            Dictionary with class information
        """
        class_info = {
            "classes": [],
            "parent_classes": [],
            "methods": []
        }
        
        # Extract class definitions
        class_pattern = re.compile(r'class\s+(\w+)(?:\(([^)]*)\))?:')
        method_pattern = re.compile(r'def\s+(\w+)\s*\([^)]*\):')
        
        for match in class_pattern.finditer(content):
            class_name = match.group(1)
            parent_classes_str = match.group(2)
            
            class_data = {
                "name": class_name,
                "parent_classes": []
            }
            
            if parent_classes_str:
                # Extract parent classes
                parent_classes = [c.strip() for c in parent_classes_str.split(',')]
                class_data["parent_classes"] = parent_classes
                class_info["parent_classes"].extend(parent_classes)
                
            class_info["classes"].append(class_data)
            
        # Extract methods
        for match in method_pattern.finditer(content):
            method_name = match.group(1)
            if not method_name.startswith('__'):  # Skip dunder methods
                class_info["methods"].append(method_name)
                
        return class_info
    
    def _get_js_imports(self, filename: str, commit_sha: str, pr_id: Optional[int] = None) -> List[Dict[str, str]]:
        """Extract JavaScript/TypeScript imports
        
        Args:
            filename: Path to the file
            commit_sha: Commit SHA
            pr_id: Optional PR ID to prioritize PR branch content for new files
            
        Returns:
            List of import information
        """
        imports = []
        
        try:
            content = self.git_client.get_file_content(filename, commit_sha, pr_id)
            
            # Check for empty content
            if not content:
                logger.warning(f"Empty content returned for JS import analysis in {filename}")
                return imports
            
            # Match ES6 import statements
            import_pattern = re.compile(r'import\s+(?:{([^}]+)}\s+from\s+)?[\'"]([^\'"]+)[\'"]', re.MULTILINE)
            
            for match in import_pattern.finditer(content):
                imported_names = match.group(1)
                module_path = match.group(2)
                
                # Skip external modules before processing
                if self._should_skip_external_module(module_path):
                    continue
                
                if imported_names:
                    # Handle named imports like: import { x, y as z } from 'module'
                    for named_import in imported_names.split(','):
                        named_import = named_import.strip()
                        
                        if ' as ' in named_import:
                            original, alias = named_import.split(' as ')
                            imports.append({
                                "type": "named_import",
                                "module": module_path,
                                "name": original.strip(),
                                "alias": alias.strip(),
                            })
                        else:
                            imports.append({
                                "type": "named_import",
                                "module": module_path,
                                "name": named_import,
                                "alias": None,
                            })
                else:
                    # Handle default import like: import x from 'module'
                    imports.append({
                        "type": "default_import",
                        "module": module_path,
                    })
                    
            # Also match require statements
            require_pattern = re.compile(r'(?:const|let|var)\s+(?:(\w+)|{([^}]+)})\s*=\s*require\([\'"]([^\'"]+)[\'"]\)', re.MULTILINE)
            
            for match in require_pattern.finditer(content):
                default_import = match.group(1)
                named_imports = match.group(2)
                module_path = match.group(3)
                
                # Skip external modules before processing
                if self._should_skip_external_module(module_path):
                    continue
                
                if default_import:
                    imports.append({
                        "type": "require_default",
                        "module": module_path,
                        "name": default_import,
                    })
                elif named_imports:
                    for named_import in named_imports.split(','):
                        named_import = named_import.strip()
                        
                        if ' as ' in named_import or ':' in named_import:
                            # Handle both ES6-style and CommonJS-style renaming
                            parts = re.split(r'\s+as\s+|:', named_import, 1)
                            if len(parts) == 2:
                                original, alias = parts
                                imports.append({
                                    "type": "require_named",
                                    "module": module_path,
                                    "name": original.strip(),
                                    "alias": alias.strip(),
                                })
                        else:
                            imports.append({
                                "type": "require_named",
                                "module": module_path,
                                "name": named_import,
                                "alias": None,
                            })
                            
        except Exception as e:
            logger.warning(f"Error analyzing JS imports for {filename}: {e}")
            
        return imports
        
    def _should_skip_external_module(self, module_path: str) -> bool:
        """Check if a module path should be skipped because it's an external dependency
        
        Args:
            module_path: Module path from import statement
            
        Returns:
            True if the module should be skipped (is external), False otherwise
        """
        # Skip scoped packages like @angular/core, @types/node
        if module_path.startswith('@'):
            return True
            
        # Skip common external packages
        external_packages = {
            'rxjs', 'lodash', 'moment', 'axios', 'express', 'react', 'vue', 'angular',
            'typescript', 'webpack', 'babel', 'eslint', 'prettier', 'jest', 'mocha',
            'karma', 'protractor', 'jasmine', 'zone.js', 'core-js', 'regenerator-runtime',
            'tslib', 'reflect-metadata', 'class-transformer', 'class-validator'
        }
        
        # Get the base package name (before any subpaths)
        base_package = module_path.split('/')[0]
        if base_package in external_packages:
            return True
            
        # Skip if it doesn't start with relative path indicators
        if not (module_path.startswith('./') or module_path.startswith('../')):
            # Allow absolute imports that look like project paths (contain multiple slashes)
            if module_path.count('/') < 2:
                return True
                
        return False
        
    def _get_js_class_info(self, content: str, filename: str) -> Dict[str, Any]:
        """Extract JS/TS class information
        
        Args:
            content: File content
            filename: File path
            
        Returns:
            Dictionary with class information
        """
        class_info = {
            "classes": [],
            "parent_classes": [],
            "methods": [],
            "react_components": []
        }
        
        # Extract ES6 class definitions
        class_pattern = re.compile(r'class\s+(\w+)(?:\s+extends\s+(\w+))?')
        method_pattern = re.compile(r'(?:async\s+)?(?:function\s+(\w+)|(\w+)\s*:\s*function|\s+(\w+)\([^)]*\)\s*{)')
        react_component_pattern = re.compile(r'(?:const|let|var)\s+(\w+)\s*=\s*(?:React\.createClass|React\.memo|React\.forwardRef|createReactClass)')
        react_func_component = re.compile(r'(?:function|const)\s+(\w+)[^(]*\([^)]*\)\s*{[^}]*return\s*\(\s*<')
        
        # Extract classes and inheritance
        for match in class_pattern.finditer(content):
            class_name = match.group(1)
            parent_class = match.group(2)
            
            class_data = {
                "name": class_name,
                "parent_class": parent_class
            }
            
            if parent_class:
                class_info["parent_classes"].append(parent_class)
                
            class_info["classes"].append(class_data)
            
        # Extract methods
        for match in method_pattern.finditer(content):
            method_name = match.group(1) or match.group(2) or match.group(3)
            if method_name and not method_name.startswith('_'):  # Skip private methods
                class_info["methods"].append(method_name)
                
        # Extract React components
        for match in react_component_pattern.finditer(content):
            component_name = match.group(1)
            class_info["react_components"].append(component_name)
            
        # Extract functional React components
        for match in react_func_component.finditer(content):
            component_name = match.group(1)
            class_info["react_components"].append(component_name)
            
        return class_info
                
    # Add stubs for other language implementations
    def _get_java_imports(self, filename: str, commit_sha: str, pr_id: Optional[int] = None) -> List[Dict[str, str]]:
        """Extract Java imports"""
        imports = []
        
        try:
            content = self.git_client.get_file_content(filename, commit_sha, pr_id)
            
            if not content:
                logger.warning(f"Empty content returned for Java import analysis in {filename}")
                return imports
            
            # Match Java import statements
            import_pattern = re.compile(r'^import\s+(?:static\s+)?([a-zA-Z_][\w.]*(?:\.\*)?);?', re.MULTILINE)
            
            for match in import_pattern.finditer(content):
                import_name = match.group(1)
                
                # Skip standard Java libraries
                if import_name.startswith(('java.', 'javax.', 'org.junit.', 'org.springframework.')):
                    continue
                    
                imports.append({
                    "type": "import",
                    "module": import_name,
                })
                
        except Exception as e:
            logger.warning(f"Error analyzing Java imports for {filename}: {e}")
            
        return imports
        
    def _get_go_imports(self, filename: str, commit_sha: str, pr_id: Optional[int] = None) -> List[Dict[str, str]]:
        """Extract Go imports"""
        imports = []
        
        try:
            content = self.git_client.get_file_content(filename, commit_sha, pr_id)
            
            if not content:
                logger.warning(f"Empty content returned for Go import analysis in {filename}")
                return imports
            
            # Match Go import statements - both single and grouped
            single_import = re.compile(r'^import\s+"([^"]+)"', re.MULTILINE)
            group_import = re.compile(r'import\s*\(\s*\n(.*?)\n\s*\)', re.MULTILINE | re.DOTALL)
            
            # Handle single imports
            for match in single_import.finditer(content):
                import_path = match.group(1)
                
                # Skip standard library
                if '/' not in import_path or import_path.startswith('golang.org/'):
                    continue
                    
                imports.append({
                    "type": "import",
                    "module": import_path,
                })
            
            # Handle grouped imports
            for match in group_import.finditer(content):
                import_block = match.group(1)
                line_import = re.compile(r'"([^"]+)"')
                
                for line_match in line_import.finditer(import_block):
                    import_path = line_match.group(1)
                    
                    # Skip standard library
                    if '/' not in import_path or import_path.startswith('golang.org/'):
                        continue
                        
                    imports.append({
                        "type": "import",
                        "module": import_path,
                    })
                
        except Exception as e:
            logger.warning(f"Error analyzing Go imports for {filename}: {e}")
            
        return imports
        
    def _get_ruby_imports(self, filename: str, commit_sha: str, pr_id: Optional[int] = None) -> List[Dict[str, str]]:
        """Extract Ruby imports (require statements)"""
        imports = []
        
        try:
            content = self.git_client.get_file_content(filename, commit_sha, pr_id)
            
            if not content:
                logger.warning(f"Empty content returned for Ruby import analysis in {filename}")
                return imports
            
            # Match Ruby require statements
            require_pattern = re.compile(r'require\s+[\'"]([^\'"]+)[\'"]', re.MULTILINE)
            require_relative_pattern = re.compile(r'require_relative\s+[\'"]([^\'"]+)[\'"]', re.MULTILINE)
            
            # Handle require statements
            for match in require_pattern.finditer(content):
                require_path = match.group(1)
                
                # Skip gems and standard library
                if '/' not in require_path or require_path in ['date', 'json', 'yaml', 'csv', 'uri', 'net/http']:
                    continue
                    
                imports.append({
                    "type": "require",
                    "module": require_path,
                })
            
            # Handle require_relative statements
            for match in require_relative_pattern.finditer(content):
                require_path = match.group(1)
                
                imports.append({
                    "type": "require_relative",
                    "module": require_path,
                })
                
        except Exception as e:
            logger.warning(f"Error analyzing Ruby imports for {filename}: {e}")
            
        return imports
        
    def _get_php_imports(self, filename: str, commit_sha: str, pr_id: Optional[int] = None) -> List[Dict[str, str]]:
        """Extract PHP imports (use/include/require statements)"""
        imports = []
        
        try:
            content = self.git_client.get_file_content(filename, commit_sha, pr_id)
            
            if not content:
                logger.warning(f"Empty content returned for PHP import analysis in {filename}")
                return imports
            
            # Match PHP use statements (namespaces)
            use_pattern = re.compile(r'use\s+([\\A-Za-z0-9_]+)(?:\s+as\s+(\w+))?;', re.MULTILINE)
            
            # Match require/include statements
            require_pattern = re.compile(r'(?:require|include)(?:_once)?\s*\(?[\'"]([^\'"]+)[\'"]', re.MULTILINE)
            
            # Handle use statements
            for match in use_pattern.finditer(content):
                namespace = match.group(1)
                alias = match.group(2)
                
                # Skip vendor/external packages
                if namespace.startswith('\\') or '\\' not in namespace:
                    continue
                    
                imports.append({
                    "type": "use",
                    "module": namespace,
                    "alias": alias,
                })
            
            # Handle require/include statements
            for match in require_pattern.finditer(content):
                file_path = match.group(1)
                
                imports.append({
                    "type": "require",
                    "module": file_path,
                })
                
        except Exception as e:
            logger.warning(f"Error analyzing PHP imports for {filename}: {e}")
            
        return imports
        
    def _get_java_class_info(self, content: str, filename: str) -> Dict[str, Any]:
        """Java class info stub"""
        return {}