from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "protected_namespaces": ()}

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

    # Google Calendar — credenciais OAuth2 (obrigatório para o fluxo /auth/google)
    google_client_id: str = ""
    google_client_secret: str = ""
    # URL de callback cadastrada no Google Cloud Console
    google_redirect_uri: str = "https://web-production-62584.up.railway.app/auth/google/callback"
    # Deprecated: refresh_token direto por env var (substituído pelo fluxo OAuth no banco)
    google_refresh_token: str = ""

    # Gateway WhatsApp Web
    wa_bridge_shared_secret: str = ""
    wa_gateway_url: str = ""

    # Whapi: chat_id permitido para mensagens from_me de grupos (ex: "5521999999999-1234567890@g.us")
    # Se vazio, mensagens from_me de grupos são rejeitadas pelo webhook Whapi.
    alfred_whapi_chat_id: str = ""

    # Modelos Claude
    model_fast: str = "claude-haiku-4-5-20251001"
    model_smart: str = "claude-sonnet-4-6"

    @property
    def is_gcal_configured(self) -> bool:
        """True se as credenciais OAuth2 do Google estão presentes."""
        return bool(self.google_client_id and self.google_client_secret)


settings = Settings()
