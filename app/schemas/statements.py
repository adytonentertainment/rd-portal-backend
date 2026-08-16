"""Response schemas for the Phase-1 statement admin API (PRD §9 subset).

Money fields are Decimal end-to-end; pydantic v2 serializes them as JSON
strings, which is deliberate — the frontend must never do float math on them.
Enum-typed columns are exposed as their .value strings (e.g. "approved",
"blocker"); the routers convert explicitly.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class FindingCounts(BaseModel):
    """Open findings by severity (waived/resolved are out of the gate)."""

    blocker: int = 0
    warning: int = 0
    info: int = 0


class ValidationRunSummary(BaseModel):
    id: int
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    rules_version: Optional[str] = None
    blockers: int
    warnings: int
    infos: int


class BatchSummary(BaseModel):
    id: int
    label: str
    period_code: str
    catalog: str
    cadence: Optional[str] = None
    status: str
    uploaded_at: Optional[datetime] = None
    statement_count: int
    stats: Optional[Dict[str, Any]] = None


class BatchDetail(BatchSummary):
    finding_counts: FindingCounts
    last_run: Optional[ValidationRunSummary] = None


class FindingOut(BaseModel):
    id: int
    run_id: int
    rule_id: str
    severity: str
    scope: str
    scope_ref: Optional[str] = None
    message: str
    details: Optional[Dict[str, Any]] = None
    status: str
    waived_by: Optional[int] = None
    waived_reason: Optional[str] = None
    waived_at: Optional[datetime] = None
    acknowledged_by: Optional[int] = None
    acknowledged_at: Optional[datetime] = None


class StatementKeyFigures(BaseModel):
    """One row of the batch drill-down table — the admin's scan view."""

    id: int
    account_code: str
    writer_name: Optional[str] = None
    period_code: str
    version: int
    parse_status: str
    calculated: Optional[Decimal] = None
    payable: Optional[Decimal] = None
    detail_sum: Optional[Decimal] = None
    embedded_total: Optional[Decimal] = None
    line_count: Optional[int] = None
    zero_pay_reason: Optional[str] = None


class StatementDetail(BaseModel):
    id: int
    batch_id: int
    account_code: str
    writer_name: Optional[str] = None
    period_code: str
    version: int
    pdf_path: Optional[str] = None
    xlsx_path: Optional[str] = None
    parse_status: str
    parse_error: Optional[str] = None
    zero_pay_reason: Optional[str] = None

    # PDF account summary fields (None == absent in old layout, never 0.0)
    calculated: Optional[Decimal] = None
    recouped: Optional[Decimal] = None
    reserve_taken: Optional[Decimal] = None
    reserve_released: Optional[Decimal] = None
    carried_forward_in: Optional[Decimal] = None
    carried_forward_out: Optional[Decimal] = None
    payable_prev: Optional[Decimal] = None
    payable_this: Optional[Decimal] = None
    settlement_paid: Optional[Decimal] = None
    before_tax: Optional[Decimal] = None
    payable: Optional[Decimal] = None
    cheque_amount: Optional[Decimal] = None

    # Computed from XLSX detail
    detail_sum: Optional[Decimal] = None
    embedded_total: Optional[Decimal] = None
    line_count: Optional[int] = None

    # Open findings scoped to this statement
    finding_counts: FindingCounts


class StatementLineOut(BaseModel):
    id: int
    row_no: int
    song_code: Optional[str] = None
    asset_id: Optional[str] = None
    custom_id: Optional[str] = None
    song_title: Optional[str] = None
    country: Optional[str] = None
    channel: Optional[str] = None
    income_source: Optional[str] = None
    income_type: Optional[str] = None
    price: Optional[Decimal] = None
    commission_pct: Optional[Decimal] = None
    rbp: Optional[Decimal] = None
    rate_applied: Optional[Decimal] = None
    writer_split_pct: Optional[Decimal] = None
    ben_split_pct: Optional[Decimal] = None
    units: Optional[Decimal] = None
    earnings: Optional[Decimal] = None


class StatementLinesPage(BaseModel):
    statement_id: int
    page: int
    page_size: int
    total: int
    items: List[StatementLineOut]


class WaiveRequest(BaseModel):
    reason: str = Field(min_length=1)
