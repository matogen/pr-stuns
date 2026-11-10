#!/usr/bin/env python3
"""
Flow diagram of the code changed in a pull request.

The LLM is asked for a Mermaid flowchart of the changed code paths. The
diagram is then added to the PR summary comment, either as an image (the
Mermaid source is encoded into the URL of a renderer such as mermaid.ink,
so nothing has to be uploaded to the git host) or as a ```mermaid block for
hosts that render Mermaid themselves (GitHub, GitLab).

Everything here is best effort: any problem yields None and the review is
posted without a diagram.
"""

import base64
import json
import re
import zlib
from typing import Any, Dict, List, Optional

from pr_reviewer.models import FileChange
from pr_reviewer.utils.logging import logger

NO_DIAGRAM_SENTINEL = "NO_DIAGRAM"

DEFAULT_RENDERER_URL = "https://mermaid.ink"
DEFAULT_MAX_NODES = 25

# Diff text sent to the model for the diagram, across all files and per file.
MAX_DIAGRAM_DIFF_CHARS = 60000
MAX_DIAGRAM_DIFF_CHARS_PER_FILE = 12000

# The whole diagram travels in the image URL. Past this length git hosts and
# proxies start rejecting or truncating the URL, so no image is posted.
MAX_IMAGE_URL_CHARS = 6000

_DIAGRAM_START = re.compile(r"^\s*(flowchart|graph)\s+(TD|TB|BT|LR|RL)\b", re.IGNORECASE)
_FENCED_BLOCK = re.compile(r"```(?:mermaid)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def resolve_flow_diagram_config(global_config: Any, repo_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge the top-level `flow_diagram:` block with a repo's own override.

    A repo entry may set `flow_diagram: true/false` or a mapping with any of
    the same keys as the top-level block.
    """
    resolved: Dict[str, Any] = {
        "enabled": False,
        "mode": "image",
        "renderer_url": DEFAULT_RENDERER_URL,
        "max_nodes": DEFAULT_MAX_NODES,
    }
    for source in (global_config, (repo_config or {}).get("flow_diagram")):
        if isinstance(source, bool):
            resolved["enabled"] = source
        elif isinstance(source, dict):
            resolved.update({k: v for k, v in source.items() if v is not None})
    resolved["enabled"] = resolved["enabled"] is True
    return resolved


def build_flow_diagram_prompt(pr_context_block: str, file_changes: List[FileChange], max_nodes: int) -> str:
    """Build the prompt asking the model for a Mermaid flowchart of the PR."""
    prompt = (
        "Draw a flow diagram of the code changed in this pull request, so a reviewer can see "
        "at a glance what the changed code does and how control flows through it.\n\n"
        "Rules:\n"
        "- Respond with ONE Mermaid flowchart in a ```mermaid code block and nothing else.\n"
        "- Start the diagram with `flowchart TD`.\n"
        "- Show the runtime flow through the changed code: entry points, decisions, calls "
        "between the changed functions, error paths and outcomes. Do not draw a file list.\n"
        f"- Use at most {max_nodes} nodes. Prefer the important paths over completeness.\n"
        "- Use short ids (A, B, C1) and put every label in double quotes, e.g. "
        'A["Validate input"] or B{"Token valid?"}. Label decision edges, e.g. B -->|"yes"| C.\n'
        "- Keep labels under 60 characters, plain text only: no double quotes, backticks, "
        "HTML or line breaks inside a label.\n"
        "- Only draw what the diff shows. Do not invent behaviour.\n"
        f"- If the change has no meaningful control flow to draw (config, docs, styling, "
        f"renames, dependency bumps), respond with exactly {NO_DIAGRAM_SENTINEL}.\n\n"
    )
    prompt += pr_context_block or ""
    prompt += "\nChanged files:\n"

    remaining = MAX_DIAGRAM_DIFF_CHARS
    omitted = 0
    for change in file_changes:
        if not change.patch:
            continue
        patch = change.patch
        if len(patch) > MAX_DIAGRAM_DIFF_CHARS_PER_FILE:
            patch = patch[:MAX_DIAGRAM_DIFF_CHARS_PER_FILE] + "\n... (diff truncated)"
        if len(patch) > remaining:
            omitted += 1
            continue
        remaining -= len(patch)
        prompt += f"\n### {change.filename} ({change.status})\n```diff\n{patch}\n```\n"
    if omitted:
        prompt += f"\n({omitted} more changed file(s) not shown because of size.)\n"
    return prompt


def extract_mermaid(response: Optional[str]) -> Optional[str]:
    """Pull a Mermaid flowchart out of the model's answer.

    Returns None when the model declined, errored, or answered with
    something that is not recognisably a flowchart.
    """
    text = (response or "").strip()
    if not text or text.startswith("Error:") or NO_DIAGRAM_SENTINEL in text:
        return None

    candidates = [m.strip() for m in _FENCED_BLOCK.findall(text)] or [text]
    for candidate in candidates:
        if not _DIAGRAM_START.match(candidate):
            continue
        # A flowchart needs at least one edge to be worth showing.
        if "--" not in candidate and "==" not in candidate:
            continue
        # A fence inside the diagram would break out of the comment's code block.
        if "```" in candidate:
            continue
        return candidate
    return None


def build_image_url(mermaid_source: str, renderer_url: str = DEFAULT_RENDERER_URL) -> Optional[str]:
    """Encode the diagram into a mermaid.ink-style image URL.

    Uses the same "pako" encoding as the Mermaid live editor: the JSON state
    is zlib-compressed and base64url-encoded into the path. Returns None if
    the resulting URL would be too long to be reliable.
    """
    state = json.dumps({"code": mermaid_source, "mermaid": {"theme": "default"}})
    encoded = base64.urlsafe_b64encode(zlib.compress(state.encode("utf-8"), 9)).decode("ascii")
    url = f"{renderer_url.rstrip('/')}/img/pako:{encoded}?type=png"
    if len(url) > MAX_IMAGE_URL_CHARS:
        return None
    return url


def build_flow_diagram_section(mermaid_source: str, mode: str = "image",
                               renderer_url: str = DEFAULT_RENDERER_URL) -> Optional[str]:
    """Markdown for the summary comment, or None if it cannot be rendered."""
    heading = "## Flow of the changed code\n\n"
    if mode == "mermaid":
        return f"{heading}```mermaid\n{mermaid_source}\n```"

    url = build_image_url(mermaid_source, renderer_url)
    if not url:
        logger.info("Flow diagram is too large to encode in an image URL, skipping it")
        return None
    return f"{heading}![Flow diagram of the changed code]({url})"


def generate_flow_diagram_section(llm: Any, pr_context_block: str, file_changes: List[FileChange],
                                  config: Dict[str, Any]) -> Optional[str]:
    """Ask the LLM for a diagram and return the markdown section, or None."""
    if not config.get("enabled"):
        return None
    if not any(change.patch for change in file_changes):
        return None

    try:
        max_nodes = int(config.get("max_nodes") or DEFAULT_MAX_NODES)
        prompt = build_flow_diagram_prompt(pr_context_block, file_changes, max_nodes)
        response = llm.analyze_code(prompt, "flow_diagram")
        mermaid_source = extract_mermaid(response if isinstance(response, str) else None)
        if not mermaid_source:
            logger.info("No flow diagram produced for this PR")
            return None
        return build_flow_diagram_section(
            mermaid_source,
            mode=str(config.get("mode") or "image").lower(),
            renderer_url=str(config.get("renderer_url") or DEFAULT_RENDERER_URL),
        )
    except Exception as e:
        logger.warning(f"Flow diagram generation failed, continuing without it: {e}")
        return None
