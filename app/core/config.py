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
    seed_user_email: str = ""
    seed_user_name: str = "Admin"
    seed_user_password: str = ""
    cors_origins: str = "http://localhost:3000,http://localhost:3001,https://dashboard.saturn-core.com"
    x402_network: str = "base-sepolia"
    x402_asset_address: str = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
    x402_pay_to: str = ""
    in_process_runtime: bool = False
    saturn_secret_project: str = ""
    resend_api_key: str = ""
    resend_from: str = ""
    policy_llm_base_url: str = ""
    policy_llm_api_key: str = ""
    policy_llm_model: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
