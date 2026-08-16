"""Level 2 — statement math rules V-STMT-1..5 (PRD §7.2).

All checks read figures the parse stage persisted on the statement row —
Decimal columns compared with Decimal tolerances (±$0.02 per PRD), never
float equality. Statements missing the inputs of a rule are skipped here:
absent figures are V-FILE-3/4/5's findings, not math violations.

Sign convention for the payable identity (V-STMT-3): the parser stores
amounts exactly as printed, and the PDF prints 'Amount recouped' as a
NEGATIVE number (PRD §2.5, verified by the JN0249 fixture: calculated
38,009.34, recouped −38,009.34, payable 0). The PRD formula's '− recouped'
subtracts the deduction *magnitude*, which for the stored signed value
means ADDING it. 'Carried forward to Next period' (below-threshold money
leaving the account, stored as carried_forward_out) is subtracted — the
PRD formula's carried_forward term covers only the inbound side.

V-STMT-5 owns zero-pay classification: it SETS statement.zero_pay_reason
for payable == 0 statements (the engine's commit persists it) and emits a
blocker only for unexplainable zeros (calculated > 0, no recoupment, no
carryforward out — PRD §7.2), which stay unclassified.
"""

from decimal import Decimal
from typing import List, Optional

from app.models.statements import FindingScope, FindingSeverity, Statement, ZeroPayReason
from app.services.validation.engine import Rule, RuleContext, RuleFinding, register
from app.services.validation.file_rules import _statement_details, _statement_ref

CENT_TOLERANCE = Decimal("0.02")
# V-STMT-2: historical data shows ≤$1.02 rounding; >$0.02 warns, >$5 blocks
ROUNDING_BLOCKER_THRESHOLD = Decimal("5.00")
ZERO = Decimal("0")


def _money(value: Optional[Decimal]) -> Decimal:
    """None treated as 0 — old-layout PDFs simply lack the line (PRD §2.5)."""
    return ZERO if value is None else value


def check_detail_sum_matches_embedded_total(ctx: RuleContext) -> List[RuleFinding]:
    """V-STMT-1: the XLSX line-item sum equals its own grand-total row."""
    findings = []
    for statement in ctx.statements:
        if statement.detail_sum is None or statement.embedded_total is None:
            continue
        diff = abs(statement.detail_sum - statement.embedded_total)
        if diff > CENT_TOLERANCE:
            findings.append(
                RuleFinding(
                    scope_ref=_statement_ref(statement),
                    message=(
                        f"{statement.account.account_code} {statement.period_code}: "
                        f"detail sum {statement.detail_sum} != embedded grand total "
                        f"{statement.embedded_total} (diff {diff})"
                    ),
                    details=dict(
                        _statement_details(statement),
                        detail_sum=str(statement.detail_sum),
                        embedded_total=str(statement.embedded_total),
                        diff=str(diff),
                    ),
                )
            )
    return findings


def check_detail_sum_matches_calculated(ctx: RuleContext) -> List[RuleFinding]:
    """V-STMT-2: the XLSX earnings agree with the PDF 'Royalties calculated'.
    Severity scales with magnitude: ≤$0.02 pass, ≤$5.00 warning, else blocker."""
    findings = []
    for statement in ctx.statements:
        if statement.detail_sum is None or statement.calculated is None:
            continue
        diff = abs(statement.detail_sum - statement.calculated)
        if diff <= CENT_TOLERANCE:
            continue
        findings.append(
            RuleFinding(
                scope_ref=_statement_ref(statement),
                message=(
                    f"{statement.account.account_code} {statement.period_code}: "
                    f"detail sum {statement.detail_sum} != PDF calculated "
                    f"{statement.calculated} (diff {diff})"
                ),
                details=dict(
                    _statement_details(statement),
                    detail_sum=str(statement.detail_sum),
                    calculated=str(statement.calculated),
                    diff=str(diff),
                ),
                severity=(
                    FindingSeverity.BLOCKER
                    if diff > ROUNDING_BLOCKER_THRESHOLD
                    else None  # registry default: warning
                ),
            )
        )
    return findings


def _expected_payable(statement: Statement) -> Decimal:
    return (
        statement.calculated
        + _money(statement.recouped)  # printed negative; see module docstring
        - _money(statement.reserve_taken)
        + _money(statement.reserve_released)
        + _money(statement.carried_forward_in)
        + _money(statement.payable_prev)
        - _money(statement.settlement_paid)
        - _money(statement.carried_forward_out)
    )


def check_payable_identity(ctx: RuleContext) -> List[RuleFinding]:
    """V-STMT-3: the account-summary waterfall reconciles to the PRE-tax
    subtotal.

    The waterfall (calculated ± recoup/reserves/carryforwards/settlement)
    reconciles to `before_tax`, NOT `payable`. `payable` is `before_tax` minus
    the client's commission / withholding — a per-client deduction (10 / 15 /
    30 %…) that is a legitimate business term, not a math error. Reconcile to
    before_tax when present; old-layout PDFs that lack it fall back to payable
    (in those, no commission line is printed, so the two are equal)."""
    findings = []
    for statement in ctx.statements:
        if statement.calculated is None:
            continue
        expected = _expected_payable(statement)
        # Accept a match against EITHER the pre-tax subtotal (`before_tax`) or
        # `payable`: newer layouts print a commission/withholding line so the
        # waterfall lands on before_tax and payable is that minus the fee;
        # older layouts print no fee, so it lands on payable directly. Only a
        # statement that reconciles to NEITHER is a real math violation.
        targets = [t for t in (statement.before_tax, statement.payable) if t is not None]
        if not targets:
            continue
        if any(abs(t - expected) <= CENT_TOLERANCE for t in targets):
            continue
        target = min(targets, key=lambda t: abs(t - expected))
        diff = abs(target - expected)
        findings.append(
            RuleFinding(
                scope_ref=_statement_ref(statement),
                message=(
                    f"{statement.account.account_code} {statement.period_code}: "
                    f"payable subtotal {target} != identity result {expected} "
                    f"(diff {diff})"
                ),
                details=dict(
                    _statement_details(statement),
                    before_tax=str(statement.before_tax),
                    payable=str(statement.payable),
                    expected=str(expected),
                    diff=str(diff),
                ),
            )
        )
    return findings


def check_payable_not_negative(ctx: RuleContext) -> List[RuleFinding]:
    """V-STMT-4: payable >= 0 — zero negative payables exist in 2,612 real
    statements; a negative one is an upstream error."""
    findings = []
    for statement in ctx.statements:
        if statement.payable is None or statement.payable >= ZERO:
            continue
        findings.append(
            RuleFinding(
                scope_ref=_statement_ref(statement),
                message=(
                    f"{statement.account.account_code} {statement.period_code}: "
                    f"negative payable {statement.payable}"
                ),
                details=dict(_statement_details(statement), payable=str(statement.payable)),
            )
        )
    return findings


def check_zero_pay_classified(ctx: RuleContext) -> List[RuleFinding]:
    """V-STMT-5 (classification half): every payable == 0 gets an explained
    zero_pay_reason — recouped (recouped < 0), threshold_carryover
    (calculated > 0 with a carryforward out), zero_earnings otherwise. A zero
    with calculated > 0 but no recoupment and no carryforward out is
    unexplainable: blocker, reason left unset."""
    findings = []
    for statement in ctx.statements:
        if statement.payable is None:
            continue
        if statement.payable != ZERO:
            # data changed since a previous classification (e.g. corrected
            # re-parse) — a non-zero payable can't keep a zero-pay story
            statement.zero_pay_reason = None
            continue
        if _money(statement.recouped) < ZERO:
            statement.zero_pay_reason = ZeroPayReason.RECOUPED
        elif _money(statement.calculated) > ZERO:
            if _money(statement.carried_forward_out) != ZERO:
                statement.zero_pay_reason = ZeroPayReason.THRESHOLD_CARRYOVER
            else:
                statement.zero_pay_reason = None
                findings.append(
                    RuleFinding(
                        scope_ref=_statement_ref(statement),
                        message=(
                            f"{statement.account.account_code} "
                            f"{statement.period_code}: payable is 0 but "
                            f"{statement.calculated} was calculated with no "
                            "recoupment and no carryforward to next period"
                        ),
                        details=dict(
                            _statement_details(statement),
                            calculated=str(statement.calculated),
                            recouped=str(_money(statement.recouped)),
                            carried_forward_out=str(_money(statement.carried_forward_out)),
                        ),
                    )
                )
        else:
            statement.zero_pay_reason = ZeroPayReason.ZERO_EARNINGS
    return findings


register(
    Rule(
        id="V-STMT-1",
        level=2,
        default_severity=FindingSeverity.BLOCKER,
        scope=FindingScope.STATEMENT,
        description="detail_sum == embedded_total (±$0.02)",
        check=check_detail_sum_matches_embedded_total,
    )
)
register(
    Rule(
        id="V-STMT-2",
        level=2,
        default_severity=FindingSeverity.WARNING,
        scope=FindingScope.STATEMENT,
        description="detail_sum == pdf.calculated (±$0.02; >$5.00 escalates to blocker)",
        check=check_detail_sum_matches_calculated,
    )
)
register(
    Rule(
        id="V-STMT-3",
        level=2,
        default_severity=FindingSeverity.BLOCKER,
        scope=FindingScope.STATEMENT,
        description=(
            "payable == calculated + recouped(signed) − reserve_taken + reserve_released "
            "+ carried_forward_in + payable_prev − settlement_paid − carried_forward_out (±$0.02)"
        ),
        check=check_payable_identity,
    )
)
register(
    Rule(
        id="V-STMT-4",
        level=2,
        default_severity=FindingSeverity.BLOCKER,
        scope=FindingScope.STATEMENT,
        description="payable >= 0",
        check=check_payable_not_negative,
    )
)
register(
    Rule(
        id="V-STMT-5",
        level=2,
        default_severity=FindingSeverity.BLOCKER,
        scope=FindingScope.STATEMENT,
        description="payable == 0 has an explained zero_pay_reason",
        check=check_zero_pay_classified,
    )
)
