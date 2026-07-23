import os
from pathlib import Path
from dotenv import load_dotenv

# Find project root .env
ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(dotenv_path=ENV_PATH)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _resolve_path(env_value: str, default_relative: str) -> str:
    """Join relative paths to project root; keep absolute paths as-is."""
    raw = env_value or default_relative
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return str(path.resolve())


# Retrieve origins list
origins_raw = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173")
origins_list = [origin.strip() for origin in origins_raw.split(",") if origin.strip()]

config = {
    # AI Keys and Endpoints
    "openai_api_key": os.getenv("OPENAI_API_KEY", ""),
    "openai_base_url": os.getenv("OPENAI_BASE_URL", ""),
    "openai_model_name": os.getenv("OPENAI_MODEL_NAME", "gpt-4o-mini"),
    "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),

    # Security
    "backend_api_key": os.getenv("BACKEND_API_KEY", ""),
    "allowed_origins": origins_list,

    # Scan cadence
    "scan_cron_schedule": os.getenv("SCAN_CRON_SCHEDULE", "0 6 * * *"),

    # SQLite config
    "db_path": _resolve_path(os.getenv("DB_PATH", ""), "./data/monitoring.db"),

    # Reports output
    "reports_dir": _resolve_path(os.getenv("REPORTS_DIR", ""), "./reports"),

    # Proxy settings
    "proxy_url": os.getenv("PROXY_URL", ""),

    # Scraper settings (Node defaults: 2000–5000ms delay, 3 retries, 30s timeout)
    "scrape_delay_min": int(os.getenv("SCRAPE_DELAY_MIN", "2000")),
    "scrape_delay_max": int(os.getenv("SCRAPE_DELAY_MAX", "5000")),
    "scrape_max_retries": int(os.getenv("SCRAPE_MAX_RETRIES", "3")),
    "scrape_timeout": int(os.getenv("SCRAPE_TIMEOUT", "30000")),
}


def validate_env():
    # Require at least one API key to perform Messaging Drift summaries
    if not config["openai_api_key"] and not config["anthropic_api_key"]:
        raise ValueError(
            "Missing required API credentials. Either OPENAI_API_KEY or ANTHROPIC_API_KEY must be set in your .env file."
        )

    # Require security key for API route protection and ensure it is secure
    api_key = config["backend_api_key"].strip() if config["backend_api_key"] else ""
    if not api_key:
        raise ValueError("Missing BACKEND_API_KEY. Configure a secret key in your .env file to secure your API endpoints.")
    if api_key == "competitor_monitor_secret_api_key_12345":
        raise ValueError(
            "Insecure BACKEND_API_KEY. The default guessable API key is not allowed. "
            "Please generate a strong random key in your .env file."
        )
