from enum import Enum


class AgentStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"


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


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    APPROVAL_REQUIRED = "approval_required"
    DENY = "deny"


class ActorType(str, Enum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"
    WALLET = "wallet"


class PaymentStatus(str, Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    SETTLED = "settled"
    DENIED = "denied"
    FAILED = "failed"


class AuditEvent(str, Enum):
    RUN_CREATED = "run_created"
    AGENT_STARTED = "agent_started"
    TOOL_REQUESTED = "tool_requested"
    POLICY_EVALUATED = "policy_evaluated"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_REJECTED = "approval_rejected"
    X402_PAYMENT_REQUIRED = "x402_payment_required"
    WALLET_EVALUATED = "wallet_evaluated"
    X402_SETTLED = "x402_settled"
    X402_FAILED = "x402_failed"
    TOOL_EXECUTED = "tool_executed"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    PROMPT_UPDATED = "prompt_updated"
    CREDENTIAL_ACCESSED = "credential_accessed"
