"""Exercise the real JSONL -> SQLite builder with small offline fixtures."""
import contextlib
import copy
import gzip
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts import build_index as builder


def printing(sid, date, lang="ja", **changes):
    card = {
        "id": sid, "oracle_id": "fixture-oracle", "name": "Fixture Beast",
        "lang": lang, "released_at": date, "games": ["paper"],
        "set": "test", "set_name": "Test", "collector_number": "1",
        "rarity": "rare", "layout": "normal", "type_line": "Creature — Beast",
        "oracle_text": "Trample", "power": "6", "toughness": "6",
        "mana_cost": "{4}{G}{G}", "cmc": 6, "colors": ["G"],
        "color_identity": ["G"], "legalities": {"commander": "legal"},
        "image_uris": {"normal": f"https://example.invalid/{sid}.jpg",
                       "small": f"https://example.invalid/{sid}-small.jpg"},
    }
    if lang == "ja":
        card.update(printed_name="検証用ビースト", printed_type_line="クリーチャー — ビースト",
                    printed_text="トランプル")
    card.update(changes)
    return card


def build(cards):
    payload = gzip.compress("\n".join(json.dumps(c) for c in cards).encode())
    with tempfile.TemporaryDirectory() as directory:
        with contextlib.chdir(directory), patch.object(builder, "request", return_value=io.BytesIO(payload)):
            with contextlib.redirect_stdout(io.StringIO()):
                builder.create_database("offline-fixture", "2026-09-17T00:00:00Z")
            with sqlite3.connect("dist/deckloom-card-index.sqlite") as db:
                db.row_factory = sqlite3.Row
                result = {t: [dict(r) for r in db.execute(f"SELECT * FROM {t}")]
                          for t in ("cards", "tokens", "card_aliases")}
                assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            result["manifest"] = json.loads(Path("dist/manifest.json").read_text())
            audit_path = Path("dist/japanese-coverage.json")
            result["audit"] = json.loads(audit_path.read_text()) if audit_path.exists() else None
            return result


class JapaneseSelectionTests(unittest.TestCase):
    def test_new_empty_printing_preserves_complete_translation_in_either_order(self):
        old = printing("old", "2020-01-01")
        new = printing("new", "2026-01-01", printed_name=None, printed_text=None,
                       printed_type_line=None)
        for cards in ([old, new], [new, old]):
            with self.subTest(order=[c["id"] for c in cards]):
                row = build(cards)["cards"][0]
                self.assertEqual(row["japanese_name"], old["printed_name"])
                self.assertEqual(row["japanese_text"], old["printed_text"])
                self.assertEqual(row["japanese_scryfall_id"], "old")
                self.assertEqual(row["preferred_scryfall_id"], "new")

    def test_complete_newer_translation_wins(self):
        row = build([printing("old", "2020-01-01"), printing("new", "2026-01-01", printed_text="新しい本文")])["cards"][0]
        self.assertEqual(row["japanese_text"], "新しい本文")
        self.assertEqual(row["japanese_scryfall_id"], "new")

    def test_partial_newer_printing_does_not_erase_text(self):
        row = build([printing("old", "2020-01-01"), printing("new", "2026-01-01", printed_text=" ")])["cards"][0]
        self.assertEqual(row["japanese_text"], "トランプル")

    def test_partial_only_remains_available(self):
        row = build([printing("partial", "2026-01-01", printed_text=None)])["cards"][0]
        self.assertEqual(row["japanese_name"], "検証用ビースト")
        self.assertIsNone(row["japanese_text"])

    def test_no_japanese_printing_stays_untranslated(self):
        row = build([printing("en", "2026-01-01", lang="en")])["cards"][0]
        self.assertIsNone(row["japanese_name"])
        self.assertIsNone(row["japanese_scryfall_id"])

    def test_vanilla_empty_text_is_valid(self):
        row = build([printing("old", "2020-01-01", oracle_text="", printed_text=None),
                     printing("new", "2026-01-01", oracle_text="", printed_text=None)])["cards"][0]
        self.assertEqual(row["japanese_scryfall_id"], "new")

    def test_complete_digital_translation_beats_incomplete_paper(self):
        row = build([printing("digital", "2020-01-01", games=["arena"]),
                     printing("paper", "2026-01-01", printed_text=None)])["cards"][0]
        self.assertEqual(row["japanese_scryfall_id"], "digital")
        self.assertEqual(row["preferred_scryfall_id"], "paper")

    def test_double_faced_card_keeps_both_faces_from_one_printing(self):
        faces = [dict(name=n, type_line="Creature — Beast", oracle_text="Trample",
                      printed_name=j, printed_type_line="クリーチャー — ビースト", printed_text="トランプル")
                 for n, j in (("Front", "表"), ("Back", "裏"))]
        old = printing("old", "2020-01-01", layout="transform", card_faces=faces,
                       printed_name=None, printed_type_line=None, printed_text=None)
        new = copy.deepcopy(old)
        new.update(id="new", released_at="2026-01-01")
        new["card_faces"][1]["printed_name"] = None
        new["card_faces"][1]["printed_text"] = None
        row = build([old, new])["cards"][0]
        self.assertEqual(row["japanese_name"], "表 // 裏")
        self.assertEqual(json.loads(row["japanese_faces_json"])[1]["printed_name"], "裏")

    def test_tokens_also_keep_translation_and_matching_japanese_image(self):
        old = printing("old", "2020-01-01", layout="token")
        new = printing("new", "2026-01-01", layout="token", printed_name=None, printed_text=None)
        row = build([old, new])["tokens"][0]
        self.assertEqual(row["japanese_name"], "検証用ビースト")
        self.assertEqual(row["japanese_scryfall_id"], "old")
        self.assertEqual(row["japanese_image_normal"], old["image_uris"]["normal"])
        self.assertEqual(row["preferred_scryfall_id"], "new")

    def test_equal_quality_preserves_paper_preference(self):
        row = build([printing("paper", "2020-01-01"), printing("digital", "2026-01-01", games=["arena"])])["cards"][0]
        self.assertEqual(row["japanese_scryfall_id"], "paper")

    def test_same_date_localization_is_independent_of_input_order(self):
        a = printing("a", "2026-01-01", printed_name="検証甲")
        b = printing("b", "2026-01-01", printed_name="検証乙")
        self.assertEqual(build([a, b])["cards"][0]["japanese_name"],
                         build([b, a])["cards"][0]["japanese_name"])

    def test_audit_separates_missing_printing_incomplete_and_recovered(self):
        result = build([
            printing("old", "2020-01-01"),
            printing("new", "2026-01-01", printed_text=None),
            printing("partial", "2026-01-01", oracle_id="partial", printed_name=None),
            printing("en", "2026-01-01", lang="en", oracle_id="english-only"),
            printing("vanilla", "2026-01-01", oracle_id="vanilla", oracle_text="", printed_text=None),
        ])
        report = result["audit"]["cards"]
        self.assertEqual(report["without_japanese_printing"], 1)
        self.assertEqual(report["complete"], 2)
        self.assertEqual(len(report["incomplete"]), 1)
        self.assertEqual(report["incomplete"][0]["missing"], ["printed_name"])
        self.assertEqual(len(report["recovered_from_incomplete_printing"]), 1)
        self.assertEqual(report["recovered_from_incomplete_printing"][0]["selected_scryfall_id"], "old")
        self.assertEqual(result["manifest"]["japanese_coverage"]["cards"]["incomplete"], 1)


if __name__ == "__main__":
    unittest.main()
