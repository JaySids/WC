from pydantic_settings import BaseSettings
from functools import lru_cache
import os


class Settings(BaseSettings):
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    openrouter_api_key: str = ""  # kept for backward compat, not used by agent
    daytona_api_key: str = ""
    supabase_url: str = ""
    supabase_key: str = ""

    # Clone defaults
    default_model: str = "claude-sonnet-4-20250514"
    clone_timeout: int = 120  # seconds
    page_load_timeout: int = 30000  # milliseconds
    viewport_width: int = 1920
    viewport_height: int = 1080
    scroll_overlap: int = 200

    class Config:
        # Look for .env in the repo root (two levels up from backend/app/)
        env_file = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
        env_file_encoding = "utf-8"
        extra = "ignore"


@lru_cache()
def get_settings():
    return Settings()
