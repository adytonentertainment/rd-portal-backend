"""Working out which entries are catalogs the publisher acquired.

The client list says it in the NAME and nowhere else: "Likybo NEW" is the
catalog still theirs, plain "Likybo" is the old one the publisher now owns. Get
this backwards and a writer either loses sight of their own royalties or is
shown a catalog they sold.
"""

import pytest

from app.models.statements import Publisher, Writer, WriterKind
from app.services.client_import.importer import mark_publisher_owned_counterparts


@pytest.fixture()
def pub(session):
    p = Publisher(name="Regalias Digitales")
    session.add(p)
    session.commit()
    return p


def mk(session, pub, name, *, partner=False, house=False):
    w = Writer(
        publisher_id=pub.id,
        canonical_name=name,
        kind=WriterKind.COMMISSION_PARTNER if partner else WriterKind.CLIENT,
        is_client=not partner,
        is_commission_partner=partner,
        is_house_account=house,
    )
    session.add(w)
    session.flush()
    return w


def test_the_counterpart_of_a_NEW_entry_is_the_acquired_one(session, pub):
    old = mk(session, pub, "Likybo")
    new = mk(session, pub, "Likybo NEW")
    session.commit()

    assert mark_publisher_owned_counterparts(session) == 1
    assert old.publisher_owned is True
    assert new.publisher_owned is False   # still the writer's


def test_the_commission_entry_is_never_marked(session, pub):
    """J Swey, Likybo and Dante Storch each have a same-named row on the partner
    sheet. That is commission they earned — their money. Marking it would hide
    their own income from them."""
    old = mk(session, pub, "J Swey")
    commission = mk(session, pub, "J Swey", partner=True)
    mk(session, pub, "J Swey NEW")
    session.commit()

    mark_publisher_owned_counterparts(session)
    assert old.publisher_owned is True
    assert commission.publisher_owned is False


def test_a_NEW_entry_with_no_counterpart_marks_nothing(session, pub):
    """One row on the delivered list is like this. Inventing a counterpart would
    be worse than leaving it for a human."""
    mk(session, pub, "AmpLive NEW")
    session.commit()
    assert mark_publisher_owned_counterparts(session) == 0


def test_an_ordinary_client_is_untouched(session, pub):
    """Only a NEW sibling makes an entry acquired. Without one, a plain name is
    just a client — which is almost every row on the roster."""
    plain = mk(session, pub, "Amenazzy")
    session.commit()
    assert mark_publisher_owned_counterparts(session) == 0
    assert plain.publisher_owned is False


def test_it_is_idempotent_and_does_not_overrule_a_human(session, pub):
    """Re-running an import must not thrash the flag, and an admin who set it by
    hand keeps their decision."""
    old = mk(session, pub, "Dante Storch")
    mk(session, pub, "Dante Storch NEW")
    manual = mk(session, pub, "Someone Else")
    manual.publisher_owned = True
    session.commit()

    assert mark_publisher_owned_counterparts(session) == 1
    assert mark_publisher_owned_counterparts(session) == 0   # nothing left to do
    assert old.publisher_owned is True
    assert manual.publisher_owned is True   # untouched


def test_a_house_account_is_left_alone(session, pub):
    house = mk(session, pub, "Regalias Digitales", house=True)
    mk(session, pub, "Regalias Digitales NEW")
    session.commit()
    mark_publisher_owned_counterparts(session)
    assert house.publisher_owned is False   # already excluded by its own flag
