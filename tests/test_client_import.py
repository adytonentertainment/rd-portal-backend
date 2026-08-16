"""Client-list importer: parser, matcher, validator, and DB apply."""

from app.models.statements import (
    BeneficiaryAccount,
    Catalog,
    Contact,
    Publisher,
    Writer,
    WriterContact,
    WriterKind,
)
from app.services.client_import.matcher import AccountIndex, AccountRef
from app.services.client_import.parser import ClientRow, ParsedEmail
from app.services.client_import.parser import WriterKind as ParsedKind
from app.services.client_import import importer
from app.services.client_import.validator import validate_rows


def _row(name, payee, emails, kind=ParsedKind.CLIENT, catalogs=("MECH", "YT"),
         contact_names=("Mgr",), unknown=(), lang="en", row_no=1, sheet="Client List"):
    return ClientRow(
        sheet=sheet, row_no=row_no, kind=kind, name=name, payee_name=payee,
        emails=[ParsedEmail(e, "@" in e and "." in e.split("@")[-1]) for e in emails],
        contact_names=list(contact_names), catalogs=list(catalogs),
        unknown_catalog_tokens=list(unknown), preferred_language=lang,
    )


def _seed_placeholder(db, code, display, catalog, is_house=False):
    """Mimic what statement ingestion creates: a placeholder writer + account."""
    pub = db.query(Publisher).first()
    if pub is None:
        pub = Publisher(name="Regalias Digitales")
        db.add(pub)
        db.flush()
    w = Writer(publisher_id=pub.id, canonical_name=display, is_house_account=is_house)
    db.add(w)
    db.flush()
    acct = BeneficiaryAccount(writer_id=w.id, account_code=code, catalog=catalog)
    db.add(acct)
    db.flush()
    return acct


# --- matcher -----------------------------------------------------------------

def test_matcher_exact_and_group_and_none():
    idx = AccountIndex([
        AccountRef("JN0232", "RedZed", "MECH"),
        AccountRef("C00616", "RedZed", "YT"),
        AccountRef("JN0345h", "ElReghosg (Loudness Music)", "MECH"),
        AccountRef("CS0001", "Regalias Digitales, LLC", "YT", is_house=True),
    ])
    exact = idx.match("RedZed", "RedZed")
    assert exact.matched and exact.confidence == "exact"
    assert set(exact.account_codes) == {"JN0232", "C00616"}

    # group parenthetical resolves the family, lower confidence
    grp = idx.match("Loudness Music", None)
    assert grp.matched and grp.method == "group"

    miss = idx.match("Totally Unknown Artist", "Nobody At All")
    assert not miss.matched and miss.confidence == "none"

    # house accounts are excluded from the index
    assert idx.match("Regalias Digitales, LLC", None).matched is False


def test_matcher_ignores_accents_and_legal_suffix():
    idx = AccountIndex([AccountRef("C00475", "Sylvan LaCue", "YT")])
    assert idx.match("Sylvan LaCué", None).matched


# --- validator ---------------------------------------------------------------

def test_validator_flags_bad_email_catalog_and_dupes():
    rows = [
        _row("A", "A", ["ok@x.com"], unknown=["ST"]),
        _row("B", "B", ["not-an-email"], row_no=2),
        _row("Dup", "Same Payee", ["c@x.com"], row_no=3),
        _row("Dup2", "Same Payee", ["d@x.com"], row_no=4),
        _row("NoMail", "NoMail", ["bad"], row_no=5),
    ]
    findings = validate_rows(rows)
    ids = {f.rule_id for f in findings}
    assert "C-BAD-EMAIL" in ids
    assert "C-BAD-CATALOG" in ids
    assert "C-NAME-DUP" in ids
    assert "C-NO-EMAIL" in ids


def test_validator_house_collision_and_unlisted():
    rows = [_row("House Claimer", "House Claimer", ["x@y.com"])]
    from app.services.client_import.matcher import MatchResult
    matches = {id(rows[0]): MatchResult(True, "exact", 1.0, ["CS0001"], "House Claimer")}
    findings = validate_rows(rows, matches=matches,
                             all_account_codes={"CS0001", "JN0999"})
    ids = {f.rule_id for f in findings}
    # House special-casing is disabled, so CS0001 is an ordinary account a
    # client row may legitimately claim — no collision finding.
    assert "C-HOUSE-COLLISION" not in ids
    assert "C-UNLISTED-ACCOUNT" in ids  # JN0999 has no client row


# --- importer apply (the merge) ----------------------------------------------

def test_apply_merges_two_placeholder_accounts_into_one_writer(session):
    # A person ingested as two placeholders (mechanical + youtube).
    _seed_placeholder(session, "JN0232", "RedZed", Catalog.MECH)
    _seed_placeholder(session, "C00616", "RedZed", Catalog.YT)
    session.commit()

    rows = [_row("RedZed", "RedZed", ["redzed@mgmt.com"])]
    result = importer.apply_rows(session, rows)

    assert result["created_writers"] == 1
    assert result["repointed_accounts"] == 2
    assert result["merged_placeholders"] == 2

    # exactly one client writer now owns both accounts
    client_writers = session.query(Writer).filter(Writer.kind.isnot(None)).all()
    assert len(client_writers) == 1
    w = client_writers[0]
    assert w.kind == WriterKind.CLIENT
    codes = {a.account_code for a in session.query(BeneficiaryAccount)
             .filter(BeneficiaryAccount.writer_id == w.id)}
    assert codes == {"JN0232", "C00616"}
    # placeholder writers are gone
    assert session.query(Writer).filter(Writer.kind.is_(None)).count() == 0


def test_apply_is_idempotent_and_shares_contacts(session):
    _seed_placeholder(session, "JN0303", "Arelys Henao", Catalog.MECH)
    _seed_placeholder(session, "C00303b", "Arelys Henao", Catalog.YT)
    _seed_placeholder(session, "JN0446", "Amilcar Boscan", Catalog.MECH)
    session.commit()

    attorney = "egallo@gcvalaw.com"
    rows = [
        _row("Arelys Henao", "Arelys Henao", [attorney]),
        _row("Amilcar Boscan", "Amilcar Boscan", [attorney]),
    ]
    importer.apply_rows(session, rows)
    # second run creates nothing new
    r2 = importer.apply_rows(session, rows)
    assert r2["created_writers"] == 0
    assert r2["repointed_accounts"] == 0
    assert r2["created_contacts"] == 0

    # one shared contact linked to both writers
    contact = session.query(Contact).filter(Contact.email == attorney).one()
    links = session.query(WriterContact).filter(
        WriterContact.contact_id == contact.id).all()
    assert len(links) == 2


def test_apply_creates_unmatched_rows_without_attaching_accounts(session):
    """The roster IS the client list: an unmatched row still becomes a client
    (with contacts), it just owns no accounts — the account stays with its
    placeholder and the row goes to the resolution queue."""
    _seed_placeholder(session, "JN0001", "Beeda Weeda", Catalog.MECH)
    session.commit()
    rows = [_row("Totally New Signing", "Totally New Signing", ["new@x.com"])]
    result = importer.apply_rows(session, rows)
    assert result["created_writers"] == 1
    assert result["repointed_accounts"] == 0  # nothing guessed
    w = session.query(Writer).filter(Writer.canonical_name == "Totally New Signing").one()
    assert session.query(BeneficiaryAccount).filter(
        BeneficiaryAccount.writer_id == w.id).count() == 0
    # the seeded account keeps its placeholder owner
    acct = session.query(BeneficiaryAccount).filter(
        BeneficiaryAccount.account_code == "JN0001").one()
    assert session.get(Writer, acct.writer_id).kind is None


# --- manual resolution (the queue) -------------------------------------------

def test_resolve_row_attaches_admin_chosen_account(session):
    # A client whose statement account display shares nothing with the roster
    # name — does NOT auto-match, so the admin picks the account manually.
    _seed_placeholder(session, "C00901", "Zed Kollective", Catalog.YT)
    session.commit()

    rows = [_row("Atu", "Atupele Ndisale", ["atu@x.com"])]
    # confirmed_only apply creates the identity but attaches NO account
    importer.apply_rows(session, rows)
    assert session.query(Writer).filter(Writer.kind.isnot(None)).count() == 1
    seeded = session.query(BeneficiaryAccount).filter(
        BeneficiaryAccount.account_code == "C00901").one()
    assert session.get(Writer, seeded.writer_id).kind is None

    summary = importer.resolve_row(session, rows[0], ["C00901"])
    assert summary["repointed_accounts"] == 1
    assert summary["merged_placeholders"] == 1
    acct = session.query(BeneficiaryAccount).filter(
        BeneficiaryAccount.account_code == "C00901").one()
    w = session.get(Writer, acct.writer_id)
    assert w.kind == WriterKind.CLIENT
    # the client's identity is the ARTIST name; the payee is only who the
    # money is made out to
    assert w.canonical_name == "Atu"
    assert w.payee_name == "Atupele Ndisale"
    # original ingestion display name preserved as an alias
    aliases = {a.alias_name for a in w.aliases}
    assert "Zed Kollective" in aliases


def test_resolve_row_with_no_accounts_creates_identity_only(session):
    rows = [_row("New Signing No Earnings", "New Signing No Earnings", ["ns@x.com"])]
    summary = importer.resolve_row(session, rows[0], [])
    assert summary["created_writers"] == 1
    assert summary["repointed_accounts"] == 0
    assert session.query(Contact).filter(Contact.email == "ns@x.com").count() == 1


def test_client_row_from_dict_roundtrips_enough_to_resolve(session):
    _seed_placeholder(session, "JN0442", "LouLou Ghelichkhani", Catalog.MECH)
    session.commit()
    stored = {
        "sheet": "Client List", "row_no": 5, "kind": "client",
        "name": "LouLou G", "payee_name": "LouLou Ghelichkhani",
        "emails": ["loulou@x.com"], "contact_names": ["LouLou"],
        "catalogs": ["MECH"], "language": "en",
    }
    row = importer.client_row_from_dict(stored)
    summary = importer.resolve_row(session, row, ["JN0442"])
    assert summary["repointed_accounts"] == 1
    contact = session.query(Contact).filter(Contact.email == "loulou@x.com").one()
    assert contact.display_name == "LouLou"


def test_exact_row_beats_group_sweep(session):
    """Regression: the Luna Negra steal. A row claiming the '(Luna Negra)'
    group must NOT take accounts whose display name exactly matches another
    client row — regardless of apply order."""
    _seed_placeholder(session, "C00139", "Abel De Luna (Luna Negra)", Catalog.YT)
    _seed_placeholder(session, "C00139f", "Edipurepecha (Luna Negra)", Catalog.YT)
    _seed_placeholder(session, "C00139k", "Agave (Luna Negra)", Catalog.YT)
    session.commit()

    rows = [
        # group claimer applied LAST used to steal the members' accounts
        _row("Edipurepecha", "Edipurepecha", ["edi@x.com"]),
        _row("Agave", "Agave", ["agave@x.com"]),
        _row("Abel De Luna (Luna Negra)", "Abel De Luna", ["abel@x.com"]),
    ]
    importer.apply_rows(session, rows, confirmed_only=False)

    def owner_of(code):
        acct = session.query(BeneficiaryAccount).filter(
            BeneficiaryAccount.account_code == code).one()
        return session.get(Writer, acct.writer_id).canonical_name

    assert owner_of("C00139f") == "Edipurepecha"
    assert owner_of("C00139k") == "Agave"
    assert owner_of("C00139") == "Abel De Luna (Luna Negra)"  # named by its own row
