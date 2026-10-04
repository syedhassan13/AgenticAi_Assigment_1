from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore')

    # --- server ---
    host: str = '127.0.0.1'
    port: int = 8000

    # --- primary model (used by default in deployment) ---
    model_provider: str = 'gemini'                # gemini | openai
    model_name: str = 'gemini-3.8-flash'

    # --- secondary model (for comparison harness, also free Gemini) ---
    secondary_provider: str = 'gemini'
    secondary_model: str = 'gemini-3.5-flash-lite'

    # --- API keys: ONLY from env vars, never hardcoded ---
    gemini_api_key: str = ''
    openai_api_key: str = ''  # optional, only if you have a paid key

    # --- execution limits (system ceilings; arena_config can only lower) ---
    max_steps: int = Field(default=6, ge=1, le=6)
    max_tool_retries: int = Field(default=2, ge=0, le=2)
    max_repair_retries: int = Field(default=2, ge=0, le=3)
    max_output_tokens: int = Field(default=512, ge=1)
    run_timeout_seconds: float = Field(default=40, gt=0, le=40)

    # --- context / memory limits ---
    history_max_messages: int = Field(default=12, ge=2, le=20)


settings = Settings()

