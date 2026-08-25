"""集中管理環境變數設定。"""
from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


class Settings:
    # LINE
    line_channel_access_token: str = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
    line_channel_secret: str = os.getenv("LINE_CHANNEL_SECRET", "")

    # Gemini
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

    # Fugle
    fugle_api_key: str = os.getenv("FUGLE_API_KEY", "")
    stock_watchlist: list[str] = [
        s.strip() for s in os.getenv("STOCK_WATCHLIST", "2330,2454,2317").split(",") if s.strip()
    ]

    # Database：本地沒設定就用 SQLite；雲端填 PostgreSQL 連線字串
    database_url: str = os.getenv("DATABASE_URL") or "sqlite:///./data/app.db"

    # 每日推播對象
    daily_push_user_id: str = os.getenv("DAILY_PUSH_USER_ID", "")

    # 給排程端點的簡易保護金鑰（可選）
    task_secret: str = os.getenv("TASK_SECRET", "")

    # Google Calendar OAuth
    google_client_id: str = os.getenv("GOOGLE_CLIENT_ID", "")
    google_client_secret: str = os.getenv("GOOGLE_CLIENT_SECRET", "")
    google_calendar_id: str = os.getenv("GOOGLE_CALENDAR_ID", "primary")
    # 對外網址（cloudflared / Zeabur），授權完成後導回用
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "http://localhost:8080")

    @property
    def google_redirect_uri(self) -> str:
        custom = os.getenv("GOOGLE_REDIRECT_URI", "").strip()
        if custom:
            return custom
        return self.public_base_url.rstrip("/") + "/oauth/google/callback"

    @property
    def timezone(self) -> str:
        return os.getenv("TZ", "Asia/Taipei")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
