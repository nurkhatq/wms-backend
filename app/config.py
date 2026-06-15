from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    secret_key: str = "dev-secret-change-in-production"
    access_token_expire_minutes: int = 480
    refresh_token_expire_days: int = 30

    database_url: str = "postgresql+asyncpg://wms_user:wms_str0ng_2026@127.0.0.1:5432/wms_db"
    redis_url: str = "redis://:wms_redis_2026@127.0.0.1:6379/0"

    kaspi_api_base_url: str = "https://kaspi.kz/shop/api/v2"
    kaspi_api_token: str = ""
    kaspi_poll_interval_seconds: int = 300
    kaspi_lookback_days: int = 14

    moysklad_api_url: str = "https://api.moysklad.ru/api/remap/1.2"
    moysklad_token: str = ""
    moysklad_organization_id: str = ""

    log_level: str = "INFO"


settings = Settings()
