from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://autoflow:autoflow@postgres:5432/autoflow"

    # Where the trading engine writes its state / heartbeat / trade log.
    engine_var_dir: Path = Path("/engine/var")
    engine_config_path: Path = Path("/engine/config/config.paper.yaml")
    engine_repo_dir: Path = Path("/engine")

    # Name of the docker-compose service for the bot container.
    trading_bot_service: str = "trading-bot"

    # Heartbeat considered stale after this many seconds.
    heartbeat_stale_seconds: int = 30

    cors_origins: str = "http://localhost:3000"


settings = Settings()
