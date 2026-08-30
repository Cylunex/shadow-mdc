from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHADOW_MDC_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    database_url: str = "sqlite:///data/shadow-mdc.db"
    javdb_base_url: str = "https://javdb.com"
    javbus_base_url: str = "https://www.javbus.com"
    theporndb_graphql_url: str = "https://theporndb.net/graphql"
    theporndb_token: str | None = None
    request_timeout_seconds: float = Field(default=20, ge=1, le=120)
    request_retries: int = Field(default=1, ge=0, le=5)
    proxy_url: str | None = None
    user_agent: str = "ShadowMDC/0.1 (+https://github.com/Cylunex/shadow-mdc)"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "artwork").mkdir(parents=True, exist_ok=True)
