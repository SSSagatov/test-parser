import json

from pydantic import Field

from app.models.domain import LotData, Record


class Checkpoint(Record):
    version: int = 1
    cursor: str | None = None
    pending: list[LotData] = Field(default_factory=list)
    exhausted: bool = False
    recent_page_hashes: list[str] = Field(default_factory=list)

    @classmethod
    def decode(cls, value: str | None) -> "Checkpoint":
        if value is None:
            return cls()
        checkpoint = cls.model_validate(json.loads(value))
        if checkpoint.version != 1:
            raise ValueError("Unsupported crawl checkpoint version")
        return checkpoint

    def encode(self) -> str:
        return self.model_dump_json()
