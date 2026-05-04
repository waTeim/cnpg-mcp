#!/usr/bin/env python3
"""
cnpg-mcp - Prompt Registry

Manages MCP prompts with versioning, validation, and hot-reloading.
Prompts are loaded from a YAML/JSON file (typically mounted from a ConfigMap).

Features:
- Versioned prompt bundles with SHA256 hashing
- Pydantic schema validation
- Hot-reload via file watching or admin endpoint
- ETag support for caching
"""

import hashlib
import json
import os
import re
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

from pydantic import BaseModel, field_validator

logger = logging.getLogger(__name__)


# =============================================================================
# Schema Definitions
# =============================================================================

class PromptArgument(BaseModel):
    """Schema for prompt arguments."""
    name: str
    description: str
    required: bool = True


class PromptDefinition(BaseModel):
    """Schema for a single prompt definition."""
    id: str
    name: str
    description: str
    template: str
    arguments: List[PromptArgument] = []

    @field_validator('template')
    @classmethod
    def validate_template_size(cls, v: str) -> str:
        """Enforce maximum template size."""
        max_size = 10000  # 10KB max template size
        if len(v) > max_size:
            raise ValueError(f"Template exceeds max size of {max_size} bytes")
        return v

    @field_validator('id')
    @classmethod
    def validate_id_format(cls, v: str) -> str:
        """Enforce ID format: lowercase alphanumeric with dashes."""
        if not re.match(r'^[a-z0-9-]+$', v):
            raise ValueError("ID must be lowercase alphanumeric with dashes")
        return v


class PromptBundle(BaseModel):
    """Schema for the entire prompt bundle."""
    version: str
    updated_at: Optional[str] = None
    prompts: List[PromptDefinition]

    @field_validator('prompts')
    @classmethod
    def validate_prompt_count(cls, v: List[PromptDefinition]) -> List[PromptDefinition]:
        """Enforce maximum prompt count."""
        max_prompts = 100
        if len(v) > max_prompts:
            raise ValueError(f"Too many prompts (max {max_prompts})")
        return v


# =============================================================================
# Safety Guardrails
# =============================================================================

# Patterns that suggest prompt injection attempts
FORBIDDEN_PATTERNS = [
    "ignore previous",
    "disregard all",
    "forget your instructions",
    "you are now",
    "act as if",
    "pretend you are",
    "override",
    "bypass",
]


def validate_prompt_safety(template: str) -> tuple[bool, Optional[str]]:
    """
    Check template doesn't contain prompt injection patterns.

    Returns:
        (is_safe, violation_message)
    """
    lower_template = template.lower()
    for pattern in FORBIDDEN_PATTERNS:
        if pattern in lower_template:
            return False, f"Template contains forbidden pattern: '{pattern}'"
    return True, None


# =============================================================================
# Prompt Registry
# =============================================================================

@dataclass
class PromptRegistry:
    """
    Registry for managing MCP prompts with versioning and hot-reload.

    Usage:
        registry = PromptRegistry(prompts_path=Path("/etc/mcp/prompts.yaml"))
        registry.load()

        # Get all prompts
        for prompt in registry.get_all_prompts():
            print(f"{prompt.id}: {prompt.name}")

        # Get specific prompt
        prompt = registry.get_prompt("my-prompt-id")

        # Check for updates (call periodically)
        if registry.check_for_updates():
            print("Prompts were reloaded")
    """

    prompts_path: Path
    bundle: Optional[PromptBundle] = None
    bundle_hash: str = ""
    loaded_at: Optional[datetime] = None
    _file_mtime: float = 0.0

    def load(self, validate_safety: bool = True) -> bool:
        """
        Load prompts from file.

        Args:
            validate_safety: Whether to check for prompt injection patterns

        Returns:
            True if bundle changed, False otherwise
        """
        if not self.prompts_path.exists():
            logger.warning(f"Prompts file not found: {self.prompts_path}")
            return False

        try:
            # Check if file changed (optimization)
            current_mtime = self.prompts_path.stat().st_mtime
            if current_mtime == self._file_mtime and self.bundle is not None:
                return False  # No change

            # Load and parse file
            content = self.prompts_path.read_text()

            if self.prompts_path.suffix in ['.yaml', '.yml']:
                if not HAS_YAML:
                    raise ImportError(
                        "PyYAML required for YAML files. "
                        "Install with: pip install pyyaml"
                    )
                data = yaml.safe_load(content)
            else:
                data = json.loads(content)

            # Validate against schema
            new_bundle = PromptBundle(**data)

            # Safety validation
            if validate_safety:
                for prompt in new_bundle.prompts:
                    is_safe, violation = validate_prompt_safety(prompt.template)
                    if not is_safe:
                        logger.error(
                            f"Prompt '{prompt.id}' failed safety validation: {violation}"
                        )
                        return False

            # Compute hash
            new_hash = self._compute_hash(content)

            # Check if content actually changed
            if new_hash == self.bundle_hash:
                self._file_mtime = current_mtime
                return False

            # Update registry
            self.bundle = new_bundle
            self.bundle_hash = new_hash
            self._file_mtime = current_mtime
            self.loaded_at = datetime.now(timezone.utc)

            logger.info(
                f"Loaded {len(new_bundle.prompts)} prompts, "
                f"version={new_bundle.version}, hash={new_hash[:16]}..."
            )
            return True

        except Exception as e:
            logger.error(f"Failed to load prompts: {e}")
            return False

    def _compute_hash(self, content: str) -> str:
        """Compute SHA256 hash of bundle content."""
        return hashlib.sha256(content.encode()).hexdigest()

    def get_manifest(self) -> Dict[str, Any]:
        """
        Get bundle manifest for clients.

        Returns dict with version, hash, timestamps, and prompt IDs.
        Useful for caching and change detection.
        """
        if not self.bundle:
            return {
                "error": "No prompts loaded",
                "loaded": False
            }

        return {
            "version": self.bundle.version,
            "bundle_hash": f"sha256:{self.bundle_hash}",
            "updated_at": self.bundle.updated_at or (
                self.loaded_at.isoformat() if self.loaded_at else None
            ),
            "prompt_count": len(self.bundle.prompts),
            "prompt_ids": [p.id for p in self.bundle.prompts],
            "loaded": True
        }

    def get_prompt(self, prompt_id: str) -> Optional[PromptDefinition]:
        """Get a specific prompt by ID."""
        if not self.bundle:
            return None
        for prompt in self.bundle.prompts:
            if prompt.id == prompt_id:
                return prompt
        return None

    def get_all_prompts(self) -> List[PromptDefinition]:
        """Get all prompts."""
        return self.bundle.prompts if self.bundle else []

    def check_for_updates(self) -> bool:
        """
        Check if file changed and reload if needed.

        Call this periodically (e.g., every 30 seconds) to pick up
        ConfigMap changes without restarting.
        """
        if not self.prompts_path.exists():
            return False

        current_mtime = self.prompts_path.stat().st_mtime
        if current_mtime != self._file_mtime:
            return self.load()
        return False

    def render_prompt(
        self,
        prompt_id: str,
        arguments: Dict[str, Any]
    ) -> tuple[Optional[str], Optional[str]]:
        """
        Render a prompt template with provided arguments.

        Args:
            prompt_id: The prompt ID to render
            arguments: Dict of argument name -> value

        Returns:
            (rendered_template, error_message)
        """
        prompt = self.get_prompt(prompt_id)
        if not prompt:
            return None, f"Prompt '{prompt_id}' not found"

        # Check required arguments
        for arg in prompt.arguments:
            if arg.required and arg.name not in arguments:
                return None, f"Missing required argument: '{arg.name}'"

        # Simple template rendering (replace {{ name }} with value)
        template = prompt.template
        for key, value in arguments.items():
            # Support both {{ key }} and {{key}} syntax
            template = template.replace(f"{{{{ {key} }}}}", str(value))
            template = template.replace(f"{{{{{key}}}}}", str(value))

        return template, None


# =============================================================================
# Global Registry Instance
# =============================================================================

_prompt_registry: Optional[PromptRegistry] = None


def get_prompt_registry() -> PromptRegistry:
    """
    Get or create the prompt registry singleton.

    Uses PROMPTS_PATH environment variable, defaulting to /etc/mcp/prompts.yaml
    """
    global _prompt_registry
    if _prompt_registry is None:
        prompts_path = Path(os.environ.get(
            "PROMPTS_PATH",
            "/etc/mcp/prompts.yaml"
        ))
        _prompt_registry = PromptRegistry(prompts_path=prompts_path)
        _prompt_registry.load()
    return _prompt_registry


def reload_prompt_registry() -> Dict[str, Any]:
    """
    Force reload the prompt registry.

    Returns the new manifest.
    """
    registry = get_prompt_registry()
    changed = registry.load()
    manifest = registry.get_manifest()
    manifest["reloaded"] = changed
    return manifest