"""Ingest matching must not depend on upload order.

There were two definitions of "same client": the client-import matcher
(AccountIndex, which indexes space-collapsed and alias forms) and the sorter's
own weaker rule. Which one decided an account's owner depended on whether the
client list or the statements were uploaded first — client-list-first meant the
import linked nothing (no accounts existed yet) and every account was later
resolved by the weak rule. Real filenames drop spaces, so "AJCastillo" never
met "AJ Castillo" and the money sat unmatched.
"""

import pytest

from app.models.statements import Publisher, Writer, WriterKind, WriterStatus
from app.services.statement_ingest.sorter import _match_existing_writer


class _Parsed:
    def __init__(self, display_name, is_house=False):
        self.display_name = display_name
        self.is_house = is_house


@pytest.fixture()
def roster(session):
    pub = Publisher(name="Regalias Digitales")
    session.add(pub)
    session.commit()

    def add(name):
        w = Writer(canonical_name=name, publisher_id=pub.id, kind=WriterKind.CLIENT,
                   status=WriterStatus.ACTIVE, is_client=True)
        session.add(w)
        return w

    for n in [
        "AJ Castillo", "Los Tucanes De Tijuana", "J Boog", "William Luna",
        "California Honey Drops", "Akwid / AkwidAfterVydia",
        "AMS Records - Malagon Publishing",
        # deliberately ambiguous pair: same pre-parenthetical name
        "Don Kalavera (Loudness Music)", "Don Kalavera (Mastered Trax)",
    ]:
        add(n)
    session.commit()
    return session


@pytest.mark.parametrize("filename_name,expected", [
    ("AJCastillo", "AJ Castillo"),                       # dropped space
    ("Los Tucanes DeTijuana", "Los Tucanes De Tijuana"),  # dropped space mid-name
    ("JBoog", "J Boog"),
    ("WilliamLuna", "William Luna"),
    ("California HoneyDrops", "California Honey Drops"),
    ("AkwidAfterVydia", "Akwid / AkwidAfterVydia"),       # "/" alias list
    ("Malagon Publishing", "AMS Records - Malagon Publishing"),  # " - " split
])
def test_filename_variants_resolve_to_the_client(roster, filename_name, expected):
    w = _match_existing_writer(roster, _Parsed(filename_name))
    assert w is not None, f"{filename_name!r} matched nothing"
    assert w.canonical_name == expected


def test_full_name_beats_a_shared_prefix(roster):
    """The money-losing case: two clients share a pre-parenthetical name, so a
    flat overlap test picks by row id and pays the wrong one."""
    w = _match_existing_writer(roster, _Parsed("Don Kalavera (Mastered Trax)"))
    assert w.canonical_name == "Don Kalavera (Mastered Trax)"
    w2 = _match_existing_writer(roster, _Parsed("Don Kalavera (Loudness Music)"))
    assert w2.canonical_name == "Don Kalavera (Loudness Music)"


def test_an_unknown_name_still_returns_none(roster):
    """Widening the match must not make everything match something."""
    assert _match_existing_writer(roster, _Parsed("Completely Unrelated Artist")) is None
