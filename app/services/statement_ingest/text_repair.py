"""Repair mis-encoded text in statement detail (mojibake).

WHAT IS BROKEN, AND WHERE. The corruption is NOT ours. It is already present
in the bytes Regalias Digitales ships: opening the source workbooks directly
shows 'El CafÃ©' and 'Adi¾s Amigo' in the cells themselves. Our parser reads
them faithfully, and writers then see the mangled title in their portal. So
this module does not change how anything is DECODED — it repairs known-bad
text on the way in.

THREE CLASSES, from two different export runs:

  utf8_as_latin1    'El CafÃ©'      -> 'El Café'
        UTF-8 bytes decoded as Latin-1. C3 A9 ('é') shows as 'Ã©'.

  titlecased_utf8   'Esa Niã±A'     -> 'Esa Niña'
        The same fault, but the source then TITLE-CASED the broken string.
        That lowercased the 'Ã' to 'ã' (so a plain UTF-8 repair no longer
        decodes) and uppercased the letter after the mangled pair. Both have
        to be undone, in that order.

  latin1_as_cp850   'Adi¾s Amigo'   -> 'Adiós Amigo'
        Latin-1 bytes decoded as CP850. F3 ('ó') shows as '¾'.

DETECTION IS TWO-LEVEL, and both levels are load-bearing.

  * The FILE decides the CLASS. Several artifacts are also ordinary letters,
    and 'Ú' is the trap: 'El Último Viaje' is CORRECT and "repairing" it
    yields the nonsense 'El éltimo Viaje', while 'cafÚ' in a corrupted file
    really is 'café'. The character alone cannot tell you which; the file can,
    because it is a single export written with a single encoding.
  * The STRING decides WHETHER. A file is NOT uniformly broken — the real
    2026H1 corpus holds 'Adi¾s Amigo (En Vivo)' (corrupt) and 'Adiós Amigo'
    (already correct) side by side. Repairing a whole file blindly turns the
    correct ones INTO mojibake: 'Adiós' -> 'Adi¢s', 'Así' -> 'As¡', because
    round-tripping already-good text through the wrong codec corrupts it.
    So a string is only touched when it actually carries an artifact of its
    file's class.

WHY THE OLD ONE-LEVEL RULE WAS WRONG.
Of the statements carrying 'Ú', 52 have no other corruption marker (leave them
alone) and 76 sit in files riddled with it. Class detection therefore uses only
characters that CANNOT occur in a real title — '¾', box-drawing, 'Ã©'-style
pairs — and the per-string gate then keeps the untouched strings untouched.

A repair that fails validation is discarded and the original kept. Showing a
writer a mangled title is bad; showing them a differently mangled one, or a
title we silently invented, is worse.
"""

import re
import unicodedata
from typing import Dict, Iterable, List, Optional

from app.logger.logger import get_logger

logger = get_logger("text_repair")

# --- detection ---------------------------------------------------------------

# A UTF-8 lead byte for Latin-1 range text ('Ã'/'Â') followed by a continuation
# byte, as it appears once decoded as Latin-1. Nothing in a Latin-script title
# legitimately contains these pairs.
_UTF8_AS_LATIN1 = re.compile(r"[ÃÂ][\x80-\xbf]")

# The same, after the source title-cased it and lowercased the lead byte.
_TITLECASED_UTF8 = re.compile(r"ã[\x80-\xbf]")

# CP850 renderings of Latin-1 accented characters that cannot be ordinary text:
# '¾'(ó), 'Ë'(Ó), 'Ð'(Ñ) and the box-drawing/block glyphs for the capitals.
# Deliberately EXCLUDES 'ß' 'Ý' '·' '±' 'Ú' — each is a real letter or symbol
# somewhere, so none of them may decide that a file is corrupt. Once a file is
# known corrupt from the characters below, they get repaired along with it.
_LATIN1_AS_CP850 = re.compile(r"[¾ËÐ┌┐┴═╔▄]")

# Second detector for the same class: one of the AMBIGUOUS artifacts sitting
# mid-word, between two letters. 'La Verdad Te Extra±o' and 'Arcßngel' carry no
# unambiguous marker at all, so the strict detector above left them mangled —
# but no Latin script puts '±' or '·' inside a word, so the position gives it
# away where the character alone does not.
#
# The negative lookbehind is essential: in 'Esa Niã±A' the '±' is the SECOND
# BYTE of a mis-decoded UTF-8 pair, not a CP850 artifact, and that file belongs
# to titlecased_utf8. Without it, every such file would be misclassified.
#
# KNOWN LIMITATION: a German title ('Straße') would match on 'ß' and be
# "repaired" to 'Straáe'. Accepted deliberately — this catalogue is
# Spanish-language Latin American music with no such titles in 2,613 files, and
# the alternative leaves real corruption in front of writers.
_CP850_MIDWORD = re.compile(r"(?<=[A-Za-zÀ-ÿ])(?<![ÃÂã])[ßÝ·±³]")

# Layers of corruption to peel. Three covers everything in the real corpus
# (worst case is corrupt -> title-cased -> corrupt again); the bound exists so
# a pathological string cannot spin.
_MAX_REPAIR_PASSES = 3

UTF8_AS_LATIN1 = "utf8_as_latin1"
TITLECASED_UTF8 = "titlecased_utf8"
LATIN1_AS_CP850 = "latin1_as_cp850"

# PER-STRING GATE: does THIS string actually carry the fault its file has?
#
# Wider than the class detectors above, because inside a file already proven
# corrupt these characters are no longer ambiguous — 'ß' really is 'á', '·'
# really is 'ú'. Still excludes 'í' and 'Ú', which stay far more likely to be
# the real letters ('Así', 'El Último') even here, and which we would rather
# leave mangled in the rare case than silently rewrite when they were right.
_ARTIFACTS = {
    UTF8_AS_LATIN1: _UTF8_AS_LATIN1,
    TITLECASED_UTF8: _TITLECASED_UTF8,
    LATIN1_AS_CP850: re.compile(r"[¾ßÝ·±³ÐË┌┐┴═╔▄]"),
}


def has_artifact(value: Optional[str], fault: Optional[str]) -> bool:
    """Whether this individual string shows the given fault. A file is not
    uniformly broken, and touching a string that was already correct is how a
    repair pass creates the very corruption it is meant to remove."""
    if not fault or not value or not isinstance(value, str):
        return False
    pattern = _ARTIFACTS.get(fault)
    return bool(pattern and pattern.search(value))


def classify_value(value: Optional[str], file_fault: Optional[str]) -> Optional[str]:
    """Which fault THIS string shows — which is not always its file's.

    A statement can carry more than one class at once: its titles were written
    upstream at different times, so 'Â¿Para Que?' (UTF-8 read as Latin-1) sits
    in the same statement as 'QuÚ Bonitos A±os' (CP850). Picking one class per
    file and applying it everywhere therefore left whole rows mangled — the
    file's majority class simply did not match them, and the per-string gate
    correctly refused to touch them.

    So the string decides whenever it can say so unambiguously, and the file
    is consulted only for strings whose characters are ambiguous in isolation
    (a lone 'ß' or 'Ú'), which is the one job file-level detection was for.
    """
    if not value or not isinstance(value, str):
        return None
    if _TITLECASED_UTF8.search(value):
        return TITLECASED_UTF8
    if _UTF8_AS_LATIN1.search(value):
        return UTF8_AS_LATIN1
    if _LATIN1_AS_CP850.search(value) or _CP850_MIDWORD.search(value):
        return LATIN1_AS_CP850
    if file_fault == LATIN1_AS_CP850 and has_artifact(value, LATIN1_AS_CP850):
        return LATIN1_AS_CP850
    return None


def detect_encoding_fault(values: Iterable[Optional[str]]) -> Optional[str]:
    """Which corruption, if any, this FILE suffers from.

    Returns one of the class constants, or None to leave the file alone.
    Callers pass every string in the file: one mangled title is enough to
    establish the export was broken, and the rest are then repaired with it
    even where their own characters would have been ambiguous in isolation.
    """
    counts = {UTF8_AS_LATIN1: 0, TITLECASED_UTF8: 0, LATIN1_AS_CP850: 0}
    for value in values:
        if not value or not isinstance(value, str):
            continue
        if _TITLECASED_UTF8.search(value):
            counts[TITLECASED_UTF8] += 1
        elif _UTF8_AS_LATIN1.search(value):
            counts[UTF8_AS_LATIN1] += 1
        elif _LATIN1_AS_CP850.search(value) or _CP850_MIDWORD.search(value):
            counts[LATIN1_AS_CP850] += 1

    best = max(counts, key=lambda k: counts[k])
    return best if counts[best] else None


# --- repair ------------------------------------------------------------------

# Accented lowercase letters whose following character the source's title-case
# pass would have wrongly capitalised.
_ACCENTED_LOWER = "áéíóúñüàèìòùâêîôûãõçä"
_CASE_ARTIFACT = re.compile(r"([" + _ACCENTED_LOWER + r"])([A-ZÁÉÍÓÚÑÜ])")


def _undo_titlecase_artifact(value: str) -> str:
    """'Esa NiñA' -> 'Esa Niña'.

    The source title-cased the BROKEN string. The mangled pair read as a word
    boundary, so the letter after it was capitalised mid-word. An uppercase
    letter directly after a lowercase accented one is that artifact; ordinary
    text does not do this, and restricting the trigger to accented letters
    keeps legitimate intercaps ('McCartney', 'DJ') untouched.
    """
    return _CASE_ARTIFACT.sub(lambda m: m.group(1) + m.group(2).lower(), value)


def _is_plausible(value: str) -> bool:
    """Reject a 'repair' that produced something no title could contain.

    Round-tripping through the wrong codec happily yields C1 control
    characters — 'é' encoded to CP850 and read back as Latin-1 becomes U+0082.
    That is the signal that this string was never corrupt in the first place,
    and the original must be kept.
    """
    if "�" in value:
        return False
    for ch in value:
        if ch in "\t\n\r":
            continue
        if unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cs"):
            return False
    return True


def _repair_one(value: str, fault: str) -> str:
    """Repair a single string, or return it unchanged if the repair is unsafe."""
    try:
        if fault == TITLECASED_UTF8:
            # Restore the lead byte the title-casing lowercased, THEN decode.
            restored = _TITLECASED_UTF8.sub(lambda m: "Ã" + m.group(0)[1:], value)
            repaired = restored.encode("latin-1").decode("utf-8")
            repaired = _undo_titlecase_artifact(repaired)
        elif fault == UTF8_AS_LATIN1:
            repaired = value.encode("latin-1").decode("utf-8")
            repaired = _undo_titlecase_artifact(repaired)
        elif fault == LATIN1_AS_CP850:
            repaired = value.encode("cp850").decode("latin-1")
        else:
            return value
    except (UnicodeDecodeError, UnicodeEncodeError):
        # Not actually corrupt in this way (or not repairable) — keep as-is.
        return value

    # A repaired 'Â\xa0' is a genuine non-breaking space, correctly decoded —
    # and then sits at the front of the title as invisible indentation in the
    # portal. Fold it to an ordinary space and trim, so the repair leaves
    # something a person would actually have typed.
    repaired = repaired.replace("\xa0", " ").strip()

    if repaired == value or not _is_plausible(repaired):
        return value
    return repaired


def repair_text(value: Optional[str], fault: Optional[str]) -> Optional[str]:
    """Public single-value repair. None/non-str and unknown faults pass through.

    A string with no artifact of its file's class is returned untouched — it
    was already correct, and running it through the inverse codec is exactly
    what turns 'Adiós' into 'Adi¢s'.
    """
    # Some titles were mangled more than once upstream — 'Serã\x81 La Ãšltima
    # Vez' is UTF-8 corrupted, title-cased, then corrupted again — so a single
    # pass strips one layer and leaves the rest on screen. Each pass is
    # independently gated and validated, so iterating cannot corrupt: it stops
    # as soon as a pass finds no artifact or declines to change anything.
    current = value
    for _ in range(_MAX_REPAIR_PASSES):
        actual = classify_value(current, fault)
        if actual is None:
            break
        repaired = _repair_one(current, actual)
        if repaired == current:
            break
        current = repaired
    return current


def repair_rows(rows: List[Dict], fields: Iterable[str]) -> int:
    """Repair `fields` across every row of ONE file, in place.

    Detection runs over the whole batch first (see module docstring), so an
    ambiguous string is repaired only on the evidence of its file-mates.
    Returns the number of values changed, for logging and for the backfill to
    report honestly.
    """
    fields = list(fields)
    fault = detect_encoding_fault(
        row.get(f) for row in rows for f in fields
    )
    if fault is None:
        return 0

    changed = 0
    for row in rows:
        for f in fields:
            original = row.get(f)
            repaired = repair_text(original, fault)
            if repaired != original:
                row[f] = repaired
                changed += 1
    if changed:
        logger.info(f"Repaired {changed} mis-encoded value(s) ({fault})")
    return changed
