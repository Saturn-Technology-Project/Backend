from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_id, utcnow
from app.models import User
from app.models.conversation import ConversationMessage
from app.services.mapping_service import mapping_service

STATUSES = {"queued", "running", "waiting_approval", "completed", "failed", "cancelled"}


class ConversationService:
    async def list_messages(
        self, session: AsyncSession, user: User, agent_id: str
    ) -> list[ConversationMessage]:
        await mapping_service.require_org_agent(session, user, agent_id)
        result = await session.execute(
            select(ConversationMessage)
            .where(
                ConversationMessage.agent_id == agent_id,
                ConversationMessage.user_id == user.id,
                ConversationMessage.organization_id == user.organization_id,
            )
            .order_by(ConversationMessage.created_at.asc())
        )
        return list(result.scalars().all())

    async def create_message(
        self, session: AsyncSession, user: User, agent_id: str, text: str
    ) -> ConversationMessage:
        agent = await mapping_service.require_org_agent(session, user, agent_id)
        prompt = text.strip()
        if not prompt:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Message is empty")
        message = ConversationMessage(
            id=new_id("msg"),
            organization_id=agent.organization_id,
            agent_id=agent.id,
            user_id=user.id,
            input=prompt,
            output="",
            status="queued",
            actions=[],
            error=None,
        )
        session.add(message)
        await session.flush()
        return message

    async def upsert_message(
        self,
        session: AsyncSession,
        user: User,
        agent_id: str,
        message_id: str,
        *,
        text: str,
        output: str,
        run_status: str,
        actions: list,
        error: str | None,
    ) -> ConversationMessage:
        agent = await mapping_service.require_org_agent(session, user, agent_id)
        prompt = text.strip()
        if not prompt:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Message is empty")
        if run_status not in STATUSES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown message status")
        current = await session.get(ConversationMessage, message_id)
        if current is None:
            current = ConversationMessage(
                id=message_id,
                organization_id=agent.organization_id,
                agent_id=agent.id,
                user_id=user.id,
                input=prompt,
                output=output,
                status=run_status,
                actions=list(actions or []),
                error=error,
            )
            session.add(current)
            await session.flush()
            return current
        if (
            current.user_id != user.id
            or current.agent_id != agent.id
            or current.organization_id != user.organization_id
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found")
        if (
            current.status == "running"
            and run_status == "running"
            and output == ""
            and prompt == current.input
        ):
            raise HTTPException(status.HTTP_409_CONFLICT, "Message is already running")
        revised = prompt != current.input
        current.input = prompt
        current.output = "" if revised and output == "" else output
        current.status = run_status
        current.actions = [] if revised and not actions else list(actions or [])
        current.error = None if revised and error is None else error
        current.updated_at = utcnow()
        if revised:
            await session.execute(
                delete(ConversationMessage).where(
                    ConversationMessage.user_id == user.id,
                    ConversationMessage.organization_id == user.organization_id,
                    ConversationMessage.agent_id == agent.id,
                    ConversationMessage.created_at > current.created_at,
                )
            )
        await session.flush()
        return current


conversation_service = ConversationService()
