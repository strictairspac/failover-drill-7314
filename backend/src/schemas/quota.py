from pydantic import BaseModel, Field


class PoolInput(BaseModel):
    node_id: int = Field(gt=0)
    capacity: int = Field(gt=0, le=1_000_000_000)


class ReservationInput(BaseModel):
    pool_id: int = Field(gt=0)
    amount: int = Field(gt=0, le=1_000_000_000)
    idem_key: str | None = Field(default=None, min_length=8, max_length=80)


class TransferInput(BaseModel):
    from_pool_id: int = Field(gt=0)
    to_pool_id: int = Field(gt=0)
    amount: int = Field(gt=0, le=1_000_000_000)
    idem_key: str | None = Field(default=None, min_length=8, max_length=80)


class SimulationInput(BaseModel):
    available: bool | None = None
    fenced: bool | None = None
