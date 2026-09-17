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


class JapaneseLanguageTests(unittest.TestCase):
    def test_english_text_cannot_replace_older_translation_in_either_order(self):
        old = printing('old', '2020-01-01')
        new = printing('new', '2026-01-01', printed_text='Trample')
        for cards in ([old, new], [new, old]):
            result = build(cards)
            row = result['cards'][0]
            self.assertEqual(row['japanese_scryfall_id'], 'old')
            self.assertEqual(row['japanese_text'], 'トランプル')
            self.assertEqual(row['english_oracle_text'], 'Trample')
            self.assertEqual(row['preferred_scryfall_id'], 'new')
            recovered = result['audit']['cards']['recovered_from_incomplete_printing'][0]
            self.assertEqual(recovered['preferred_printing_untranslated'],
                             [{'field': 'printed_text', 'reason': 'english_only'}])
            self.assertTrue(recovered['fully_resolved'])

    def test_unrecoverable_text_is_null_but_name_and_oracle_survive(self):
        card = printing('uthros-like', '2026-01-01', printed_type_line=None,
                        printed_text='Trample')
        result = build([card])
        row = result['cards'][0]
        self.assertEqual(row['japanese_name'], '検証用ビースト')
        self.assertIsNone(row['japanese_text'])
        self.assertEqual(row['english_oracle_text'], 'Trample')
        gap = result['audit']['cards']['incomplete'][0]
        self.assertEqual(gap['missing'], ['printed_type_line'])
        self.assertEqual(gap['untranslated'][0]['field'], 'printed_text')
        self.assertEqual(result['manifest']['japanese_coverage']['cards']['incomplete'], 1)

    def test_fully_english_ja_record_is_not_complete(self):
        result = build([printing('en-ja', '2026-01-01', printed_name='Fixture Beast',
                                 printed_type_line='Creature — Beast', printed_text='Trample')])
        row = result['cards'][0]
        for key in ['japanese_name', 'japanese_type_line', 'japanese_text']:
            self.assertIsNone(row[key])
        self.assertEqual(result['audit']['cards']['complete'], 0)
        self.assertEqual(len(result['audit']['cards']['incomplete'][0]['untranslated']), 3)

    def test_usable_text_wins_between_incomplete_printings(self):
        old = printing('old', '2020-01-01', printed_type_line=None)
        new = printing('new', '2026-01-01', printed_text='Trample')
        row = build([old, new])['cards'][0]
        self.assertEqual(row['japanese_text'], 'トランプル')
        self.assertEqual(row['japanese_scryfall_id'], 'old')
        self.assertIsNone(row['japanese_type_line'])

    def test_mixed_paragraphs_are_detected(self):
        result = build([printing('mixed', '2026-01-01', oracle_text='Flying\nTrample',
                                 printed_text='飛行\nTrample')])
        self.assertIsNone(result['cards'][0]['japanese_text'])
        self.assertEqual(result['audit']['cards']['incomplete'][0]['untranslated'][0]['reason'],
                         'english_paragraph')
        self.assertEqual(builder.japanese_language_issue('飛行\nDraw a card.', 'printed_text'),
                         'english_paragraph')

    def test_japanese_with_latin_names_and_symbols_is_usable(self):
        text = 'B.O.B.という名前のトークンを生成する。\n{T}: {G}'
        result = build([printing('legitimate', '2026-01-01', printed_text=text)])
        self.assertEqual(result['cards'][0]['japanese_text'], text)
        self.assertEqual(result['audit']['cards']['complete'], 1)
        self.assertIsNone(builder.japanese_language_issue('ﾄﾗﾝﾌﾟﾙ', 'printed_text'))

    def test_symbol_only_text_and_intrinsic_basic_land_text_are_valid(self):
        result = build([printing('symbols', '2026-01-01', printed_text='{T}: {G}',
                                 oracle_text='{T}: Add {G}.')])
        self.assertEqual(result['audit']['cards']['complete'], 1)
        basic = printing('forest', '2026-01-01', type_line='Basic Land — Forest',
                         oracle_text='{T}: Add {G}.', printed_name='森',
                         printed_type_line='基本土地 — 森', printed_text=None)
        self.assertEqual(build([basic])['audit']['cards']['complete'], 1)
        basic['oracle_text'] += '\nDraw a card.'
        self.assertEqual(build([basic])['audit']['cards']['complete'], 0)

    def test_level_ranges_shared_with_oracle_do_not_erase_japanese_rules(self):
        text = 'Lvアップ {1}\nLEVEL 6-11\n6/6\n絆魂\nLv 12+\n9/9\n絆魂、破壊不能'
        card = printing('leveler', '2026-01-01', printed_text=text,
                        oracle_text='Level up {1}\nLEVEL 6-11\nLifelink\nLEVEL 12+\nIndestructible')
        result = build([card])
        self.assertEqual(result['cards'][0]['japanese_text'], text)
        self.assertEqual(result['audit']['cards']['complete'], 1)

    def test_one_english_face_keeps_usable_translation_and_face_audit(self):
        face = dict(name='Front', type_line='Creature', oracle_text='Trample',
                    printed_name='表', printed_type_line='クリーチャー', printed_text='トランプル')
        back = dict(face, name='Back', printed_name='裏', printed_text='Trample')
        card = printing('dfc', '2026-01-01', card_faces=[face, back], layout='transform',
                        printed_name=None, printed_type_line=None, printed_text=None,
                        oracle_text=None)
        result = build([card]); row = result['cards'][0]
        self.assertEqual(row['japanese_text'], 'トランプル')
        faces = json.loads(row['japanese_faces_json'])
        self.assertEqual(faces[0]['printed_text'], 'トランプル')
        self.assertIsNone(faces[1]['printed_text'])
        self.assertEqual(result['audit']['cards']['incomplete'][0]['untranslated'][0]['field'],
                         'face[1].printed_text')
        translated = copy.deepcopy(card)
        translated.update(id='old', released_at='2020-01-01')
        translated['card_faces'][1]['printed_text'] = 'トランプル'
        self.assertEqual(build([translated, card])['cards'][0]['japanese_scryfall_id'], 'old')

    def test_adventure_partial_text_is_retained_and_reported_incomplete(self):
        front = dict(name='Front', type_line='Creature', oracle_text='Lifelink',
                     printed_name='表', printed_text='絆魂')
        back = dict(name='Adventure', type_line='Instant', oracle_text='Draw a card.')
        card = printing('adventure', '2026-01-01', layout='adventure', card_faces=[front, back],
                        printed_name=None, printed_type_line=None, printed_text=None, oracle_text=None)
        result = build([card]); row = result['cards'][0]
        self.assertEqual(row['japanese_text'], '絆魂')
        self.assertEqual(row['english_oracle_text'], 'Lifelink\n//\nDraw a card.')
        self.assertIn('face[1].printed_text', result['audit']['cards']['incomplete'][0]['missing'])
        self.assertEqual(result['audit']['cards']['complete'], 0)
        # Some source records embed both halves in the first face's text.
        front['printed_text'] = '絆魂\n//ADV//\n冒険\nカード１枚を引く。'
        self.assertEqual(build([card])['cards'][0]['japanese_text'], front['printed_text'])

    def test_root_aggregate_translation_is_not_lost_when_face_fields_are_absent(self):
        card = printing('aggregate', '2026-01-01', card_faces=[
            dict(name='Front', type_line='Creature', oracle_text='Trample'),
            dict(name='Back', type_line='Creature', oracle_text='Flying')],
            printed_name='表 // 裏', printed_text='トランプル\n//\n飛行')
        row = build([card])['cards'][0]
        self.assertEqual(row['japanese_name'], card['printed_name'])
        self.assertEqual(row['japanese_text'], card['printed_text'])

    def test_token_language_and_image_source_stay_together(self):
        old = printing('old', '2020-01-01', layout='token')
        new = printing('new', '2026-01-01', layout='token', printed_text='Trample')
        result = build([old, new])
        row = result['tokens'][0]
        self.assertEqual(row['japanese_text'], 'トランプル')
        self.assertEqual(row['japanese_image_normal'], old['image_uris']['normal'])
        self.assertTrue(result['audit']['tokens']['recovered_from_incomplete_printing'])

    def test_english_type_cannot_pollute_japanese_display_terms(self):
        result = build([printing('bad-type', '2026-01-01', printed_type_line='Creature — Beast')])
        self.assertIsNone(result['cards'][0]['japanese_type_line'])
        self.assertEqual(result['audit']['cards']['incomplete'][0]['untranslated'][0]['field'],
                         'printed_type_line')


class ReviewedOverrideTests(unittest.TestCase):
    def uthros_fixture(self):
        entry = next(iter(builder.load_japanese_overrides().values()))
        card = printing(entry['scryfall_id'], '2025-08-01', name='Uthros Scanship',
                        oracle_id=entry['oracle_id'], set=entry['set'],
                        collector_number=entry['collector_number'],
                        printed_name='ウスロスの探査船', printed_type_line=None,
                        printed_text='Draw two cards, then discard a card.',
                        oracle_text='Draw two cards, then discard a card.')
        return card, entry

    def test_reviewed_printing_recovery_is_recorded_and_keeps_oracle(self):
        card, entry = self.uthros_fixture()
        result = build([card]); row = result['cards'][0]
        self.assertEqual(row['japanese_text'], entry['fields']['printed_text'])
        self.assertEqual(row['japanese_type_line'], 'アーティファクト — 宇宙船')
        self.assertEqual(row['english_oracle_text'], card['oracle_text'])
        self.assertEqual(row['japanese_scryfall_id'], card['id'])
        observed = result['audit']['reviewed_overrides']['observed'][0]
        self.assertEqual(set(observed['applied_fields']), {'printed_text', 'printed_type_line'})
        self.assertEqual(observed['source'], entry['source'])
        self.assertEqual(result['manifest']['reviewed_japanese_overrides_applied'], 1)
        self.assertEqual(result['audit']['cards']['complete'], 1)

    def test_valid_upstream_japanese_takes_precedence(self):
        card, _ = self.uthros_fixture()
        card.update(printed_text='取得元で修正された日本語本文。', printed_type_line='アーティファクト — 宇宙船')
        result = build([card])
        self.assertEqual(result['cards'][0]['japanese_text'], card['printed_text'])
        self.assertEqual(result['audit']['reviewed_overrides']['observed'][0]['applied_fields'], [])
        self.assertEqual(result['manifest']['reviewed_japanese_overrides_applied'], 0)

    def test_override_is_bound_to_exact_printing_and_oracle(self):
        card, _ = self.uthros_fixture()
        for field, bad in [('oracle_id', 'wrong'), ('lang', 'en'), ('set', 'other'), ('collector_number', '99')]:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'identity mismatch'):
                builder.apply_japanese_override(dict(card, **{field: bad}), builder.load_japanese_overrides())
        other = dict(card, id='another-printing')
        self.assertIsNone(builder.apply_japanese_override(other, builder.load_japanese_overrides()))
        self.assertEqual(other['printed_text'], card['printed_text'])

    def test_override_loader_rejects_missing_provenance_and_english_text(self):
        entry = next(iter(builder.load_japanese_overrides().values()))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'overrides.json'
            for change in ('source', 'text', 'duplicate'):
                bad = copy.deepcopy(entry)
                if change == 'source': bad['source']['sha256'] = ''
                if change == 'text': bad['fields']['printed_text'] = 'Draw a card.'
                entries = [bad, bad] if change == 'duplicate' else [bad]
                path.write_text(json.dumps({'schema_version': 1, 'printings': entries}))
                with self.subTest(change=change), self.assertRaises(ValueError):
                    builder.load_japanese_overrides(path)


if __name__ == "__main__":
    unittest.main()
