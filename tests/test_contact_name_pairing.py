"""Which contact name belongs to which address.

The client list gives names and emails as two comma-separated cells that do not
reliably line up — 295 of 888 delivered rows carry different counts. Pairing by
position regardless is what put "Che" on accounting@monkmusic.co and "Nate" on
che@chekothari.com. A wrong name is worse than none: it is what an admin reads
when deciding who to send someone's royalty statements to.
"""

from app.services.client_import.importer import pair_names_to_emails


def test_equal_counts_keep_the_spreadsheets_own_order():
    """Two thirds of the file lines up; that convention is trusted."""
    got = pair_names_to_emails(["ann@x.com", "bob@y.com"], ["Ann", "Bob"])
    assert got == {"ann@x.com": "Ann", "bob@y.com": "Bob"}


def test_the_reported_caribbean_row_comes_out_right():
    """The exact correction the publisher asked for, derived rather than typed:
    the shared accounting address takes no name, and the two personal addresses
    take theirs."""
    got = pair_names_to_emails(
        ["accounting@monkmusic.co", "che@chekothari.com",
         "nate@machelmontano.com", "graeme@monkmusic.co"],
        ["Che", "Nate", "Graeme"],
    )
    assert got == {
        "accounting@monkmusic.co": None,
        "che@chekothari.com": "Che",
        "nate@machelmontano.com": "Nate",
        "graeme@monkmusic.co": "Graeme",
    }


def test_a_name_nothing_supports_is_left_off_rather_than_guessed():
    """One name, two addresses, and nothing in either address to tie it to. The
    old code put the name on the first address; it belonged to the second."""
    got = pair_names_to_emails(
        ["loudnessmusicoficial@gmail.com", "aramtve@gmail.com"], ["Manuel (Eanz)"]
    )
    assert got == {"loudnessmusicoficial@gmail.com": None, "aramtve@gmail.com": None}


def test_more_names_than_addresses_does_not_invent_an_owner():
    got = pair_names_to_emails(["yaimamusicproject@gmail.com"], ["Pepper", "Mas"])
    assert got == {"yaimamusicproject@gmail.com": None}


def test_a_parenthetical_name_can_still_match_on_either_word():
    got = pair_names_to_emails(
        ["info@label.com", "eanz@label.com"], ["Manuel (Eanz)"]
    )
    assert got["eanz@label.com"] == "Manuel (Eanz)"
    assert got["info@label.com"] is None


def test_one_name_claims_one_address_only():
    """Two addresses could both match 'nate'; the name is not applied twice."""
    got = pair_names_to_emails(
        ["nate@a.com", "nate@b.com", "che@c.com"], ["Nate", "Che"]
    )
    assert sum(1 for v in got.values() if v == "Nate") == 1
    assert got["che@c.com"] == "Che"


def test_short_fragments_cannot_claim_an_address():
    """A two-letter token would match half the file."""
    got = pair_names_to_emails(["administration@x.com"], ["Al", "Bo"])
    assert got == {"administration@x.com": None}


def test_no_names_at_all_is_not_an_error():
    assert pair_names_to_emails(["a@x.com"], []) == {"a@x.com": None}
    assert pair_names_to_emails([], ["Ann"]) == {}
