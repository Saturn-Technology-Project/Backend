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
    cors_origins: str = "http://localhost:3000,http://localhost:3001"
    public_ws_url: str = ""
    x402_network: str = "base-sepolia"
    x402_asset_address: str = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
    x402_pay_to: str = ""
    in_process_runtime: bool = False
    saturn_secret_project: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


def public_ws_origin() -> str:
    raw = (get_settings().public_ws_url or "").strip().rstrip("/")
    if raw.endswith("/saturn-api"):
        raw = raw[: -len("/saturn-api")]
    if raw.startswith("https://"):
        return "wss://" + raw[len("https://") :]
    if raw.startswith("http://"):
        return "ws://" + raw[len("http://") :]
    return raw


def public_ws_url(path: str) -> str:
    origin = public_ws_origin()
    suffix = path if path.startswith("/") else f"/{path}"
    return f"{origin}{suffix}" if origin else suffix
