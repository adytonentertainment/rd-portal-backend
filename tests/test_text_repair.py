"""Mojibake repair for statement detail text.

Every fixture below is a REAL string taken from the Regalias Digitales corpus,
not an invented one. The corruption arrives already present in their exported
workbooks; our parser reads it faithfully and writers then see it in their
portal.

The tests that matter most are the ones asserting nothing happens. A repair
pass that rewrites already-correct text does not reduce corruption, it moves
it — 'Adiós' becomes 'Adi¢s' and 'Así' becomes 'As¡' if a file is repaired
wholesale, because round-tripping good text through the wrong codec breaks it.
"""

import pytest

from app.services.statement_ingest.text_repair import (
    LATIN1_AS_CP850,
    TITLECASED_UTF8,
    UTF8_AS_LATIN1,
    detect_encoding_fault,
    has_artifact,
    repair_rows,
    repair_text,
)


class TestDetection:
    def test_detects_each_class_from_real_titles(self):
        assert detect_encoding_fault(["El CafÃ©", "Otro"]) == UTF8_AS_LATIN1
        assert detect_encoding_fault(["Esa Niã±A", "Otro"]) == TITLECASED_UTF8
        assert detect_encoding_fault(["Adi¾s Amigo", "Otro"]) == LATIN1_AS_CP850

    def test_clean_file_is_left_alone(self):
        clean = ["Adiós Amigo", "Así Soy Yo", "El Último Viaje", "Mão"]
        assert detect_encoding_fault(clean) is None

    def test_midword_artifact_detects_cp850_without_an_unambiguous_marker(self):
        # 'La Verdad Te Extra±o' carries no '¾' or box-drawing character, so
        # strict marker matching missed it and the title stayed mangled.
        assert detect_encoding_fault(["La Verdad Te Extra±o"]) == LATIN1_AS_CP850
        assert detect_encoding_fault(["La Batalla Del Arcßngel"]) == LATIN1_AS_CP850

    def test_titlecased_pair_is_not_mistaken_for_cp850(self):
        # In 'Esa Niã±A' the '±' is the second byte of a mis-decoded UTF-8
        # pair, not a CP850 artifact. Misreading it flips the whole file to
        # the wrong class and every repair in it comes out wrong.
        assert detect_encoding_fault(["Esa Niã±A"]) == TITLECASED_UTF8
        assert detect_encoding_fault(["Mi Religiã³N"]) == TITLECASED_UTF8


class TestRepair:
    @pytest.mark.parametrize(
        "broken,fault,expected",
        [
            ("El CafÃ©", UTF8_AS_LATIN1, "El Café"),
            ("Â¿Para Que?", UTF8_AS_LATIN1, "¿Para Que?"),
            ("La Bestia En MÃ\xad", UTF8_AS_LATIN1, "La Bestia En Mí"),
            ("Esa Niã±A", TITLECASED_UTF8, "Esa Niña"),
            ("Mi Religiã³N", TITLECASED_UTF8, "Mi Religión"),
            ("Popurrã\xad Mequetrefe", TITLECASED_UTF8, "Popurrí Mequetrefe"),
            ("Vuã©Lveme A Querer", TITLECASED_UTF8, "Vuélveme A Querer"),
            ("Adi¾s Amigo", LATIN1_AS_CP850, "Adiós Amigo"),
            ("La Batalla Del Arcßngel", LATIN1_AS_CP850, "La Batalla Del Arcángel"),
            ("AsÝ Soy Yo", LATIN1_AS_CP850, "Así Soy Yo"),
            ("En Alg·n Lugar En El Cielo", LATIN1_AS_CP850, "En Algún Lugar En El Cielo"),
            ("EL GREÐAS", LATIN1_AS_CP850, "EL GREÑAS"),
            ("CORAZËN DE CRISTAL", LATIN1_AS_CP850, "CORAZÓN DE CRISTAL"),
        ],
    )
    def test_repairs_real_corrupted_titles(self, broken, fault, expected):
        assert repair_text(broken, fault) == expected

    def test_undoes_the_case_damage_from_titlecasing(self):
        # The source title-cased the BROKEN string, so the mangled pair read as
        # a word boundary and the next letter was capitalised mid-word.
        assert repair_text("Si Maã±Ana No Despierto", TITLECASED_UTF8) == "Si Mañana No Despierto"
        assert repair_text("Seã±Or Cantinero", TITLECASED_UTF8) == "Señor Cantinero"

    def test_folds_a_repaired_nonbreaking_space(self):
        # 'Â\xa0' decodes correctly to a non-breaking space, which then shows
        # as invisible indentation in the portal.
        assert repair_text("Â\xa0La Cumbia Chilanguera", UTF8_AS_LATIN1) == "La Cumbia Chilanguera"


class TestDoesNotDamageCorrectText:
    """The failure mode this module must never have."""

    @pytest.mark.parametrize(
        "already_correct",
        [
            "Adiós Amigo",          # -> 'Adi¢s' if repaired blindly
            "Así Soy Yo",           # -> 'As¡'
            "El Último Viaje",      # -> 'El éltimo Viaje'
            "El Único Obscuro",
            "Canción",
            "Mañana",
        ],
    )
    def test_correct_titles_survive_a_corrupt_file(self, already_correct):
        # These sit in the SAME statements as genuinely corrupt titles, so the
        # file-level class is right but the string must still be left alone.
        assert repair_text(already_correct, LATIN1_AS_CP850) == already_correct
        assert has_artifact(already_correct, LATIN1_AS_CP850) is False

    def test_portuguese_tilde_is_not_a_utf8_pair(self):
        # 'Mão' contains 'ã' but the next character is an ordinary letter, not
        # a UTF-8 continuation byte — it is Portuguese, not corruption.
        title = "Quando Jesus Estendeu a Sua Mão"
        assert detect_encoding_fault([title]) is None
        assert repair_text(title, TITLECASED_UTF8) == title

    def test_plain_ascii_is_never_touched(self):
        for fault in (UTF8_AS_LATIN1, TITLECASED_UTF8, LATIN1_AS_CP850):
            assert repair_text("A Mas De Cien", fault) == "A Mas De Cien"

    def test_legitimate_intercaps_survive(self):
        # The case-repair rule only fires after an accented lowercase letter,
        # so ordinary intercaps are safe.
        assert repair_text("McCartney", UTF8_AS_LATIN1) == "McCartney"

    def test_none_and_non_strings_pass_through(self):
        assert repair_text(None, UTF8_AS_LATIN1) is None
        assert repair_text("", UTF8_AS_LATIN1) == ""
        assert repair_text("x", None) == "x"


class TestRepairRows:
    def test_repairs_a_file_in_place_and_counts_changes(self):
        rows = [
            {"song_title": "Adi¾s Amigo", "income_source": "YouTube Pub"},
            {"song_title": "Adiós Amigo", "income_source": "YouTube Pub"},
            {"song_title": "La Batalla Del Arcßngel", "income_source": "YouTube Pub"},
        ]
        changed = repair_rows(rows, ["song_title", "income_source"])
        assert changed == 2
        assert rows[0]["song_title"] == "Adiós Amigo"
        assert rows[1]["song_title"] == "Adiós Amigo"  # untouched, already right
        assert rows[2]["song_title"] == "La Batalla Del Arcángel"

    def test_clean_file_changes_nothing(self):
        rows = [{"song_title": t} for t in ["Adiós Amigo", "El Último Viaje"]]
        assert repair_rows(rows, ["song_title"]) == 0
        assert rows[0]["song_title"] == "Adiós Amigo"
        assert rows[1]["song_title"] == "El Último Viaje"

    def test_is_idempotent(self):
        # The backfill must be safe to re-run: a repaired title no longer
        # carries an artifact, so a second pass is a no-op.
        rows = [{"song_title": "Adi¾s Amigo"}]
        assert repair_rows(rows, ["song_title"]) == 1
        assert repair_rows(rows, ["song_title"]) == 0
        assert rows[0]["song_title"] == "Adiós Amigo"
