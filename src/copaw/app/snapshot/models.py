# -*- coding: utf-8 -*-
"""Data models for the snapshot system."""
from __future__ import annotations

import platform
import sys
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from ...__version__ import __version__ as copaw_version

MANIFEST_SCHEMA_VERSION = 1


class SnapshotScope(str, Enum):
    """Scope of a snapshot."""

    SINGLE = "single"
    SELECTED = "selected"
    ALL = "all"


class RestoreMode(str, Enum):
    """Mode for restoring a snapshot."""

    IN_PLACE = "in_place"
    CLONE = "clone"


class AgentStatus(str, Enum):
    """Post-import agent health status."""

    READY = "ready"
    NEEDS_REVIEW = "needs_review"
    NEEDS_SETUP = "needs_setup"


class RestorePhase(str, Enum):
    """Phases of the three-stage restore state machine."""

    PREPARING = "preparing"
    PREPARED = "prepared"
    APPLYING = "applying"
    APPLIED = "applied"
    VERIFYING = "verifying"


class SnapshotManifest(BaseModel):
    """Metadata stored inside each snapshot ZIP as manifest.json."""

    schema_version: int = MANIFEST_SCHEMA_VERSION
    copaw_version: str = Field(default_factory=lambda: copaw_version)
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )
    agent_ids: List[str] = Field(default_factory=list)
    scope: SnapshotScope = SnapshotScope.SINGLE
    original_platform: str = Field(default_factory=lambda: sys.platform)
    python_version: str = Field(
        default_factory=lambda: platform.python_version(),
    )
    source_hint: str = "local"
    includes_secrets: bool = False
    includes_global: bool = False
    file_checksums: Dict[str, str] = Field(default_factory=dict)
    notes: str = ""

    def to_filename_scope(self) -> str:
        if self.scope == SnapshotScope.SINGLE and self.agent_ids:
            return self.agent_ids[0]
        if self.scope == SnapshotScope.ALL:
            return "all"
        return "selected"


class SnapshotInfo(BaseModel):
    """Summary info returned to API callers (not stored in ZIP)."""

    snapshot_id: str
    filename: str
    scope: SnapshotScope
    agent_ids: List[str]
    created_at: str
    size_bytes: int
    includes_secrets: bool
    includes_global: bool
    notes: str = ""


class RestoreState(BaseModel):
    """Persisted state for crash recovery during restore."""

    phase: RestorePhase
    agent_id: str
    workspace_dir: str
    backup_dir: str
    staging_dir: str
    snapshot_id: str
    started_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )
    last_completed_step: str = ""


class CreateSnapshotRequest(BaseModel):
    """Request body for creating a snapshot."""

    scope: SnapshotScope = SnapshotScope.SINGLE
    agent_ids: List[str] = Field(default_factory=list)
    include_secrets: bool = False
    include_global: bool = False
    exclude_sessions: bool = False
    exclude_memory: bool = False
    note: str = ""


class RestoreSnapshotRequest(BaseModel):
    """Request body for restoring a snapshot."""

    agent_id: str
    mode: RestoreMode = RestoreMode.IN_PLACE
    new_agent_id: Optional[str] = None


class TodoItem(BaseModel):
    """A single post-import checklist item."""

    severity: str  # "required" or "suggested"
    message: str
    action: str = ""


class PerAgentImportOutcome(BaseModel):
    """Per-agent row for a multi-agent import."""

    source_agent_id: str
    target_agent_id: str
    status: AgentStatus
    file_summary: Dict[str, str] = Field(default_factory=dict)
    todos: List[TodoItem] = Field(default_factory=list)


class ImportResult(BaseModel):
    """Result returned after importing a snapshot."""

    agent_id: str
    status: AgentStatus
    file_summary: Dict[str, str] = Field(default_factory=dict)
    todos: List[TodoItem] = Field(default_factory=list)
    agent_outcomes: List[PerAgentImportOutcome] = Field(default_factory=list)
