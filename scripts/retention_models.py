"""Typed contracts for the persisted retention cleanup state machine."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class RetentionJobValidationError(ValueError):
    """Raised when a persisted retention job violates its state contract."""


class RetentionPhase(str, Enum):
    PREPARING_SNAPSHOT = "preparing_snapshot"
    SNAPSHOT_PREPARED = "snapshot_prepared"
    PLANNING = "planning"
    DERIVED_RESET_PENDING = "derived_reset_pending"
    DERIVED_RESET_COMPLETE = "derived_reset_complete"
    PLANNED = "planned"
    LOGICAL_DELETE_COMMITTED = "logical_delete_committed"
    DERIVED_REBUILD_REQUIRED = "derived_rebuild_required"
    PHYSICAL_DELETE_PENDING = "physical_delete_pending"
    FAILED = "failed"
    COMPLETE = "complete"


DERIVED_REBUILD_PHASES = frozenset(
    {
        RetentionPhase.DERIVED_RESET_PENDING,
        RetentionPhase.DERIVED_RESET_COMPLETE,
        RetentionPhase.DERIVED_REBUILD_REQUIRED,
    }
)
PRE_DERIVED_RESET_PHASES = frozenset(
    {
        RetentionPhase.PREPARING_SNAPSHOT,
        RetentionPhase.SNAPSHOT_PREPARED,
        RetentionPhase.PLANNING,
    }
)

ALLOWED_TRANSITIONS: dict[RetentionPhase, frozenset[RetentionPhase]] = {
    RetentionPhase.PREPARING_SNAPSHOT: frozenset({RetentionPhase.SNAPSHOT_PREPARED}),
    RetentionPhase.SNAPSHOT_PREPARED: frozenset({RetentionPhase.PLANNING}),
    RetentionPhase.PLANNING: frozenset({RetentionPhase.DERIVED_RESET_PENDING}),
    RetentionPhase.DERIVED_RESET_PENDING: frozenset(
        {RetentionPhase.DERIVED_RESET_COMPLETE, RetentionPhase.FAILED}
    ),
    RetentionPhase.DERIVED_RESET_COMPLETE: frozenset(
        {RetentionPhase.PLANNED, RetentionPhase.FAILED}
    ),
    RetentionPhase.PLANNED: frozenset(
        {
            RetentionPhase.LOGICAL_DELETE_COMMITTED,
            RetentionPhase.PHYSICAL_DELETE_PENDING,
            RetentionPhase.FAILED,
        }
    ),
    RetentionPhase.LOGICAL_DELETE_COMMITTED: frozenset(
        {RetentionPhase.DERIVED_REBUILD_REQUIRED, RetentionPhase.FAILED}
    ),
    RetentionPhase.DERIVED_REBUILD_REQUIRED: frozenset(
        {RetentionPhase.PHYSICAL_DELETE_PENDING, RetentionPhase.FAILED}
    ),
    RetentionPhase.PHYSICAL_DELETE_PENDING: frozenset(
        {RetentionPhase.DERIVED_REBUILD_REQUIRED, RetentionPhase.FAILED}
    ),
    RetentionPhase.FAILED: frozenset(
        {RetentionPhase.DERIVED_REBUILD_REQUIRED, RetentionPhase.PHYSICAL_DELETE_PENDING}
    ),
    # COMPLETE was written by an older cleanup flow. New jobs never enter it,
    # but recovery must be able to normalize an existing marker.
    RetentionPhase.COMPLETE: frozenset(
        {RetentionPhase.DERIVED_REBUILD_REQUIRED, RetentionPhase.PHYSICAL_DELETE_PENDING}
    ),
}

_KNOWN_FIELDS = frozenset(
    {
        "phase",
        "operation_job_id",
        "failed_stage",
        "cutoff_unix",
        "deleted_rows",
        "physical_delete_pending",
        "derived_rebuild_required",
        "recovery_required",
        "pending_files",
        "pruned_state_job_id",
        "pruned_state_commit_ready",
        "pruned_state_commit_recovered",
    }
)


def _optional_string(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RetentionJobValidationError(f"retention job field {key!r} must be a string")
    return value


def _optional_bool(payload: Mapping[str, Any], key: str) -> bool | None:
    if key not in payload:
        return None
    value = payload[key]
    if not isinstance(value, bool):
        raise RetentionJobValidationError(f"retention job field {key!r} must be a boolean")
    return value


def _optional_non_negative_int(payload: Mapping[str, Any], key: str) -> int | None:
    if key not in payload:
        return None
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RetentionJobValidationError(f"retention job field {key!r} must be a non-negative integer")
    return value


def _optional_number(payload: Mapping[str, Any], key: str) -> int | float | None:
    if key not in payload:
        return None
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RetentionJobValidationError(f"retention job field {key!r} must be a number")
    return value


@dataclass(frozen=True)
class RetentionJob:
    """Validated retention job while preserving forward-compatible extra fields."""

    phase: RetentionPhase
    operation_job_id: str | None = None
    failed_stage: str | None = None
    cutoff_unix: int | float | None = None
    deleted_rows: int | None = None
    physical_delete_pending: bool | None = None
    derived_rebuild_required: bool | None = None
    recovery_required: bool | None = None
    pending_files: int | None = None
    pruned_state_job_id: str | None = None
    pruned_state_commit_ready: bool | None = None
    pruned_state_commit_recovered: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    _present_fields: frozenset[str] = field(default_factory=frozenset, repr=False, compare=False)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "RetentionJob":
        if not isinstance(payload, Mapping):
            raise RetentionJobValidationError("retention job must be an object")
        raw_phase = payload.get("phase")
        try:
            phase = raw_phase if isinstance(raw_phase, RetentionPhase) else RetentionPhase(raw_phase)
        except (TypeError, ValueError) as exc:
            raise RetentionJobValidationError(f"unknown retention job phase: {raw_phase!r}") from exc
        return cls(
            phase=phase,
            operation_job_id=_optional_string(payload, "operation_job_id"),
            failed_stage=_optional_string(payload, "failed_stage"),
            cutoff_unix=_optional_number(payload, "cutoff_unix"),
            deleted_rows=_optional_non_negative_int(payload, "deleted_rows"),
            physical_delete_pending=_optional_bool(payload, "physical_delete_pending"),
            derived_rebuild_required=_optional_bool(payload, "derived_rebuild_required"),
            recovery_required=_optional_bool(payload, "recovery_required"),
            pending_files=_optional_non_negative_int(payload, "pending_files"),
            pruned_state_job_id=_optional_string(payload, "pruned_state_job_id"),
            pruned_state_commit_ready=_optional_bool(payload, "pruned_state_commit_ready"),
            pruned_state_commit_recovered=_optional_bool(payload, "pruned_state_commit_recovered"),
            extra={key: value for key, value in payload.items() if key not in _KNOWN_FIELDS},
            _present_fields=frozenset(key for key in payload if key in _KNOWN_FIELDS),
        )

    @classmethod
    def begin(cls, *, operation_job_id: str, cutoff_unix: int | float, **fields: Any) -> "RetentionJob":
        job = cls.from_payload(
            {
                "phase": RetentionPhase.PREPARING_SNAPSHOT,
                "operation_job_id": operation_job_id,
                "cutoff_unix": cutoff_unix,
                "derived_rebuild_required": False,
                "recovery_required": True,
                "physical_delete_pending": False,
                **fields,
            }
        )
        return job.validate_for_write()

    @classmethod
    def create_at(cls, phase: RetentionPhase, **fields: Any) -> "RetentionJob":
        """Create a validated marker for a boundary that has no prior marker.

        Normal retention execution must use begin(). This constructor exists for
        the standalone logical-delete boundary and recovery fixtures.
        """

        return cls.from_payload({"phase": phase, **fields}).validate_for_write()

    def to_payload(self) -> dict[str, Any]:
        payload = dict(self.extra)
        payload["phase"] = self.phase.value
        for key in (
            "operation_job_id",
            "failed_stage",
            "cutoff_unix",
            "deleted_rows",
            "physical_delete_pending",
            "derived_rebuild_required",
            "recovery_required",
            "pending_files",
            "pruned_state_job_id",
            "pruned_state_commit_ready",
            "pruned_state_commit_recovered",
        ):
            value = getattr(self, key)
            if value is not None or key in self._present_fields:
                payload[key] = value
        return payload

    def updated(self, **changes: Any) -> "RetentionJob":
        payload = self.to_payload()
        for key, value in changes.items():
            payload[key] = value
        return self.from_payload(payload)

    def without(self, *keys: str) -> "RetentionJob":
        payload = self.to_payload()
        for key in keys:
            payload.pop(key, None)
        return self.from_payload(payload)

    def validate_for_write(self) -> "RetentionJob":
        if self.phase is RetentionPhase.COMPLETE:
            raise RetentionJobValidationError("retention phase 'complete' is legacy read-only")
        if not self.operation_job_id:
            raise RetentionJobValidationError("retention job operation_job_id is required")
        if self.cutoff_unix is None:
            raise RetentionJobValidationError("retention job cutoff_unix is required")
        if self.recovery_required is not True:
            raise RetentionJobValidationError("persisted retention job must require recovery")
        if self.pruned_state_commit_ready is True and not self.pruned_state_job_id:
            raise RetentionJobValidationError("pruned_state_commit_ready requires pruned_state_job_id")
        if self.pruned_state_commit_recovered is True and self.pruned_state_commit_ready is True:
            raise RetentionJobValidationError("recovered pruned state cannot remain commit-ready")

        if self.physical_delete_pending is True:
            if self.pending_files is None or self.pending_files < 1:
                raise RetentionJobValidationError("physical delete pending requires pending_files >= 1")
        elif self.pending_files not in {None, 0}:
            raise RetentionJobValidationError("pending_files requires physical_delete_pending")

        if self.phase in PRE_DERIVED_RESET_PHASES:
            if self.derived_rebuild_required is not False:
                raise RetentionJobValidationError(f"phase {self.phase.value!r} must precede derived rebuild")
            if self.physical_delete_pending is not False:
                raise RetentionJobValidationError(f"phase {self.phase.value!r} cannot have physical deletion pending")
        elif self.phase in {
            RetentionPhase.DERIVED_RESET_PENDING,
            RetentionPhase.DERIVED_RESET_COMPLETE,
            RetentionPhase.PLANNED,
            RetentionPhase.LOGICAL_DELETE_COMMITTED,
            RetentionPhase.DERIVED_REBUILD_REQUIRED,
        }:
            if self.derived_rebuild_required is not True:
                raise RetentionJobValidationError(f"phase {self.phase.value!r} requires derived rebuild")
            if self.phase is not RetentionPhase.DERIVED_REBUILD_REQUIRED and self.physical_delete_pending is not False:
                raise RetentionJobValidationError(f"phase {self.phase.value!r} cannot have physical deletion pending")

        if self.phase in {
            RetentionPhase.DERIVED_RESET_PENDING,
            RetentionPhase.DERIVED_RESET_COMPLETE,
            RetentionPhase.PLANNED,
            RetentionPhase.LOGICAL_DELETE_COMMITTED,
            RetentionPhase.DERIVED_REBUILD_REQUIRED,
            RetentionPhase.PHYSICAL_DELETE_PENDING,
            RetentionPhase.FAILED,
        } and self.deleted_rows is None:
            raise RetentionJobValidationError(f"phase {self.phase.value!r} requires deleted_rows")
        if self.phase is RetentionPhase.FAILED:
            if not self.failed_stage:
                raise RetentionJobValidationError("failed retention job requires failed_stage")
            if self.derived_rebuild_required is not True:
                raise RetentionJobValidationError("failed retention job requires derived rebuild")
        return self

    def transition(
        self,
        next_phase: RetentionPhase,
        *,
        clear_fields: tuple[str, ...] = (),
        **changes: Any,
    ) -> "RetentionJob":
        if next_phase is not self.phase and next_phase not in ALLOWED_TRANSITIONS[self.phase]:
            raise RetentionJobValidationError(
                f"retention transition {self.phase.value!r} -> {next_phase.value!r} is not allowed"
            )
        payload = self.to_payload()
        for key in clear_fields:
            payload.pop(key, None)
        payload.update(changes)
        payload["phase"] = next_phase
        return self.from_payload(payload).validate_for_write()

    @property
    def requires_derived_rebuild(self) -> bool:
        return self.derived_rebuild_required is True or self.phase in DERIVED_REBUILD_PHASES

    @property
    def is_pre_derived_reset(self) -> bool:
        return self.phase in PRE_DERIVED_RESET_PHASES
