import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts import build_index as builder, japanese_supplements
from test_japanese_selection import build, printing


class SupplementTests(unittest.TestCase):
    def fixture(self, name):
        return next(x for x in japanese_supplements.load() if x['english_name'].startswith(name))

    def test_secondary_name_never_becomes_front(self):
        c=printing('ja','2020-01-01',printed_name=None,card_faces=[
            {'name':'Virtue','printed_name':'Virtue'}, {'name':'Adventure','printed_name':'出来事名'}])
        self.assertEqual(builder.japanese_name(c),'Virtue // 出来事名')
        c['card_faces'][0]['printed_name']='徳目'
        self.assertEqual(builder.japanese_name(c),'徳目 // 出来事名')

    def test_split_aggregate_name_is_not_duplicated(self):
        card={'card_faces':[{'name':'Pain','printed_name':'苦痛 // 受難'},{'name':'Suffering'}]}
        self.assertEqual(builder.japanese_name(card),'苦痛 // 受難')
        self.assertEqual(json.loads(builder.face_json(card,True))[1]['printed_name'],'受難')
        card['card_faces'][1]['printed_name']='苦痛 // 受難'
        self.assertEqual(builder.japanese_name(card),'苦痛 // 受難')
        self.assertEqual(json.loads(builder.face_json(card,True))[0]['printed_name'],'苦痛')

    def test_missing_printing_is_supplemented_without_fake_printing_id(self):
        item=self.fixture('Argoth,')
        c=printing('en','2022-11-18',lang='en',name=item['english_name'],oracle_id=item['oracle_id'],layout='meld')
        result=build([c]);r=result['cards'][0]
        self.assertEqual(r['japanese_name'],'自然の聖域、アルゴス')
        self.assertIn('熊',r['japanese_text'])
        self.assertIsNone(r['japanese_scryfall_id'])
        self.assertEqual(r['preferred_scryfall_id'],'en')
        self.assertEqual(r['english_oracle_text'],'Trample')
        self.assertTrue(any(a['alias']=='自然の聖域、アルゴス' for a in result['card_aliases']))
        self.assertEqual(len(result['audit']['reviewed_card_supplements']['applied']),1)

    def test_valid_upstream_translation_wins(self):
        item=self.fixture('Argoth,')
        c=printing('ja','2022-11-18',name=item['english_name'],oracle_id=item['oracle_id'],layout='meld')
        result=build([c])
        self.assertEqual(result['cards'][0]['japanese_name'],'検証用ビースト')
        self.assertEqual(result['audit']['reviewed_card_supplements']['applied'],[])

    def test_dfc_both_faces_and_adventure_primary_name(self):
        for prefix in ['Heliod,','Virtue of Strength']:
            item=self.fixture(prefix)
            faces=[{'name':name,'type_line':'Enchantment','oracle_text':'Draw a card.'}
                   for name in item['english_name'].split(' // ')]
            c=printing('en','2023-01-01',lang='en',name=item['english_name'],oracle_id=item['oracle_id'],layout=item['layout'],card_faces=faces,oracle_text=None)
            if prefix.startswith('Virtue'):
                c['lang']='ja';faces[0].update(printed_name='Virtue of Strength',printed_text='日本語の本体本文')
                faces[1].update(printed_name='ギャレンブリグの成長',printed_text='日本語の出来事本文')
            r=build([c])['cards'][0];localized=json.loads(r['japanese_faces_json'])
            self.assertEqual([f['name'] for f in localized],[f['name'] for f in faces])
            self.assertEqual(localized[0]['printed_name'],item['faces'][0]['fields']['printed_name'])
            self.assertTrue(r['japanese_name'].startswith(localized[0]['printed_name']))
            if prefix.startswith('Heliod'): self.assertIn('瞬速',localized[1]['printed_text'])
            else: self.assertEqual(localized[1]['printed_text'],'日本語の出来事本文')

    def test_identity_mismatch_fails_closed(self):
        item=self.fixture('Argoth,')
        for wrong in [{'name':'Wrong'}, {'layout':'transform'}]:
            c=printing('en','2022-11-18',lang='en',name=item['english_name'],oracle_id=item['oracle_id'],layout='meld')
            c.update(wrong)
            with self.assertRaisesRegex(ValueError,'identity mismatch'):build([c])

    def test_bad_face_identity_fails_closed(self):
        item=self.fixture('Heliod,')
        c=printing('en','2023-01-01',lang='en',name=item['english_name'],oracle_id=item['oracle_id'],layout=item['layout'],card_faces=[{'name':'Wrong'}])
        with self.assertRaisesRegex(ValueError,'face identity mismatch'):build([c])

    def test_bad_provenance_rejected(self):
        item=copy.deepcopy(self.fixture('Argoth,'));item['faces'][0]['source']['sha256']=''
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'bad.json';p.write_text(json.dumps({'schema_version':1,'cards':[item]}))
            with self.assertRaisesRegex(ValueError,'provenance'):japanese_supplements.load(p)

    def test_meld_result_keeps_separate_identity(self):
        entries=[self.fixture(n) for n in ['Argoth,','Titania, Voice','Titania, Gaea']]
        cards=[printing(str(i),'2022-11-18',lang='en',name=e['english_name'],oracle_id=e['oracle_id'],layout='meld') for i,e in enumerate(entries)]
        result=build(cards)
        self.assertEqual(len(result['cards']),3)
        self.assertEqual(len({r['oracle_id'] for r in result['cards']}),3)
        self.assertTrue(all(r['japanese_faces_json'] is None for r in result['cards']))


class GallerySourceTests(unittest.TestCase):
    def test_gallery_name_source_requires_exact_printing_reference(self):
        entry=copy.deepcopy(next(e for e in japanese_supplements.load() if e['faces'][0]['source']['kind']=='official_card_gallery'))
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'bad.json'
            for change in ['identity','text']:
                bad=copy.deepcopy(entry)
                if change=='identity':bad['faces'][0]['source'].pop('scryfall_id')
                else:bad['faces'][0]['fields']['printed_text']='本文は画像との照合が必要'
                p.write_text(json.dumps({'schema_version':1,'cards':[bad]}))
                with self.subTest(change=change),self.assertRaises(ValueError):japanese_supplements.load(p)
