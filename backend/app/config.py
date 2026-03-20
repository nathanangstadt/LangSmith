from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Agent Playground"
    api_prefix: str = "/api"
    database_url: str = Field(
        default="sqlite:///./agent_playground.db",
        alias="DATABASE_URL",
    )
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    app_encryption_key: str = Field(default="", alias="APP_ENCRYPTION_KEY")
    langsmith_tracing: bool = Field(default=False, alias="LANGSMITH_TRACING")
    langsmith_api_key: str = Field(default="", alias="LANGSMITH_API_KEY")
    langsmith_project: str = Field(default="agent-playground", alias="LANGSMITH_PROJECT")
    otel_exporter_otlp_endpoint: Optional[str] = Field(
        default=None,
        alias="OTEL_EXPORTER_OTLP_ENDPOINT",
    )
    otel_exporter_otlp_headers: Optional[str] = Field(
        default=None,
        alias="OTEL_EXPORTER_OTLP_HEADERS",
    )
    otel_service_name: str = Field(default="agent-playground", alias="OTEL_SERVICE_NAME")
    jaeger_ui_url: str = Field(default="", alias="JAEGER_UI_URL")
    frontend_origin: str = Field(default="http://localhost:5173", alias="FRONTEND_ORIGIN")
    log_llm_traffic: bool = Field(default=False, alias="LOG_LLM_TRAFFIC")

    @property
    def langsmith_enabled(self) -> bool:
        return self.langsmith_tracing and bool(self.langsmith_api_key)

    @property
    def langsmith_project_url(self) -> str:
        if not self.langsmith_enabled:
            return ""
        return "https://smith.langchain.com/"


@lru_cache
def get_settings() -> Settings:
    return Settings()
