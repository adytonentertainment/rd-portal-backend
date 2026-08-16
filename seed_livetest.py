"""Seed a fresh, fully-consistent DEV database for live-testing the Writer-Scale
UX features (Client Manager pagination/edit, portal invites + pills, client-
import resolution queue). Writes to an ISOLATED db file so the existing
tunescan_development.db is never touched.

Run:  ENVIRONMENT=DEVELOPMENT SQLALCHEMY_DATABASE_URL="sqlite:////abs/path/verax_livetest.db" python seed_livetest.py
"""
import os

os.environ.setdefault("ENVIRONMENT", "DEVELOPMENT")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.database import Base
import app.models.models  # noqa: F401 — registers every table on Base
from app.models.models import User
from app.models.statements import (
    AccountStatus, BeneficiaryAccount, Cadence, Catalog, Contact, ClientImport,
    ClientImportStatus, ContactRole, Publisher, Writer, WriterContact,
    WriterKind, WriterStatus,
)
from app.routers.auth import bcrypt_context
from app.services.portal import invites as invite_svc

DB_PATH = "/Users/stevengarcia/VERAX_2/verax_backend/verax_livetest.db"
ADMIN_EMAIL = "steven@adytonentertainment.com"
ADMIN_USERNAME = "steven_garcia"
ADMIN_PASSWORD = "TestAdmin123!"

# fresh file every run
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

engine = create_engine(f"sqlite:///{DB_PATH}")
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
db = Session()

# --- admin user --------------------------------------------------------------
admin = User(
    email=ADMIN_EMAIL, username=ADMIN_USERNAME,
    hashed_password=bcrypt_context.hash(ADMIN_PASSWORD),
    activated=True, role="admin", admin_approved=True, royalty_per_stream=0,
)
db.add(admin)

pub = Publisher(name="Regalias Digitales")
db.add(pub)
db.flush()

# --- writers -----------------------------------------------------------------
FIRST = ["Javier", "Luna", "El", "Swifty", "Kill", "OMB", "Bello", "Marco",
         "Sofia", "Diego", "Camila", "Mateo", "Valentina", "Lucas", "Isabella",
         "Andres", "Gabriela", "Nico", "Renata", "Tomas", "Emilia", "Bruno",
         "Carla", "Felipe", "Daniela", "Hugo", "Paula", "Ivan", "Rocio", "Leo"]
LAST = ["Solis", "Negra", "Taiger", "Blue", "Bill", "Peezy", "Musical", "Rios",
        "Vega", "Cruz", "Mora", "Luna", "Soto", "Pena", "Reyes", "Duarte",
        "Campos", "Ibarra", "Nava", "Bravo", "Cano", "Gil", "Mesa", "Rojas",
        "Prado", "Vera", "Sosa", "Leon", "Diaz", "Marin"]
CATS = [["MECH"], ["YT"], ["MECH", "YT"], ["PERF"], ["MECH", "YT", "PERF"]]

writers = []
for i in range(30):
    name = f"{FIRST[i]} {LAST[i]}"
    kind = WriterKind.COMMISSION_PARTNER if i % 7 == 0 else WriterKind.CLIENT
    status = WriterStatus.OFFBOARDED if i % 11 == 10 else WriterStatus.ACTIVE
    w = Writer(
        publisher_id=pub.id, canonical_name=name,
        payee_name=f"{name} LLC" if i % 3 == 0 else None,
        kind=kind, status=status,
        expected_catalogs=CATS[i % len(CATS)],
        preferred_language="es" if i % 2 == 0 else "en",
        cadence=Cadence.QUARTERLY if i % 4 == 0 else Cadence.SEMIANNUAL,
    )
    db.add(w)
    writers.append(w)
db.flush()

# beneficiary accounts on the first several writers
for i, w in enumerate(writers[:12]):
    db.add(BeneficiaryAccount(
        writer_id=w.id, account_code=f"C{600 + i:05d}",
        catalog=Catalog.YT if i % 2 else Catalog.MECH,
        status=AccountStatus.ACTIVE,
    ))
    if i % 5 == 0:
        db.add(BeneficiaryAccount(
            writer_id=w.id, account_code=f"JN{100 + i:04d}", catalog=Catalog.MECH,
            status=AccountStatus.ACTIVE,
        ))

# --- contacts + portal states ------------------------------------------------
# writer[0]: a contact WITH a login  → "Portal active" pill
portal_user = User(
    email="luna.manager@example.com", username="luna_manager",
    hashed_password=bcrypt_context.hash("Managerpass1!"),
    activated=True, royalty_per_stream=0,
)
db.add(portal_user)
db.flush()
c0 = Contact(email="luna.manager@example.com", display_name="Luna Manager", user_id=portal_user.id)
db.add(c0)
db.flush()
db.add(WriterContact(writer_id=writers[0].id, contact_id=c0.id, role=ContactRole.PRIMARY))

# writer[1]: a contact, no login yet
c1 = Contact(email="javier.contact@example.com", display_name="Javier Contact")
db.add(c1)
db.flush()
db.add(WriterContact(writer_id=writers[1].id, contact_id=c1.id, role=ContactRole.MANAGER))
db.commit()

# writer[2]: a PENDING invite  → "Invited" pill
invite_svc.create_invite(db, writers[2].id, "el.taiger@example.com", ContactRole.PRIMARY)
db.commit()

# --- a client import with a resolution queue ---------------------------------
diff = {
    "rows": [
        {"sheet": "Client List", "row_no": 5, "name": "Marco Rios",
         "payee_name": "Marco Rios LLC", "kind": "client", "emails": ["marco@example.com"],
         "catalogs": ["MECH", "YT"], "language": "es", "quarterly": False, "resolved": False,
         "match": {"matched": "Marco Rios", "confidence": "probable", "score": 0.82,
                   "account_codes": ["C00711"], "method": "fuzzy", "matched_on": "name"}},
        {"sheet": "Client List", "row_no": 8, "name": "Sofia Vega",
         "payee_name": None, "kind": "client", "emails": [], "catalogs": ["YT"],
         "language": "en", "quarterly": True, "resolved": False,
         "match": {"matched": "Sofia V.", "confidence": "probable", "score": 0.76,
                   "account_codes": ["C00712"], "method": "fuzzy", "matched_on": "name"}},
        {"sheet": "Client List", "row_no": 12, "name": "Unknown Artist One",
         "payee_name": None, "kind": "client", "emails": [], "catalogs": ["MECH"],
         "language": "es", "quarterly": False, "resolved": False,
         "match": {"matched": None, "confidence": "none", "score": 0.0,
                   "account_codes": [], "method": None, "matched_on": None}},
        {"sheet": "Client List", "row_no": 15, "name": "Unknown Artist Two",
         "payee_name": "UA2 Publishing", "kind": "commission_partner", "emails": ["ua2@example.com"],
         "catalogs": ["PERF"], "language": "en", "quarterly": False, "resolved": False,
         "match": {"matched": None, "confidence": "none", "score": 0.0,
                   "account_codes": [], "method": None, "matched_on": None}},
    ]
}
findings = [
    {"rule_id": "C-UNLISTED-ACCOUNT", "severity": "warning", "sheet": "-",
     "row_no": None, "subject": "C09901", "message": "Statement account has no client-list row"},
    {"rule_id": "C-UNLISTED-ACCOUNT", "severity": "warning", "sheet": "-",
     "row_no": None, "subject": "C09902", "message": "Statement account has no client-list row"},
]
ci = ClientImport(
    publisher_id=pub.id, filename="Client List for Verax.xlsx",
    sha256="seed-fake-hash", uploaded_by=admin.id,
    status=ClientImportStatus.PENDING_REVIEW, row_count=30,
    diff=diff, findings=findings,
    stats={"probable": 2, "unmatched": 2, "unlisted_accounts": 2},
)
db.add(ci)
db.commit()

print(f"Seeded {DB_PATH}")
print(f"  writers: {db.query(Writer).count()}  contacts: {db.query(Contact).count()}"
      f"  accounts: {db.query(BeneficiaryAccount).count()}")
print(f"  client_import id = {ci.id}")
print(f"  admin login: {ADMIN_EMAIL} / {ADMIN_PASSWORD}")
db.close()
