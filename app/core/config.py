import os
from functools import lru_cache

from pydantic import field_validator
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
    in_process_runtime: bool = False
    policy_default_deny: bool = True
    saturn_embed_worker: bool = True
    saturn_secret_project: str = ""
    resend_api_key: str = ""
    resend_from: str = ""
    policy_llm_base_url: str = ""
    policy_llm_api_key: str = ""
    policy_llm_model: str = ""
    # Connection pool, per process. Instances x (pool_size + max_overflow) must stay under the
    # database's connection limit: 10 instances at the defaults can open 150.
    db_pool_size: int = 5
    db_max_overflow: int = 10
    # Seconds a request waits for a free connection before it fails. Short, so a full pool
    # turns into quick 503s instead of every request hanging for half a minute.
    db_pool_timeout: float = 10.0
    # Seconds before a pooled connection is replaced, below the usual idle cut-off of proxies.
    db_pool_recycle: int = 1800
    # Seconds one statement may run. 0 turns the limit off.
    db_command_timeout: float = 60.0
    # ISO date (YYYY-MM-DD). After this day, a legacy password can no longer sign in.
    legacy_password_until: str = ""

    @field_validator("cors_origins")
    @classmethod
    def _https_origins_in_production(cls, value: str) -> str:
        """On Cloud Run only https origins are trusted.

        This list drives credentialed CORS, the cookie CSRF check and the passkey relying party. A
        localhost default left in production would trust any page a user runs on their own machine.
        """
        if not os.environ.get("K_SERVICE", "").strip():
            return value
        kept = [item.strip() for item in (value or "").split(",") if item.strip().lower().startswith("https://")]
        return ",".join(kept)


@lru_cache
def get_settings() -> Settings:
    return Settings()
