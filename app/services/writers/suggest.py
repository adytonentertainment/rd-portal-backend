"""Guess which client an UNMATCHED statement account probably belongs to.

An unmatched record exists only to hold a beneficiary account no client-list
row claimed. All we know about it is the name printed on the statement
filename — so the publisher is left staring at "JN0232 RedZed" trying to
remember whether that is a current client, a former one, or a typo of someone
already on the roster.

This proposes the nearest client by name so that question has a starting
point. It is deliberately only a SUGGESTION: it never re-points an account,
never writes anything, and is rendered as "did you mean" — the same rule the
client-list import follows, because a wrong merge silently sends one client's
royalties to another and is close to unrecoverable.

Scoring reuses the import matcher's normalisation (accents, legal suffixes,
"Name (Group)" splitting) so a name judged similar here is judged the same way
there, rather than two matchers disagreeing about the same two strings.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from app.models.statements import BeneficiaryAccount, Writer, WriterStatus
from app.services.client_import.matcher import _token_set, normalize

# Below this the "did you mean" is noise. The import matcher links siblings at
# 0.72; a human-facing hint can be a little looser, but not so loose that every
# unmatched row gets a confident-looking wrong answer.
SUGGEST_THRESHOLD = 0.55


def _score(a_tokens, b_tokens) -> float:
    """Token-set Jaccard — same shape the import matcher's fuzzy pass uses."""
    if not a_tokens or not b_tokens:
        return 0.0
    return len(a_tokens & b_tokens) / len(a_tokens | b_tokens)


def suggest_for_names(
    names: Dict[int, str], candidates: Sequence[Writer]
) -> Dict[int, Optional[dict]]:
    """{writer_id: best {id,name,score,method} or None} for the given names."""
    index = []
    for c in candidates:
        for raw in (c.canonical_name, c.payee_name):
            if raw:
                index.append((normalize(raw), _token_set(raw), c))

    out: Dict[int, Optional[dict]] = {}
    for writer_id, raw_name in names.items():
        if not raw_name:
            out[writer_id] = None
            continue
        norm = normalize(raw_name)
        tokens = _token_set(raw_name)

        best = None
        for cand_norm, cand_tokens, cand in index:
            if cand.id == writer_id:
                continue
            # An exact normalised hit is the strong case: same name, two rows.
            if cand_norm and cand_norm == norm:
                best = {"id": cand.id, "name": cand.canonical_name,
                        "score": 1.0, "method": "exact"}
                break
            score = _score(tokens, cand_tokens)
            if score >= SUGGEST_THRESHOLD and (best is None or score > best["score"]):
                best = {"id": cand.id, "name": cand.canonical_name,
                        "score": round(score, 2), "method": "fuzzy"}
        out[writer_id] = best
    return out


def suggest_clients_for(db: Session, writer_ids: Sequence[int]) -> Dict[int, Optional[dict]]:
    """Best client guess for each unmatched writer id, in two queries.

    Batched on purpose: this runs per roster page, and a per-row query against
    the whole client list would be 25 scans of 876 names to render one screen.
    """
    ids = [i for i in writer_ids if i is not None]
    if not ids:
        return {}

    unmatched = db.query(Writer).filter(Writer.id.in_(ids)).all()
    # The account's filename name is the account's real identity; the writer's
    # canonical name is a placeholder derived from it. Prefer the account.
    account_names = {}
    for acct in db.query(BeneficiaryAccount).filter(BeneficiaryAccount.writer_id.in_(ids)):
        if acct.display_name and acct.writer_id not in account_names:
            account_names[acct.writer_id] = acct.display_name

    names = {w.id: account_names.get(w.id) or w.canonical_name for w in unmatched}

    # Candidates are REAL clients only: a roster row with a client type, still
    # active, not a house account. Suggesting one placeholder for another just
    # moves the question.
    candidates: List[Writer] = (
        db.query(Writer)
        .filter(
            Writer.kind.isnot(None),
            Writer.is_house_account.is_(False),
            Writer.status == WriterStatus.ACTIVE,
        )
        .all()
    )
    return suggest_for_names(names, candidates)
