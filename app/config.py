from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str

    # Whapi legado — opcional quando o Alfred roda via gateway whatsapp-web.js
    whapi_token: str = ""
    whapi_api_url: str = "https://gate.whapi.cloud"
    pedro_phone: str = ""

    anthropic_api_key: str
    jira_base_url: str
    jira_email: str
    jira_api_token: str
    google_refresh_token: str
    google_client_id: str
    google_client_secret: str

    # Gateway WhatsApp Web
    wa_bridge_shared_secret: str = ""
    wa_gateway_url: str = ""

    # Modelos Claude
    model_fast: str = "claude-haiku-4-5-20251001"
    model_smart: str = "claude-sonnet-4-6"

    class Config:
        env_file = ".env"


settings = Settings()
