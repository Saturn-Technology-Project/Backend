from enum import Enum


class ToolStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class ToolType(str, Enum):
    MCP = "mcp"


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ActionStatus(str, Enum):
    PENDING = "pending"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    DENIED = "denied"
    FAILED = "failed"
    REJECTED = "rejected"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    INVALIDATED = "invalidated"


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    APPROVAL_REQUIRED = "approval_required"
    DENY = "deny"


class ActorType(str, Enum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"


class AuditEvent(str, Enum):
    RUN_CREATED = "run_created"
    AGENT_STARTED = "agent_started"
    TOOL_REQUESTED = "tool_requested"
    POLICY_EVALUATED = "policy_evaluated"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_QUORUM = "approval_quorum"
    APPROVAL_INVALIDATED = "approval_invalidated"
    EXECUTION_CLAIMED = "execution_claimed"
    GROUP_UPDATED = "group_updated"
    TOOL_EXECUTED = "tool_executed"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    PROMPT_UPDATED = "prompt_updated"
    CREDENTIAL_ACCESSED = "credential_accessed"
