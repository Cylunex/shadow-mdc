from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHADOW_MDC_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    database_url: str = "sqlite:///data/shadow-mdc.db"
    javdb_base_url: str = "https://javdb.com"
    javbus_base_url: str = "https://www.javbus.com"
    jav321_base_url: str = "https://www.jav321.com"
    r18dev_base_url: str = "https://r18.dev"
    fanza_base_url: str = "https://www.dmm.co.jp"
    javlibrary_base_url: str = "https://www.javlibrary.com"
    mgstage_base_url: str = "https://www.mgstage.com"
    fc2club_base_url: str = "https://fc2club.top"
    fc2hub_base_url: str = "https://javten.com"
    fc2contents_base_url: str = "https://adult.contents.fc2.com"
    paipancon_base_url: str = "https://paipancon.com"
    airav_base_url: str = "https://cn.airav.wiki"
    avsox_base_url: str = "https://avsox.click"
    freejavbt_base_url: str = "https://freejavbt.com"
    theporndb_graphql_url: str = "https://theporndb.net/graphql"
    theporndb_token: str | None = None
    request_timeout_seconds: float = Field(default=20, ge=1, le=120)
    request_retries: int = Field(default=1, ge=0, le=5)
    identify_concurrency: int = Field(default=4, ge=1, le=24)
    provider_concurrency: int = Field(default=16, ge=1, le=64)
    translation_concurrency: int = Field(default=4, ge=1, le=16)
    proxy_url: str | None = None
    translation_enabled: bool = True
    translation_backend: str = "google"
    translation_endpoint: str = "https://translate.google.com/translate_a/single"
    translation_api_key: str | None = None
    translation_deepl_api_url: str | None = "https://api-free.deepl.com/v2/translate"
    translation_deepl_api_key: str | None = None
    translation_deeplx_endpoint: str | None = None
    translation_custom_endpoint: str | None = None
    translation_target_language: str = "zh-CN"
    translation_plot: bool = True
    artwork_max_bytes: int = Field(default=25 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)
    auto_seed_non_jav_works: bool = True
    # Redis for API response cache. Empty / unset → in-process TTL dict.
    # Production NAS: redis://192.168.0.21:6379/0
    # Local/dev fallback: redis://127.0.0.1:6379/0 when reachable.
    redis_url: str | None = "redis://127.0.0.1:6379/0"
    user_agent: str = "ShadowMDC/0.1 (+https://github.com/Cylunex/shadow-mdc)"
    # 115 Open Platform — temporary community id; prefer your own app at open.115.com
    pan_client_id: str = "100197303"
    pan_client_secret: str | None = None
    # GFriends actress portraits (https://github.com/gfriends/gfriends)
    gfriends_filetree_url: str = "https://cdn.jsdelivr.net/gh/gfriends/gfriends@master/Filetree.json"
    gfriends_cdn_base_url: str = "https://cdn.jsdelivr.net/gh/gfriends/gfriends@master"
    gfriends_cache_ttl_hours: int = Field(default=24 * 7, ge=1, le=24 * 30)


    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "artwork").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "actor-images").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "category-covers").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "pan").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "cache" / "gfriends").mkdir(parents=True, exist_ok=True)
