from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class AgentCreate(BaseModel):
    id: str | None = None
    name: str
    description: str = ""
    model: str = "claude-sonnet"


class AgentRead(ORMModel):
    id: str
    name: str
    description: str
    model: str
    organization_id: str | None = None
    connected: bool = False
    enrolled: bool = False
    runtime_id: str | None = None
    last_seen_at: datetime | None = None
    settings: dict[str, Any] = Field(default_factory=dict)
    tool_set_id: str | None = None
    profile_id: str | None = None
    origin: str | None = None
    client_id: str | None = None
    registered_user_id: str | None = None
    registered_user_name: str | None = None
    registered_user_email: str | None = None
    pushed: bool | None = None


class AgentUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    model: str | None = None
    runtime_id: str | None = None
    settings: dict[str, Any] | None = None
    tool_set_id: str | None = None


class ToolRead(ORMModel):
    id: str
    name: str
    description: str
    type: str
    mcp_endpoint: str
    permissions: list[Any]
    mcp_server_id: str | None = None
    status: str


class McpToolRead(BaseModel):
    id: str
    name: str
    description: str = ""


class McpServerRead(ORMModel):
    id: str
    name: str
    endpoint: str
    status: str
    connected: bool = False
    has_authorization: bool = False
    secret_status: str = "ok"
    gateway: str = "mcp-gateway"
    organization_id: str | None = None
    tools: list[str] = Field(default_factory=list)
    catalog: list[McpToolRead] = Field(default_factory=list)


class McpConnect(BaseModel):
    name: str
    endpoint: str
    authorization: str | None = None


class McpOAuthBegin(BaseModel):
    name: str
    endpoint: str
    nonce: str


class McpOAuthStart(BaseModel):
    authorization_url: str | None = None
    server: McpServerRead | None = None


class ToolSetItemWrite(BaseModel):
    tool_id: str
    approval: bool = False


class ToolSetWrite(BaseModel):
    name: str
    description: str = ""
    items: list[ToolSetItemWrite] = Field(default_factory=list)


class ToolSetUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    items: list[ToolSetItemWrite] | None = None


class ToolSetItemRead(BaseModel):
    tool_id: str
    name: str
    description: str = ""
    server_id: str | None = None
    server_name: str = ""
    approval: bool = False
    input_schema: dict[str, Any] = Field(default_factory=dict)


class ToolSetRead(BaseModel):
    id: str
    name: str
    description: str = ""
    items: list[ToolSetItemRead] = Field(default_factory=list)


class OrgUserRead(ORMModel):
    id: str
    user_id: str
    email: str
    name: str
    title: str
    role: str
    status: str


class EmployeeRead(ORMModel):
    id: str
    title: str
    status: str
    role: str = "operator"
    permissions: dict[str, str] = Field(default_factory=dict)


class PolicyConditionIn(BaseModel):
    field: str
    operator: str
    value: Any


class PolicyRuleIn(BaseModel):
    id: str | None = None
    tool: str = ""
    condition: str = ""
    conditions: list[PolicyConditionIn] = Field(default_factory=list)
    decision: str
    priority: int | None = None
    approval_policy_id: str | None = None


class PolicyRuleRead(ORMModel):
    id: str
    tool: str
    condition: str
    conditions: list[dict[str, Any]] = Field(default_factory=list)
    decision: str
    position: int = 0
    approval_policy_id: str | None = None


class PolicyCreate(BaseModel):
    id: str | None = None
    name: str
    description: str = ""
    status: str = "active"
    environment: str = "production"
    tool: str | None = None
    tool_set_id: str | None = None
    definition: dict[str, Any] | None = None
    agent_ids: list[str] = Field(default_factory=list)
    rules: list[PolicyRuleIn] = Field(default_factory=list)


class PolicyUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    status: str | None = None
    environment: str | None = None
    tool: str | None = None
    tool_set_id: str | None = None
    definition: dict[str, Any] | None = None
    agent_ids: list[str] | None = None
    rules: list[PolicyRuleIn] | None = None


class PolicyRead(ORMModel):
    id: str
    name: str
    description: str = ""
    status: str = "active"
    version: int = 1
    environment: str = "production"
    tool_set_id: str | None = None
    definition: dict[str, Any] | None = None
    agent_ids: list[str] = Field(default_factory=list)
    rules: list[PolicyRuleRead] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PolicyGenerate(BaseModel):
    tool_set_id: str
    name: str = ""
    instructions: str = ""


class PolicyActivate(BaseModel):
    confirm: bool = False


class PolicySimulate(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    agent_id: str | None = None


class PolicyOptimize(BaseModel):
    definition: dict[str, Any]
    instructions: str = ""


class RunCreate(BaseModel):
    agent_id: str
    input: str


class ActionRead(ORMModel):
    id: str
    run_id: str
    tool: str
    arguments: dict[str, Any]
    policy_decision: str | None
    policy_id: str | None = None
    policy_rule_id: str | None = None
    policy_version: int | None = None
    policy_reason: str | None = None
    status: str
    result: dict[str, Any] | None = None


class RunRead(ORMModel):
    id: str
    agent_id: str
    input: str
    status: str
    approval_id: str | None = None
    actions: list[ActionRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ApprovalRequirementProgress(BaseModel):
    group_id: str
    group_name: str = ""
    required_count: int
    approved_count: int = 0


class ApprovalRead(ORMModel):
    id: str
    run_id: str
    action_id: str
    status: str
    requested_amount: Decimal | float | None = None
    resolved_by: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None
    required_count: int = 1
    approved_count: int = 0
    requirements: list[ApprovalRequirementProgress] = []
    execution_state: str = "none"
    expires_at: datetime | None = None
    resubmit_expires_at: datetime | None = None
    expired_phase: str | None = None


class ApprovalDecisionRead(BaseModel):
    status: str
    action: str
    result: str | None = None


class ActivityEventRead(ORMModel):
    id: str
    event: str
    actor_type: str
    actor_id: str | None
    run_id: str | None
    action_id: str | None
    tool: str | None
    result: str | None
    payload: dict[str, Any] | None = None
    created_at: datetime


class EntitlementsRead(BaseModel):
    gateway: bool = True
    tool_set_limit: int | None = None
    policies: bool = False
    webhooks: bool = False
    groups: bool = False
    audit: str = "full"
    audit_days: int | None = None


class OrganizationRead(ORMModel):
    id: str
    name: str
    plan: str = "free"
    kind: str = "personal"
    entitlements: EntitlementsRead = Field(default_factory=EntitlementsRead)
    photo: str | None = None


class SubscriptionLineRead(BaseModel):
    kind: str
    id: str
    name: str
    amount: str


class SubscriptionInvoiceRead(BaseModel):
    id: str
    number: str
    period: str
    issued_at: datetime
    paid_at: datetime | None = None
    amount: str
    currency: str = "USD"
    status: str
    lines: list[SubscriptionLineRead] = Field(default_factory=list)


class SubscriptionPaymentRead(BaseModel):
    brand: str = ""
    last4: str = ""
    exp: str = ""
    demo: bool = False


class SubscriptionRead(BaseModel):
    organization_id: str
    organization_name: str
    status: str
    plan: SubscriptionLineRead
    renews_on: datetime
    payment: SubscriptionPaymentRead
    invoices: list[SubscriptionInvoiceRead] = Field(default_factory=list)


class McpGatewayRead(BaseModel):
    status: str
    connected: int
    total: int
    servers: list[dict[str, Any]] = Field(default_factory=list)


class UserRead(ORMModel):
    id: str
    email: str
    name: str
    photo: str | None = None
    status: str
    is_platform_admin: bool = False
    organization: OrganizationRead
    enterprise: OrganizationRead
    employee: EmployeeRead | None = None
    agents: list[AgentRead] = Field(default_factory=list)
    mcp_servers: list[McpServerRead] = Field(default_factory=list)
    tool_sets: list[ToolSetRead] = Field(default_factory=list)
    users: list[OrgUserRead] = Field(default_factory=list)
    assigned_agent_ids: list[str] = Field(default_factory=list)
    assigned_mcp_server_ids: list[str] = Field(default_factory=list)
    agent_edit_ids: list[str] | None = None
    mcp_gateway: McpGatewayRead | None = None
    permissions: dict[str, str] = Field(default_factory=dict)
    role: str | None = None
    profile_id: str = ""
    locale: str = "zh-Hant"
    email_change_pending: bool = False


class AccessPermissions(BaseModel):
    agents: str = "none"
    policies: str = "none"
    approvals: str = "none"
    team: str = "none"


class MemberInviteRequest(BaseModel):
    email: str
    name: str
    title: str = "Operator"
    role: str = "operator"
    permissions: AccessPermissions | None = None
    agent_ids: list[str] = Field(default_factory=list)
    mcp_server_ids: list[str] = Field(default_factory=list)


class MemberUpdateRequest(BaseModel):
    name: str | None = None
    title: str | None = None
    role: str | None = None
    permissions: AccessPermissions | None = None
    status: str | None = None
    agent_ids: list[str] | None = None
    agent_edit_ids: list[str] | None = None
    mcp_server_ids: list[str] | None = None


class MeUpdateRequest(BaseModel):
    name: str | None = None
    title: str | None = None


class MemberPasswordResetRequest(BaseModel):
    password: str


class MemberRead(ORMModel):
    id: str
    user_id: str
    email: str
    name: str
    title: str
    role: str
    status: str
    permissions: dict[str, str]
    agent_ids: list[str] = Field(default_factory=list)
    agent_edit_ids: list[str] | None = None
    mcp_server_ids: list[str] = Field(default_factory=list)
    created_at: datetime


class InviteRead(ORMModel):
    id: str
    email: str
    name: str
    title: str
    role: str
    status: str
    permissions: dict[str, str]
    agent_ids: list[str] = Field(default_factory=list)
    mcp_server_ids: list[str] = Field(default_factory=list)
    invited_by_user_id: str
    expires_at: datetime
    created_at: datetime
    token: str | None = None


class InviteCreateResponse(InviteRead):
    token: str


class AcceptInviteRequest(BaseModel):
    token: str
    password: str


class OrgWorkspaceRead(BaseModel):
    organization: OrganizationRead
    catalog: dict
    me: MemberRead | None = None
    agents: list[AgentRead] = Field(default_factory=list)
    mcp_servers: list[McpServerRead] = Field(default_factory=list)
    users: list[OrgUserRead] = Field(default_factory=list)
    mcp_gateway: McpGatewayRead | None = None


class AdminStatsRead(BaseModel):
    organizations: int
    users: int
    agents: int
    connections: int
    personal: int = 0
    teams: int = 0


class AdminServiceStatus(BaseModel):
    id: str
    name: str
    status: str


class AdminEventRead(ORMModel):
    id: str
    event: str
    actor_type: str
    actor_id: str | None = None
    organization_id: str | None = None
    organization_name: str | None = None
    result: str | None = None
    payload: dict[str, Any] | None = None
    created_at: datetime


class AdminOverviewRead(BaseModel):
    stats: AdminStatsRead
    services: list[AdminServiceStatus]
    events: list[AdminEventRead] = Field(default_factory=list)


class AdminOrgSummary(ORMModel):
    id: str
    name: str
    status: str = "active"
    plan: str = "free"
    kind: str = "personal"
    deployment_mode: str = "cloud"
    runtime_region: str = "asia-east1"
    runtime_version: str = "0.1.0"
    created_at: datetime
    user_count: int
    agent_count: int
    tool_count: int = 0
    connection_count: int = 0
    runtime_status: str = "disconnected"
    mcp_status: str = "none"
    last_heartbeat_at: datetime | None = None


class AdminOrgOnboardRequest(BaseModel):
    name: str
    admin_email: str
    admin_name: str
    admin_password: str
    admin_title: str = "Admin"
    plan: str = "free"
    kind: str | None = None


class AdminOrgUpdateRequest(BaseModel):
    name: str | None = None
    status: str | None = None
    plan: str | None = None
    deployment_mode: str | None = None
    runtime_region: str | None = None


class AdminHealthCheck(BaseModel):
    runtime: str = "disconnected"
    mcp: str = "none"
    agents: str = "none"
    policies: str = "none"
    database: str = "ok"
    score: int = 100
    issues: list[str] = Field(default_factory=list)


class AdminMemberCreateRequest(BaseModel):
    email: str
    name: str
    password: str
    title: str = "Operator"
    role: str = "operator"
    permissions: AccessPermissions | None = None
    status: str = "active"
    agent_ids: list[str] = Field(default_factory=list)
    mcp_server_ids: list[str] = Field(default_factory=list)


class AdminOrgDetail(AdminOrgSummary):
    members: list[MemberRead] = Field(default_factory=list)
    agents: list[AgentRead] = Field(default_factory=list)
    health: AdminHealthCheck = Field(default_factory=AdminHealthCheck)
    mcp_servers: list[McpServerRead] = Field(default_factory=list)


class AdminConnectionChannel(BaseModel):
    id: str
    name: str
    status: str
    last_heartbeat_at: datetime | None = None
    version: str | None = None
    detail: str | None = None


class AdminOrgConnections(BaseModel):
    organization_id: str
    organization_name: str
    channels: list[AdminConnectionChannel]


class AdminRuntimeRead(BaseModel):
    id: str
    organization_id: str
    organization_name: str
    status: str
    region: str
    version: str
    deployment_mode: str
    active_runs: int
    agent_count: int
    last_heartbeat_at: datetime | None = None
    last_error: str | None = None


class AdminMcpSummary(BaseModel):
    servers: int
    tools: int
    healthy: int
    errors: int
    items: list[dict[str, Any]] = Field(default_factory=list)


class AdminUsageRow(BaseModel):
    organization_id: str
    organization_name: str
    plan: str
    kind: str = "personal"
    runs: int
    tool_calls: int


class AdminSettingsRead(BaseModel):
    platform: dict[str, Any]
    security: dict[str, Any]
    infrastructure: dict[str, Any]
    admins: list[AdminUserRead] = Field(default_factory=list)


class AdminUserRead(ORMModel):
    id: str
    email: str
    name: str
    status: str
    is_platform_admin: bool
    organization_id: str
    organization_name: str
    employee_id: str | None = None
    title: str | None = None
    role: str | None = None
    created_at: datetime


class ReauthRequest(BaseModel):
    password: str | None = None
    code: str | None = None
    credential: dict[str, Any] | None = None


class EmailChangeConfirm(BaseModel):
    current_code: str
    next_code: str


class ProfileUpdate(BaseModel):
    name: str
    email: str


class PhotoUpdate(BaseModel):
    photo: str | None = None


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class ForgotPasswordRequest(BaseModel):
    email: str
    locale: str | None = None


class ResetPasswordRequest(BaseModel):
    email: str
    code: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str
    locale: str | None = None
    legacy_password: str | None = None
    legacy_v1: str | None = None


class SignupCodeRequest(BaseModel):
    name: str
    email: str
    password: str
    locale: str | None = None


class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str
    code: str
    locale: str | None = None


class LocaleUpdate(BaseModel):
    locale: str


class LoginChallengeRequest(BaseModel):
    challenge: str


class LoginEmailRequest(BaseModel):
    challenge: str
    code: str


class LoginPasskeyRequest(BaseModel):
    challenge: str
    credential: dict


class MfaCodeRequest(BaseModel):
    code: str


class LocaleRequest(BaseModel):
    locale: str | None = None


class PasskeyCreateRequest(BaseModel):
    credential: dict


class PasskeyRead(BaseModel):
    id: str
    created_at: datetime


class MfaStatus(BaseModel):
    email_mfa: bool
    passkeys: list[PasskeyRead] = Field(default_factory=list)


class NoticeSettings(BaseModel):
    approval: bool = False
    execute: bool = False
    audit: bool = False


class LoginResponse(BaseModel):
    token: str | None = None
    user: UserRead | None = None
    mfa_required: bool = False
    challenge: str | None = None
    methods: list[str] = Field(default_factory=list)


class CredentialRead(BaseModel):
    id: str
    kind: str
    name: str
    provider: str = ""
    model: str = ""
    base_url: str = ""
    secret_name: str = ""
    broker_id: str = ""
    allowed_paths: list[str] = Field(default_factory=list)
    has_secret: bool = False


class CredentialWrite(BaseModel):
    id: str | None = None
    kind: str
    name: str = ""
    provider: str = ""
    model: str = ""
    base_url: str = ""
    secret_name: str = ""
    broker_id: str = ""
    allowed_paths: list[str] = Field(default_factory=list)
    secret: str | None = None
