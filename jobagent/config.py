import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str        # empty string = no-LLM mode
    telegram_bot_token: str
    telegram_chat_id: int
    gmail_address: str
    gmail_app_password: str
    gmail_alerts_label: str
    notify_email: str
    tracker_sheet_id: str
    google_service_account_file: str
    match_threshold: int
    daily_card_cap: int
    max_apps_per_company_30d: int
    fetch_times: str              # comma-separated HH:MM, e.g. "08:00,20:00"
    digest_time: str              # HH:MM, e.g. "19:00"
    dry_run: bool = False         # skip writes; cap cards at 5


def load_settings() -> Settings:
    load_dotenv()
    return Settings(
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        telegram_chat_id=int(os.environ["TELEGRAM_CHAT_ID"]),
        gmail_address=os.environ["GMAIL_ADDRESS"],
        gmail_app_password=os.environ["GMAIL_APP_PASSWORD"],
        gmail_alerts_label=os.getenv("GMAIL_ALERTS_LABEL", "job-alerts"),
        notify_email=os.environ["NOTIFY_EMAIL"],
        tracker_sheet_id=os.environ["TRACKER_SHEET_ID"],
        google_service_account_file=os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"],
        match_threshold=int(os.getenv("MATCH_THRESHOLD", "60")),
        daily_card_cap=int(os.getenv("DAILY_CARD_CAP", "10")),
        max_apps_per_company_30d=int(os.getenv("MAX_APPS_PER_COMPANY_30D", "2")),
        fetch_times=os.getenv("FETCH_TIMES", "08:00,20:00"),
        digest_time=os.getenv("DIGEST_TIME", "19:00"),
        dry_run=os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes"),
    )
