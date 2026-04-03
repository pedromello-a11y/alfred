from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str

    # Whapi legado — opcional quando o Alfred roda via gateway whatsapp-web.js
    whapi_token: str = ""
    whapi_api_url: str = "https://gate.whapi.cloud"
    pedro_phone: str = ""

    anthropic_api_key: str

    # Jira — opcional para rodar localmente sem sync
    jira_base_url: str = ""
    jira_email: str = ""
    jira_api_token: str = ""

    # Google Calendar — opcional para rodar localmente sem agenda
    google_refresh_token: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""

    # Gateway WhatsApp Web
    wa_bridge_shared_secret: str = ""
    wa_gateway_url: str = ""

    # Whapi: chat_id permitido para mensagens from_me de grupos (ex: "5521999999999-1234567890@g.us")
    # Se vazio, mensagens from_me de grupos são rejeitadas pelo webhook Whapi.
    alfred_whapi_chat_id: str = ""

    # Modelos Claude
    model_fast: str = "claude-haiku-4-5-20251001"
    model_smart: str = "claude-sonnet-4-6"

    class Config:
        env_file = ".env"


settings = Settings()
