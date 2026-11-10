#!/usr/bin/env python3

from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as f:
    long_description = f.read()

setup(
    name="pr-stuns",
    version="0.1.0",
    author="Matogen",
    description="Automated code review for pull requests using AI models (Claude API or OpenRouter)",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/matogen/pr-stuns",
    packages=find_packages(),
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.8",
    install_requires=[
        "requests>=2.25.0",
        "pyyaml>=6.0",
        "gitpython>=3.1.30",
        "python-dateutil>=2.8.2",
    ],
    extras_require={
        "dev": [
            "pytest>=7.0.0",
            "pytest-cov>=4.0.0",
            "black>=23.0.0",
            "isort>=5.12.0",
            "mypy>=1.0.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "pr-reviewer=pr_reviewer.__main__:main",
        ],
    },
)