"""Usage report response schemas."""

from pydantic import BaseModel, Field


class UsageMonth(BaseModel):
    """Usage data for a single month."""

    month: str = Field(description="YYYY-MM format")
    used: int = Field(description="Units used in this month")
    quota: int = Field(description="Monthly quota limit at the time of this data")
    remaining: int = Field(description="quota - used")
    soft_threshold_crossed: bool = Field(description="Soft threshold was reached in this month")
    source: str = Field(description="live or rollup")


class UsageReport(BaseModel):
    """Full usage history for a key."""

    key_id: str = Field(description="Key identifier")
    name: str = Field(description="Key name")
    plan: str = Field(description="Plan slug")
    months: list[UsageMonth] = Field(description="Months in order (current live month first, then prior rollups)")
