from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "reader-api"
    app_env: str = "development"

    database_url: str
    mongodb_uri: str

    google_client_id: str = ""
    nextauth_secret: str = ""
    mobile_jwt_secret: str = ""

    cors_origins: str = "*"
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket_name: str = ""
    r2_public_base_url: str = ""

    deepseek_key: str = ""
    deepseek_model: str = "deepseek-chat"
    openrouter_key: str = ""
    openrouter_paused: str = "true"

    @property
    def google_client_id_list(self) -> list[str]:
        raw = (self.google_client_id or "").strip()
        if not raw:
            return []
        return [item.strip() for item in raw.split(",") if item.strip()]


    @property
    def cors_origin_list(self) -> list[str]:
        raw = (self.cors_origins or "*").strip()
        if raw == "*":
            return ["*"]
        return [item.strip() for item in raw.split(",") if item.strip()]


settings = Settings()
