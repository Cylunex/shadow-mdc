import os
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from .services.subtitlecat import SubtitleCatFetcher


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHADOW_MDC_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    database_url: str = "sqlite:///data/shadow-mdc.db"
    javdb_base_url: str = "https://javdb.com"
    # JavDB app JSON API (signed; primary for rankings, fallback for detail/magnets).
    javdb_api_enabled: bool = True
    javdb_api_hosts: str = "https://jdforrepam.com,https://javdb.com"
    javdb_api_token: str | None = None  # bearer token; only needed for TOP250
    # Machine-to-machine bearer token for /api/mcp/v1 (MCP server). Unset → MCP API disabled (503).
    mcp_token: str | None = Field(default=None, repr=False)
    javbus_base_url: str = "https://www.javbus.com"
    jav321_base_url: str = "https://www.jav321.com"
    r18dev_base_url: str = "https://r18.dev"
    r18_dump_db: Path | None = None  # offline dump SQLite; default data_dir/r18-dumps/r18_dump.db
    # Weekly NAS job (scripts/nas/r18_dump_weekly.sh): stop API → refresh dump → enrich → start.
    # Keep disabled until a manual run succeeds; cron expression is documentary for CRON_TZ crontab.
    r18_dump_refresh_enabled: bool = False
    # Forced rescrape (POST /api/works/{id}/refresh) budget per provider per local day.
    rescrape_daily_quota: int = Field(default=300, ge=0, le=100_000)
    # Seconds to wait for the r18 dump worker thread on shutdown.
    r18_dump_worker_join_seconds: float = Field(default=5.0, ge=0, le=60)
    r18_dump_refresh_cron: str = "1 10 * * 3"  # Wed 10:01 — pair with CRON_TZ=Asia/Shanghai
    r18_dump_refresh_timezone: str = "Asia/Shanghai"
    r18_dump_refresh_service: str = "shadow-mdc"
    r18_dump_refresh_health_url: str = "http://127.0.0.1:8700/api/health"
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
    # ``TPDB_TOKEN`` is the short name used in the NAS supervisor env file.
    theporndb_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SHADOW_MDC_THEPORNDB_TOKEN", "TPDB_TOKEN"),
    )
    request_timeout_seconds: float = Field(default=20, ge=1, le=120)
    request_retries: int = Field(default=1, ge=0, le=5)
    identify_concurrency: int = Field(default=4, ge=1, le=24)
    provider_concurrency: int = Field(default=16, ge=1, le=64)
    translation_concurrency: int = Field(default=4, ge=1, le=16)
    proxy_url: str | None = None
    translation_enabled: bool = True

    @field_validator("proxy_url", mode="before")
    @classmethod
    def _proxy_url_fallback(cls, value: object) -> object:
        """Prefer SHADOW_MDC_PROXY_URL; else HTTPS_PROXY / HTTP_PROXY / PROXY_URL.

        Seed/import CLI often sources secrets without the supervisor env block;
        without a proxy Google translate hangs until the post-ingest timeout.
        """
        if isinstance(value, str) and value.strip():
            return value.strip()
        for key in (
            "SHADOW_MDC_PROXY_URL",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "PROXY_URL",
            "https_proxy",
            "http_proxy",
        ):
            raw = os.environ.get(key)
            if raw and raw.strip():
                return raw.strip()
        return value
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
    # SubtitleCat zh subtitles (no key): after STRM export write <name>.zh.srt by code. Opt-in.
    subtitlecat_enabled: bool = False
    subtitlecat_base_url: str = "https://www.subtitlecat.com"
    subtitlecat_min_interval_seconds: float = Field(default=3.0, ge=0, le=60)
    subtitlecat_miss_ttl_hours: float = Field(default=72, ge=0, le=24 * 90)
    # Redis for API response cache. Empty / unset → in-process TTL dict.
    # Production NAS: redis://192.168.0.21:6379/0
    # Local/dev fallback: redis://127.0.0.1:6379/0 when reachable.
    redis_url: str | None = "redis://127.0.0.1:6379/0"
    # Tube swipefeed Node sidecar (listing/resolve/media proxy). Empty → /api/swipe/* returns 503.
    swipe_upstream_url: str | None = "http://127.0.0.1:8791"
    swipe_proxy_timeout_seconds: float = Field(default=60.0, ge=5, le=600)
    user_agent: str = "ShadowMDC/0.1 (+https://github.com/Cylunex/shadow-mdc)"
    # 115 Open Platform — temporary community id; prefer your own app at open.115.com
    pan_client_id: str = "100197303"
    pan_client_secret: str | None = None
    # GFriends actress portraits (https://github.com/gfriends/gfriends)
    gfriends_filetree_url: str = "https://cdn.jsdelivr.net/gh/gfriends/gfriends@master/Filetree.json"
    gfriends_cdn_base_url: str = "https://cdn.jsdelivr.net/gh/gfriends/gfriends@master"
    gfriends_cache_ttl_hours: int = Field(default=24 * 7, ge=1, le=24 * 30)

    # Local (gitignored) JSON of external play sites + Apple-CMS VOD providers.
    # Default: {data_dir}/playback-sites.local.json — never commit real entries.
    playback_sites_path: Path | None = None

    def subtitlecat_fetcher(self) -> "SubtitleCatFetcher":
        from .services.subtitlecat import SubtitleCatFetcher

        return SubtitleCatFetcher(
            base_url=self.subtitlecat_base_url,
            timeout_seconds=self.request_timeout_seconds,
            min_interval_seconds=self.subtitlecat_min_interval_seconds,
            miss_ttl_hours=self.subtitlecat_miss_ttl_hours,
            cache_path=self.data_dir / "subtitlecat-misses.json",
            proxy_url=self.proxy_url,
        )

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "artwork").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "actor-images").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "category-covers").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "pan").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "cache" / "gfriends").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "r18-dumps").mkdir(parents=True, exist_ok=True)

    def resolved_r18_dump_db(self) -> Path:
        if self.r18_dump_db is not None:
            return Path(self.r18_dump_db)
        return self.data_dir / "r18-dumps" / "r18_dump.db"
