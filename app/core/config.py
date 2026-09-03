from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://saturn:saturn@localhost:5432/saturn"
    redis_url: str = "redis://localhost:6379/0"
    default_org_id: str = "org_saturn"
    default_actor_id: str = "user_operator"
    skip_db_bootstrap: bool = False
    session_ttl_days: int = 7
    seed_user_email: str = "rick@saturn.ai"
    seed_user_name: str = "Rick"
    seed_user_password: str = "saturn"
    x402_network: str = "base-sepolia"
    x402_asset_address: str = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
    x402_pay_to: str = "0x00000000000000000000000000000000000C0DE"
    in_process_runtime: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
