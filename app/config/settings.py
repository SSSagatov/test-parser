"""Environment-driven settings; operational defaults are documented in .env.example."""

from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://procurement:procurement@localhost:5432/procurement"
    )
    portal_url: str = "https://zakup.gov.kz"
    official_hosts: list[str] = [
        "zakup.gov.kz",
        "goszakup.gov.kz",
        "old.goszakup.gov.kz",
        "ows.goszakup.gov.kz",
        "v3bl.goszakup.gov.kz",
    ]
    public_api_path: str = "/api/core/api/public/"
    public_system_name: str = "ЭГЗ"
    public_result_window: int = Field(default=10000, ge=1)
    api_announcement_cache_size: int = Field(default=64, ge=1)
    legacy_portal_url: str = "https://old.goszakup.gov.kz"
    search_url: str | None = None
    source_mode: str = "auto"
    ows_url: str = "https://ows.goszakup.gov.kz/v3/graphql"
    ows_token: SecretStr | None = None
    request_timeout: float = Field(default=45, gt=0)
    concurrency: int = Field(default=4, ge=1, le=64)
    download_concurrency: int = Field(default=3, ge=1, le=32)
    requests_per_second: float = Field(default=2, gt=0)
    retry_attempts: int = Field(default=4, ge=1, le=12)
    retry_max_wait: float = Field(default=60, gt=0)
    page_size: int = Field(default=100, ge=1, le=200)
    database_batch_size: int = Field(default=100, ge=1, le=1000)
    database_pool_size: int = Field(default=5, ge=1)
    browser: str = "chromium"
    browser_headless: bool = True
    browser_wait_ms: int = Field(default=2000, ge=0)
    proxy: SecretStr | None = None
    user_agent: str = "KazakhstanProcurementParser/0.1 (+official-data research)"
    respect_robots: bool = True
    download_folder: Path = Path("data/downloads")
    export_folder: Path = Path("data/exports")
    log_folder: Path = Path("logs")
    max_download_bytes: int = Field(default=104857600, gt=0)
    max_response_bytes: int = Field(default=33554432, gt=0)
    max_archive_members: int = Field(default=500, gt=0)
    max_archive_depth: int = Field(default=3, ge=0)
    max_extracted_bytes: int = Field(default=209715200, gt=0)
    ocr_languages: str = "rus+kaz+eng"
    ocr_dpi: int = Field(default=200, ge=72, le=600)
    ocr_timeout: int = Field(default=120, gt=0)
    libreoffice_binary: str = "libreoffice"
    tesseract_binary: str = "tesseract"
    timezone: str = "Asia/Almaty"
    active_statuses: list[str] = [
        "Опубликован",
        "Прием заявок",
        "Опубликовано (прием ценовых предложений)",
        "Опубликовано (прием заявок)",
        "Опубликован (прием заявок)",
        "Қабылдау",
        "active",
    ]
    inactive_status_fragments: list[str] = [
        "отмен",
        "заверш",
        "итог",
        "состоял",
        "cancel",
        "complet",
        "аяқтал",
        "жойыл",
    ]
    pavlodar_pattern: str = r"(?<!\w)г\s*\.\s*Павлодар(?![\w-])"
    strict_deadline_days: int = Field(default=3, ge=0)
    risk_high_terms: list[str] = [
        "строительств",
        "реконструкц",
        "капитальный ремонт",
        "сложное оборудование",
        "большой объем работ",
    ]
    risk_medium_terms: list[str] = [
        "монтаж",
        "установк",
        "настройк",
        "сертификат",
        "конфигурац",
        "специальная доставк",
    ]
    risk_standard_terms: list[str] = [
        "бумаг",
        "канцеляр",
        "картридж",
        "мебель",
        "продукт",
        "стандарт",
    ]
    summary_characters: int = Field(default=1500, ge=100)
    max_pdf_page_pixels: int = Field(default=40000000, gt=0)
    ocr_min_text_characters: int = Field(default=32, ge=0)
    ocr_image_coverage: float = Field(default=0.35, gt=0, le=1)
    max_spreadsheet_cells: int = Field(default=5000000, gt=0)

    @field_validator("browser")
    @classmethod
    def browser_supported(cls, value: str) -> str:
        if value not in {"chromium", "firefox", "webkit"}:
            raise ValueError("browser must be chromium, firefox, or webkit")
        return value

    @field_validator("source_mode")
    @classmethod
    def source_supported(cls, value: str) -> str:
        if value not in {"auto", "public", "ows", "browser"}:
            raise ValueError("source_mode must be auto, public, ows, or browser")
        return value

    @field_validator("timezone")
    @classmethod
    def timezone_exists(cls, value: str) -> str:
        ZoneInfo(value)
        return value
