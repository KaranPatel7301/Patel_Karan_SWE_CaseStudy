from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class CompanyConfig(BaseModel):
    ticker: str
    cik: str
    name: str

    @field_validator("ticker", "name")
    @classmethod
    def must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("cik")
    @classmethod
    def cik_must_be_ten_digits(cls, value: str) -> str:
        if len(value) != 10 or not value.isdigit():
            raise ValueError("cik must be a 10-digit string")
        return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://tracker:tracker@db:5432/tracker"
    sec_user_agent: str = "Karan <your-email@example.com>"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = "changeme"
    llm_model: str = "changeme"
    llm_max_tool_iterations: int = 6
    companies_path: Path = Path("companies.yaml")
    companies: list[CompanyConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def load_company_universe(self) -> "Settings":
        self.companies = read_companies(self.companies_path)
        return self


def read_companies(path: Path) -> list[CompanyConfig]:
    if not path.is_file():
        raise FileNotFoundError(f"companies file not found: {path}")

    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("companies"), list):
        raise ValueError(f"{path} must be a mapping with a 'companies' list")

    companies = [CompanyConfig.model_validate(row) for row in payload["companies"]]
    if not companies:
        raise ValueError(f"{path} companies list is empty")

    tickers = [company.ticker for company in companies]
    if len(tickers) != len(set(tickers)):
        raise ValueError(f"{path} contains a duplicate ticker")
    return companies


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
