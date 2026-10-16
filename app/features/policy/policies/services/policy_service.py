from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.ids import new_id, utcnow
from app.models import Agent, Policy, PolicyRule
from app.shared.models.associations import agent_policies
from app.schemas import PolicyCreate, PolicyRead, PolicyRuleIn, PolicyRuleRead, PolicyUpdate
from app.features.policy.policies.services.policy_engine import compile_condition, normalize_decision, parse_condition_string
from app.features.policy.policies.services.policy_graph import compile_definition, normalize_definition


_STATUSES = {"active", "paused", "draft", "validated"}


def _status(raw: str | None) -> str:
    value = (raw or "active").strip().lower()
    if value not in _STATUSES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown policy status")
    return value


def _slug_id(prefix: str, name: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in name).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return f"{prefix}_{slug}" if slug else new_id(prefix)


def _rule_payload(rule: PolicyRuleIn, fallback_tool: str | None) -> tuple[str, str, list[dict], str, int | None]:
    try:
        decision = normalize_decision(rule.decision).value
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    structured = [item.model_dump() for item in rule.conditions] if rule.conditions else []
    condition = (rule.condition or "").strip()
    if structured:
        condition = compile_condition(structured)
    elif condition:
        structured = parse_condition_string(condition)
    else:
        condition = "true"
    tool = (rule.tool or fallback_tool or "").strip()
    if not tool:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Each rule needs a tool")
    return tool, condition, structured, decision, rule.priority


def _approval_policy_id(rule: PolicyRule) -> str | None:
    attributes = rule.attributes if isinstance(rule.attributes, dict) else {}
    raw = attributes.get("approvalPolicyId") or attributes.get("approval_policy_id")
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def serialize_rule(rule: PolicyRule) -> PolicyRuleRead:
    structured = rule.conditions if isinstance(rule.conditions, list) else []
    if not structured:
        structured = parse_condition_string(rule.condition)
    return PolicyRuleRead(
        id=rule.id,
        tool=rule.tool,
        condition=rule.condition,
        conditions=structured,
        decision=rule.decision,
        position=rule.position,
        approval_policy_id=_approval_policy_id(rule),
    )


def _flat_conditions(expression: dict | None) -> list[dict] | None:
    if not isinstance(expression, dict):
        return None
    op = expression.get("op")
    if op == "true":
        return []
    if op == "cmp":
        field = str(expression.get("field") or "").strip()
        if not field:
            return []
        return [{"field": field, "operator": expression.get("operator") or "eq", "value": expression.get("value")}]
    if op == "and":
        parts: list[dict] = []
        for item in expression.get("args") or []:
            flat = _flat_conditions(item if isinstance(item, dict) else None)
            if flat is None:
                return None
            parts.extend(flat)
        return parts
    return None


def serialize_policy(policy: Policy) -> PolicyRead:
    rules = sorted(policy.rules, key=lambda item: item.position)
    definition = policy.definition if isinstance(policy.definition, dict) else None
    return PolicyRead(
        id=policy.id,
        name=policy.name,
        description=policy.description or "",
        status=policy.status or "active",
        version=policy.version or 1,
        environment=policy.environment or "production",
        tool_set_id=policy.tool_set_id,
        definition=definition,
        agent_ids=[agent.id for agent in policy.agents],
        rules=[serialize_rule(rule) for rule in rules],
        created_at=policy.created_at,
        updated_at=policy.updated_at,
    )


class PolicyService:
    async def list_policies(self, session: AsyncSession, organization_id: str) -> list[Policy]:
        result = await session.execute(
            select(Policy)
            .options(selectinload(Policy.rules), selectinload(Policy.agents))
            .where(Policy.organization_id == organization_id)
            .order_by(Policy.created_at.asc())
        )
        return list(result.scalars().unique().all())

    async def get_policy(
        self, session: AsyncSession, policy_id: str, organization_id: str
    ) -> Policy:
        result = await session.execute(
            select(Policy)
            .options(selectinload(Policy.rules), selectinload(Policy.agents))
            .where(Policy.id == policy_id)
        )
        policy = result.scalar_one_or_none()
        if policy is None or policy.organization_id != organization_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Policy not found")
        return policy

    async def delete_policy(self, session: AsyncSession, policy: Policy) -> None:
        await session.execute(delete(agent_policies).where(agent_policies.c.policy_id == policy.id))
        await session.execute(delete(PolicyRule).where(PolicyRule.policy_id == policy.id))
        await session.delete(policy)
        await session.flush()

    async def create_policy(
        self, session: AsyncSession, organization_id: str, body: PolicyCreate
    ) -> Policy:
        ident = body.id or _slug_id("policy", body.name)
        if await session.get(Policy, ident) is not None:
            ident = new_id("policy")
        definition = normalize_definition(body.definition) if body.definition is not None else None
        policy = Policy(
            id=ident,
            organization_id=organization_id,
            name=body.name.strip(),
            description=(body.description or "").strip(),
            status="draft" if definition is not None else _status(body.status),
            version=1,
            environment=(body.environment or "production").strip().lower(),
            tool_set_id=(body.tool_set_id or (definition or {}).get("toolSetId") or None),
            definition=definition,
        )
        session.add(policy)
        await session.flush()
        await self._set_agents(session, policy.id, organization_id, body.agent_ids)
        if definition is not None:
            await self._replace_compiled(session, policy.id, definition)
        else:
            await self._replace_rules(session, policy.id, body.rules, body.tool)
        await session.flush()
        return await self.get_policy(session, policy.id, organization_id)

    async def update_policy(
        self, session: AsyncSession, policy: Policy, body: PolicyUpdate
    ) -> Policy:
        changed = False
        if body.name is not None:
            policy.name = body.name.strip()
            changed = True
        if body.description is not None:
            policy.description = body.description.strip()
            changed = True
        if body.status is not None and body.definition is None and body.rules is None:
            next_status = _status(body.status)
            if next_status == "active" and (policy.status or "") != "validated":
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "Validate the policy before activating it",
                )
            policy.status = next_status
            changed = True
        if body.environment is not None:
            policy.environment = body.environment.strip().lower()
            changed = True
        if body.tool_set_id is not None:
            policy.tool_set_id = body.tool_set_id or None
            changed = True
        if body.agent_ids is not None:
            await self._set_agents(session, policy.id, policy.organization_id, body.agent_ids)
            changed = True
        if body.definition is not None:
            definition = normalize_definition(body.definition)
            policy.definition = definition
            if definition.get("toolSetId") and body.tool_set_id is None:
                policy.tool_set_id = definition["toolSetId"]
            await self._replace_compiled(session, policy.id, definition)
            if policy.status in {"active", "validated"}:
                policy.status = "draft"
            changed = True
        elif body.rules is not None:
            await self._replace_rules(session, policy.id, body.rules, body.tool)
            if policy.status in {"active", "validated"}:
                policy.status = "draft"
            changed = True
        if changed:
            policy.version = (policy.version or 1) + 1
            policy.updated_at = utcnow()
            await session.flush()
        return await self.get_policy(session, policy.id, policy.organization_id)

    async def _set_agents(
        self,
        session: AsyncSession,
        policy_id: str,
        organization_id: str,
        agent_ids: list[str],
    ) -> None:
        await session.execute(delete(agent_policies).where(agent_policies.c.policy_id == policy_id))
        agents = await self._load_agents(session, organization_id, agent_ids)
        if agents:
            await session.execute(
                insert(agent_policies),
                [{"agent_id": agent.id, "policy_id": policy_id} for agent in agents],
            )

    async def _load_agents(
        self, session: AsyncSession, organization_id: str, agent_ids: list[str]
    ) -> list[Agent]:
        if not agent_ids:
            return []
        result = await session.execute(
            select(Agent).where(
                Agent.organization_id == organization_id,
                Agent.id.in_(agent_ids),
            )
        )
        return list(result.scalars().all())

    async def _replace_rules(
        self,
        session: AsyncSession,
        policy_id: str,
        rules: list[PolicyRuleIn],
        fallback_tool: str | None,
    ) -> None:
        await session.execute(delete(PolicyRule).where(PolicyRule.policy_id == policy_id))
        await session.flush()
        for index, rule in enumerate(rules):
            tool, condition, structured, decision, priority = _rule_payload(rule, fallback_tool)
            attributes = {"approvalPolicyId": rule.approval_policy_id} if rule.approval_policy_id else None
            session.add(
                PolicyRule(
                    id=rule.id or new_id("rule"),
                    policy_id=policy_id,
                    tool=tool,
                    condition=condition,
                    conditions=structured or None,
                    decision=decision,
                    position=priority if priority is not None else index,
                    attributes=attributes,
                )
            )

    async def set_inventory(self, session: AsyncSession, organization_id: str, tool_set_id: str):
        from app.models import Tool, ToolSet, ToolSetItem

        result = await session.execute(
            select(ToolSet)
            .options(
                selectinload(ToolSet.items).selectinload(ToolSetItem.tool).selectinload(Tool.mcp_server)
            )
            .where(ToolSet.id == tool_set_id, ToolSet.organization_id == organization_id)
        )
        tool_set = result.scalar_one_or_none()
        if tool_set is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Capability set not found")
        inventory = []
        for item in tool_set.items or []:
            tool = item.tool
            if tool is None or tool.status != "active":
                continue
            server = tool.mcp_server
            inventory.append(
                {
                    "name": tool.name,
                    "description": tool.description or "",
                    "inputSchema": tool.input_schema or {},
                    "server": server.name if server is not None else "",
                    "origin": tool.type or "",
                }
            )
        if not inventory:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "This capability set has no tools")
        return tool_set, inventory

    async def generate_from_set(
        self, session: AsyncSession, organization_id: str, tool_set_id: str, name: str, instructions: str = ""
    ) -> Policy:
        from app.features.policy.policies.services.policy_graph import definition_from_analyses
        from app.features.policy.policies.services.policy_llm import analyze_tools

        tool_set, inventory = await self.set_inventory(session, organization_id, tool_set_id)
        analyses, used_model = await analyze_tools(inventory, instructions)
        definition = definition_from_analyses(tool_set.id, analyses)
        description = (
            "Draft proposed from the capability set. Review every tool, then validate and activate it."
            if used_model
            else "No policy model is configured. Every tool is unclassified and denied until you review it."
        )
        return await self.create_policy(
            session,
            organization_id,
            PolicyCreate(
                name=(name or f"{tool_set.name} policy").strip(),
                description=description,
                status="draft",
                tool_set_id=tool_set.id,
                definition=definition,
            ),
        )

    async def iter_generate(
        self, session: AsyncSession, organization_id: str, tool_set_id: str, name: str, instructions: str = ""
    ):
        from app.features.policy.policies.services.policy_graph import definition_from_analyses
        from app.features.policy.policies.services.policy_llm import iter_tool_analysis

        tool_set, inventory = await self.set_inventory(session, organization_id, tool_set_id)
        yield {"type": "status", "code": "reading", "name": tool_set.name, "count": len(inventory)}
        analyses = None
        used_model = False
        async for kind, payload in iter_tool_analysis(inventory, instructions):
            if kind == "think":
                yield {"type": "think", "text": payload}
            elif kind == "status":
                yield {"type": "status", **payload}
            elif kind == "result":
                analyses, used_model = payload
        if analyses is None:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Policy model returned an unreadable response")
        definition = definition_from_analyses(tool_set.id, analyses)
        description = (
            "Draft proposed from the capability set. Review every tool, then validate and activate it."
            if used_model
            else "No policy model is configured. Every tool is unclassified and denied until you review it."
        )
        policy = await self.create_policy(
            session,
            organization_id,
            PolicyCreate(
                name=(name or f"{tool_set.name} policy").strip(),
                description=description,
                status="draft",
                tool_set_id=tool_set.id,
                definition=definition,
            ),
        )
        await session.commit()
        yield {"type": "policy", "policy": policy}

    async def optimize_policy(
        self, session: AsyncSession, policy: Policy, definition: dict, instructions: str = ""
    ) -> dict:
        from app.features.policy.policies.services.policy_llm import optimize_definition

        set_id = ""
        if isinstance(definition, dict):
            set_id = str(definition.get("toolSetId") or definition.get("tool_set_id") or "")
        set_id = set_id or (policy.tool_set_id or "")
        inventory: list[dict] = []
        if set_id:
            from app.models import Tool, ToolSet, ToolSetItem

            result = await session.execute(
                select(ToolSet)
                .options(selectinload(ToolSet.items).selectinload(ToolSetItem.tool).selectinload(Tool.mcp_server))
                .where(ToolSet.id == set_id, ToolSet.organization_id == policy.organization_id)
            )
            tool_set = result.scalar_one_or_none()
            if tool_set is not None:
                for item in tool_set.items or []:
                    tool = item.tool
                    if tool is None or tool.status != "active":
                        continue
                    server = tool.mcp_server
                    inventory.append(
                        {
                            "name": tool.name,
                            "description": tool.description or "",
                            "inputSchema": tool.input_schema or {},
                            "server": server.name if server is not None else "",
                        }
                    )
        optimized, diff = await optimize_definition(definition, inventory, instructions)
        return {"definition": optimized, "diff": diff}

    async def iter_optimize(self, session: AsyncSession, policy: Policy, definition: dict, instructions: str = ""):
        from app.features.policy.policies.services.policy_llm import iter_optimize_definition

        set_id = ""
        if isinstance(definition, dict):
            set_id = str(definition.get("toolSetId") or definition.get("tool_set_id") or "")
        set_id = set_id or (policy.tool_set_id or "")
        inventory: list[dict] = []
        if set_id:
            try:
                _tool_set, inventory = await self.set_inventory(session, policy.organization_id, set_id)
            except HTTPException:
                inventory = []
        tool_count = len(inventory)
        if tool_count == 0 and isinstance(definition, dict):
            tool_count = sum(
                1 for node in definition.get("nodes") or [] if isinstance(node, dict) and node.get("type") == "tool"
            )
        yield {"type": "status", "code": "optimizing", "total": tool_count}
        outcome = None
        async for kind, payload in iter_optimize_definition(definition, inventory, instructions):
            if kind == "think":
                yield {"type": "think", "text": payload}
            elif kind == "result":
                outcome = payload
        if outcome is None:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Policy model returned an unreadable response")
        optimized, diff = outcome
        yield {"type": "result", "definition": optimized, "diff": diff}

    async def _set_tool_names(self, session: AsyncSession, organization_id: str, tool_set_id: str) -> list[str]:
        from app.models import Tool, ToolSet, ToolSetItem

        result = await session.execute(
            select(ToolSet)
            .options(selectinload(ToolSet.items).selectinload(ToolSetItem.tool))
            .where(ToolSet.id == tool_set_id, ToolSet.organization_id == organization_id)
        )
        tool_set = result.scalar_one_or_none()
        if tool_set is None:
            return []
        return sorted(
            item.tool.name
            for item in tool_set.items or []
            if item.tool is not None and item.tool.status == "active"
        )

    async def validate_policy(self, session: AsyncSession, policy: Policy) -> dict:
        from app.features.policy.policies.services.policy_graph import validate_definition

        names = []
        if policy.tool_set_id:
            names = await self._set_tool_names(session, policy.organization_id, policy.tool_set_id)
        report = validate_definition(policy.definition or {"nodes": [], "edges": [], "analyses": []}, names)
        if report["ok"] and policy.status != "active":
            policy.status = "validated"
            policy.version = (policy.version or 1) + 1
            policy.updated_at = utcnow()
            await session.flush()
        elif not report["ok"] and policy.status == "active":
            policy.status = "draft"
            policy.version = (policy.version or 1) + 1
            policy.updated_at = utcnow()
            await session.flush()
        return report

    async def activate_policy(self, session: AsyncSession, policy: Policy, confirm: bool) -> Policy:
        from app.features.policy.policies.services.policy_graph import risky_allows

        report = await self.validate_policy(session, policy)
        if not report["ok"]:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Policy did not pass validation")
        risky = risky_allows(list(policy.rules))
        if risky and not confirm:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Confirm high-risk allows: {', '.join(risky)}",
            )
        policy.status = "active"
        policy.version = (policy.version or 1) + 1
        policy.updated_at = utcnow()
        await session.flush()
        return await self.get_policy(session, policy.id, policy.organization_id)

    async def simulate_policy(
        self,
        policy: Policy,
        tool: str,
        arguments: dict,
        agent_id: str | None,
    ) -> dict:
        from app.features.policy.policies.services.policy_engine import EvaluationResult, PolicyDecision, evaluate_graph_rules, evaluate_rules

        tool_rules = [rule for rule in policy.rules if rule.tool == tool]
        definition = policy.definition if isinstance(policy.definition, dict) else None
        if definition and definition.get("nodes"):
            if not tool_rules:
                result = EvaluationResult(decision=PolicyDecision.DENY, reason="No policy rule")
            else:
                result = evaluate_graph_rules(tool_rules, arguments, agent_id)
        elif tool_rules:
            result = evaluate_rules(tool_rules, arguments)
        else:
            result = EvaluationResult(decision=PolicyDecision.DENY, reason="No policy rule")
        return {
            "decision": result.decision.value,
            "reason": result.reason,
            "ruleId": result.matched_rule_id,
        }

    async def _replace_compiled(self, session: AsyncSession, policy_id: str, definition: dict) -> None:
        compiled, _errors = compile_definition(definition)
        await session.execute(delete(PolicyRule).where(PolicyRule.policy_id == policy_id))
        await session.flush()
        for index, rule in enumerate(compiled):
            try:
                decision = normalize_decision(rule["decision"]).value
            except ValueError as exc:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
            expression = rule.get("expression") if isinstance(rule.get("expression"), dict) else {"op": "true"}
            flat = _flat_conditions(expression)
            structured = flat or []
            condition = compile_condition(structured) if flat is not None else "true"
            session.add(
                PolicyRule(
                    id=new_id("rule"),
                    policy_id=policy_id,
                    tool=rule["tool"],
                    condition=condition,
                    conditions=structured or None,
                    expression=expression,
                    attributes=rule.get("attributes") or None,
                    decision=decision,
                    position=index,
                )
            )


policy_service = PolicyService()
