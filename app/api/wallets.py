from fastapi import APIRouter, HTTPException, status

from app.api.deps import AgentsRead, CurrentUser, SessionDep
from app.schemas import WalletRead
from app.services.mapping_service import mapping_service
from app.services.wallet_service import wallet_service

router = APIRouter(tags=["wallets"])


@router.get("/wallets", response_model=list[WalletRead])
async def list_wallets(session: SessionDep, user: CurrentUser, _access: AgentsRead):
    return await wallet_service.list_wallets(session, user)


@router.get("/wallets/{wallet_id}", response_model=WalletRead)
async def get_wallet(wallet_id: str, session: SessionDep, user: CurrentUser, _access: AgentsRead):
    wallet = await wallet_service.get_wallet(session, wallet_id)
    if wallet.organization_id != user.organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Wallet not found")
    return wallet


@router.get("/agents/{agent_id}/wallet", response_model=WalletRead)
async def get_agent_wallet(agent_id: str, session: SessionDep, user: CurrentUser, _access: AgentsRead):
    await mapping_service.require_org_agent(session, user, agent_id)
    return await wallet_service.get_wallet_for_agent(session, agent_id)
