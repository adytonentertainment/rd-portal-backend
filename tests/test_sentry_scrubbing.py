"""Error reports must not carry what they travel with.

A crash in this system happens next to a writer's email, a bearer token, an
invite token, the database password and the royalty figures themselves. Error
reporting that ships those to a third party is a worse problem than the crash
it was added to catch.

These tests pin the redaction. The ones that matter are the two together: that
secrets are stripped, AND that enough is left to actually diagnose a fault —
redaction that removes the stack trace is just a slower way of having no
monitoring.
"""

from app.monitoring.sentry import _before_send, _scrub, init_sentry


def test_passwords_and_tokens_are_stripped():
    event = {
        "extra": {
            "password": "hunter2",
            "access_token": "eyJhbGciOi...",
            "SQLALCHEMY_DATABASE_URL": "postgresql://user:pw@host/db",
            "api_key": "rnd_live_123",
        }
    }
    out = _scrub(event)["extra"]
    assert out["password"] == "[redacted]"
    assert out["access_token"] == "[redacted]"
    assert out["SQLALCHEMY_DATABASE_URL"] == "[redacted]"
    assert out["api_key"] == "[redacted]"


def test_matching_is_case_insensitive_and_partial():
    """`db_password` must not slip through because the list says `password`."""
    out = _scrub({"DB_PASSWORD": "x", "userAuthToken": "y", "Cookie": "z"})
    assert set(out.values()) == {"[redacted]"}


def test_nested_structures_are_reached():
    event = {"contexts": {"a": {"b": [{"secret": "s"}, {"safe": "keep"}]}}}
    inner = _scrub(event)["contexts"]["a"]["b"]
    assert inner[0]["secret"] == "[redacted]"
    assert inner[1]["safe"] == "keep"


def test_query_string_and_body_are_dropped_entirely():
    """A reset or invite token rides in the query string, and a request body can
    carry a whole statement. The path alone locates the fault."""
    event = {
        "request": {
            "url": "https://api/portal/accept-invite",
            "query_string": "token=SECRET-INVITE-TOKEN",
            "data": {"password": "hunter2"},
            "headers": {"Authorization": "Bearer abc", "User-Agent": "curl"},
        }
    }
    req = _before_send(event, {})["request"]
    assert "query_string" not in req
    assert "data" not in req
    assert req["headers"]["Authorization"] == "[redacted]"
    # Non-sensitive headers survive, or the report is useless.
    assert req["headers"]["User-Agent"] == "curl"
    assert req["url"].endswith("/portal/accept-invite")


def test_enough_survives_to_diagnose():
    """Redaction that strips the diagnosis defeats the purpose."""
    event = {
        "exception": {"values": [{"type": "ValueError", "value": "bad period code"}]},
        "request": {"url": "https://api/admin/statements/uploads/1/failures",
                    "method": "GET"},
        "tags": {"environment": "staging"},
    }
    out = _before_send(event, {})
    assert out["exception"]["values"][0]["type"] == "ValueError"
    assert out["exception"]["values"][0]["value"] == "bad period code"
    assert out["request"]["method"] == "GET"
    assert out["tags"]["environment"] == "staging"


def test_a_scrubbing_failure_drops_the_event_rather_than_leaking_it():
    """If redaction cannot run, send nothing — failing open here would mean
    transmitting exactly the unredacted event it exists to prevent."""
    import app.monitoring.sentry as mod

    original = mod._scrub
    mod._scrub = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        assert _before_send({"request": {}}, {}) is None
    finally:
        mod._scrub = original


def test_disabled_without_a_dsn(monkeypatch):
    """Development and the Verax deployment must be unaffected."""
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    assert init_sentry() is False
