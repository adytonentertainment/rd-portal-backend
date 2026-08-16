"""Name-match client-list rows to statement accounts (infra PRD §3.2, §15.1).

The delivered client list carries NO beneficiary IDs, so this is the whole
join between the two data assets. It is deterministic and explainable — every
match records the method and the string that matched — and it is only ever a
*proposal*: an admin confirms before anything is distributed (§7.2).

Design:
  - Normalize aggressively: lowercase, strip accents, drop legal suffixes and
    leading "the", collapse punctuation/whitespace.
  - A statement account contributes several index keys: its full display name,
    the pre-parenthetical part ("Gussy Lau" from "Gussy Lau (Loudness Music)"),
    and the parenthetical group ("Loudness Music") as a *group* key.
  - A client row contributes several candidate names: the Artist/Publisher
    Name and Payee Name, each further split on "/" and " - " (rows are often
    "Real Name / Alias (Group)"), plus pre-parenthetical parts.
  - Exact normalized hit on any candidate == high confidence. Otherwise best
    token-set Jaccard over a threshold == probable. Else unmatched -> queue.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

_LEGAL_SUFFIXES = {
    "llc", "inc", "ltd", "sa", "sl", "sas", "sac", "cv", "de", "sa de cv",
    "music", "publishing", "records", "entertainment", "group", "productions",
}
_STOPWORDS = {"the", "and", "&"}
_PAREN_RE = re.compile(r"\(([^)]*)\)")
_NONWORD_RE = re.compile(r"[^a-z0-9\s]")


def strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def normalize(name: str) -> str:
    """Canonical form for exact comparison."""
    s = strip_accents(name or "").lower()
    s = _NONWORD_RE.sub(" ", s)
    tokens = [t for t in s.split() if t and t not in _STOPWORDS]
    return " ".join(tokens)


def _token_set(name: str) -> Set[str]:
    return {
        t for t in normalize(name).split()
        if t not in _LEGAL_SUFFIXES and len(t) > 1
    }


def _pre_paren(name: str) -> str:
    return _PAREN_STRIP_RE.sub("", name).strip()


_PAREN_STRIP_RE = re.compile(r"\s*\([^)]*\)")


def _identity_forms(name: str) -> Set[str]:
    """Normalized forms that are still the SAME entity as `name`:
    the full string, its pre-parenthetical part, and separator-split alias
    parts ("AMS Records - Malagon Publishing" → both halves) — plus a
    space-collapsed variant of each, because statement filenames drop spaces
    ("Los Tucanes DeTijuana", "WilliamLuna").

    Parenthetical GROUP tags are deliberately excluded: "(Loudness Music)" is
    shared by every member of a family, so treating it as an identity would
    collapse the whole group into one client (see AccountIndex._by_group,
    which keeps group hits at lower confidence on purpose).
    """
    out: Set[str] = set()
    if not name:
        return out
    parts = [name, _pre_paren(name)]
    for part in re.split(r"\s*[/]\s*|\s+-\s+", name):
        part = part.strip()
        if part:
            parts.extend([part, _pre_paren(part)])
    for p in parts:
        n = normalize(p)
        if n:
            out.add(n)
            out.add(n.replace(" ", ""))
    return out


def _primary_forms(name: str) -> Set[str]:
    """The row's PRIMARY identity: its full name and its FIRST "/" segment
    (plus pre-parenthetical and space-collapsed variants). An account whose own
    name equals one of these belongs to this row more strongly than to a row
    that merely mentions the name somewhere — the tie-break that decides
    "AmpLive" between its own row and "AmpLive - Anthony Anderson NEW"."""
    out: Set[str] = set()
    if not name:
        return out
    first = re.split(r"\s*/\s*", name)[0].strip()
    for form in (name, _pre_paren(name), first, _pre_paren(first)):
        n = normalize(form)
        if n:
            out.add(n)
            out.add(n.replace(" ", ""))
    return out


def _split_candidates(name: str) -> List[str]:
    """A client name field can pack several identities; explode them."""
    out: List[str] = []
    if not name:
        return out
    out.append(name)
    out.append(_pre_paren(name))
    for group in _PAREN_RE.findall(name):
        out.append(group)
    # Split on "/" FIRST (the alias separator), keeping each whole segment, then
    # additionally offer its " - " sub-parts. Splitting on both at once destroyed
    # compound identities: "AmpLive / AmpLive - Anthony Anderson" collapsed to
    # {AmpLive, Anthony Anderson} and the account literally named
    # "AmpLive - Anthony Anderson" could never be claimed exactly.
    for segment in re.split(r"\s*/\s*", name):
        segment = segment.strip()
        if not segment:
            continue
        out.append(segment)
        out.append(_pre_paren(segment))
        for part in re.split(r"\s+-\s+", segment):
            part = part.strip()
            if part:
                out.append(part)
                out.append(_pre_paren(part))
    # de-dup, drop empties/very short
    seen, uniq = set(), []
    for c in out:
        c = c.strip()
        k = normalize(c)
        if k and k not in seen and len(k) >= 2:
            seen.add(k)
            uniq.append(c)
    return uniq


@dataclass(frozen=True)
class AccountRef:
    """One statement account, from the DB or scanned filenames."""

    account_code: str
    display_name: str
    catalog: Optional[str] = None
    is_house: bool = False


@dataclass
class MatchResult:
    matched: bool
    confidence: str  # "exact" | "probable" | "none"
    score: float
    account_codes: List[str] = field(default_factory=list)
    matched_on: Optional[str] = None      # the client candidate that hit
    matched_display: Optional[str] = None  # the account display it hit
    method: Optional[str] = None           # "name" | "group" | "fuzzy"
    # Subset of account_codes hit by EXACT normalized name (not group/fuzzy).
    # The planner uses this to enforce "exact wins": a code exactly claimed by
    # one row is never left in another row's group/fuzzy sweep.
    exact_codes: List[str] = field(default_factory=list)
    # code -> 2 when the hit was on the account's FULL name, 1 when only on its
    # pre-parenthetical part. The planner prefers the more specific claim when
    # several rows exactly claim one code ("Don Kalavera (Loudness Music)" row
    # beats "Don Kalavera (Mastered Trax)" for the Loudness account).
    exact_strengths: Dict[str, int] = field(default_factory=dict)


class AccountIndex:
    """Inverted index over the statement account population."""

    def __init__(self, accounts: Sequence[AccountRef]):
        self.accounts = list(accounts)
        self._by_name: Dict[str, Set[str]] = {}       # norm display -> codes
        self._by_full_name: Dict[str, Set[str]] = {}  # FULL norm display only -> codes
        self._by_group: Dict[str, Set[str]] = {}      # norm parenthetical -> codes
        self._token_index: List[Tuple[Set[str], str, str]] = []  # (tokens, code, display)
        for a in self.accounts:
            if a.is_house:
                continue
            full_key = normalize(a.display_name)
            full_forms = {f for f in (full_key, full_key.replace(" ", "")) if f}
            for f in full_forms:
                self._by_full_name.setdefault(f, set()).add(a.account_code)
            for key in _identity_forms(a.display_name):
                self._by_name.setdefault(key, set()).add(a.account_code)
            for group in _PAREN_RE.findall(a.display_name):
                gk = normalize(group)
                if gk:
                    self._by_group.setdefault(gk, set()).add(a.account_code)
            toks = _token_set(a.display_name)
            if toks:
                self._token_index.append((toks, a.account_code, a.display_name))

    # An account this close to a candidate is linked too, even alongside an
    # exact hit — so a client's SIBLING accounts (re-issued "-New" codes, a
    # typo'd second account) all fold in, not just the one that matched best.
    _LINK_THRESHOLD = 0.72

    def match(self, row_name: str, payee_name: Optional[str]) -> MatchResult:
        candidates = _split_candidates(row_name)
        if payee_name:
            candidates += [c for c in _split_candidates(payee_name) if c not in candidates]
        cand_norm = [(normalize(c), _token_set(c)) for c in candidates]

        # exact normalized-name hits (all accounts sharing an exact name)
        exact_codes, exact_cand = set(), None
        exact_strengths: Dict[str, int] = {}
        primary = _primary_forms(row_name)
        for (cnorm, _ct), cand in zip(cand_norm, candidates):
            for key in {cnorm, cnorm.replace(" ", "")} if cnorm else set():
                if key in self._by_name:
                    full_hit = self._by_full_name.get(key, set())
                    is_primary = key in primary
                    for code in self._by_name[key]:
                        if code in full_hit:
                            strength = 3 if is_primary else 2
                        else:
                            strength = 1
                        if strength > exact_strengths.get(code, 0):
                            exact_strengths[code] = strength
                    exact_codes |= self._by_name[key]
                    exact_cand = exact_cand or cand

        # group hits (whole "(Loudness Music)" family)
        group_codes, group_cand = set(), None
        for (cnorm, _ct), cand in zip(cand_norm, candidates):
            if cnorm and cnorm in self._by_group:
                group_codes |= self._by_group[cnorm]
                group_cand = group_cand or cand

        # fuzzy: score every account by the best of token-set Jaccard, whole-
        # string similarity (typos/suffixes/extra tokens), and subset
        # containment. Collect every account clearing the link threshold — the
        # client's sibling accounts — while tracking the single best for the
        # confidence value and display.
        best_score, best_code, best_disp, best_method = 0.0, None, None, None
        strong_codes = set()
        for toks, code, disp in self._token_index:
            dnorm = normalize(disp)
            if not toks or not dnorm:
                continue
            acc_score, acc_method, acc_overlap = 0.0, None, False
            for cnorm, ctoks in cand_norm:
                if not ctoks or not cnorm:
                    continue
                inter = len(ctoks & toks)
                jac = inter / len(ctoks | toks) if inter else 0.0
                subset = bool(inter) and (ctoks <= toks or toks <= ctoks)
                sim = difflib.SequenceMatcher(None, cnorm, dnorm).ratio()
                score = max(jac, sim, 0.72 if subset else 0.0)
                if score > acc_score:
                    acc_score = score
                    acc_method = "subset" if subset and score <= jac + 0.01 else (
                        "similar" if sim >= jac else "fuzzy"
                    )
                if inter:
                    acc_overlap = True
            # A fuzzy/sibling link must share at least one DISTINCTIVE token
            # (_token_set strips legal/generic suffixes). Whole-string
            # similarity alone let "Cotorra Music Group" link to "Monk Music
            # Group…" purely on the generic "music group" suffix.
            if acc_score >= self._LINK_THRESHOLD and acc_overlap:
                strong_codes.add(code)
            if acc_score > best_score and (acc_overlap or acc_score >= 0.85):
                best_score, best_code, best_disp, best_method = acc_score, code, disp, acc_method

        if exact_codes:
            codes = sorted(exact_codes | strong_codes)
            return MatchResult(True, "exact", 1.0, codes, exact_cand,
                               matched_display=exact_cand, method="name",
                               exact_codes=sorted(exact_codes),
                               exact_strengths=dict(exact_strengths))
        if group_codes:
            return MatchResult(True, "probable", 0.8, sorted(group_codes | strong_codes),
                               group_cand, matched_display=group_cand, method="group")
        if best_score >= 0.58:
            codes = sorted(strong_codes) if strong_codes else [best_code]
            return MatchResult(True, "probable", round(best_score, 3), codes, None,
                               matched_display=best_disp, method=best_method or "fuzzy")

        return MatchResult(False, "none", round(best_score, 3), [], None,
                           matched_display=best_disp, method=None)
