import copy
import contextlib
import gzip
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts import build_index as b, japanese_enrichment as e
from test_japanese_selection import printing


def generate(cards, atomic=None, client=None, gallery=False):
    raw = gzip.compress('\n'.join(json.dumps(c) for c in cards).encode())
    with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
        with patch.object(b, 'request', return_value=io.BytesIO(raw)), contextlib.redirect_stdout(io.StringIO()):
            b.create_database('fixture', '2026-09-24', {'atomic_payload': atomic or {'meta': {'date': '2026-09-24'}, 'data': {}},
                                                      'gallery': gallery, 'client': client, 'whisper': False})
        conn = sqlite3.connect('dist/deckloom-card-index.sqlite'); conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute('SELECT * FROM cards')]; conn.close()
        return rows, json.loads(Path('dist/japanese-enrichment.json').read_text())


def atomic(card, **foreign):
    return {'meta': {'date': '2026-09-24'}, 'data': {card['name']: [{
        'identifiers': {'scryfallOracleId': card['oracle_id']}, 'name': card['name'],
        'layout': card['layout'], 'foreignData': [{'language': 'Japanese', **foreign}]}]}}


class AutomaticEnrichmentTests(unittest.TestCase):
    def test_atomic_fills_missing_fields_and_preserves_english_images_and_identity(self):
        card = printing('english', '2026-01-01', lang='en')
        rows, report = generate([card], atomic(card, name='検証用ビースト', type='クリーチャー', text='トランプル'))
        self.assertEqual(rows[0]['japanese_text'], 'トランプル')
        self.assertEqual(rows[0]['japanese_name'], '検証用ビースト')
        self.assertIsNone(rows[0]['japanese_scryfall_id'])
        self.assertEqual(rows[0]['preferred_scryfall_id'], 'english')
        self.assertEqual(rows[0]['english_oracle_text'], card['oracle_text'])
        self.assertEqual(report['summary']['fields_by_source'], {'mtgjson': 3})

    def test_japanese_label_does_not_make_english_text_usable(self):
        card = printing('english', '2026-01-01', lang='en')
        rows, report = generate([card], atomic(card, name='日本語名', text='Trample'))
        self.assertIsNone(rows[0]['japanese_text'])
        self.assertIsNone(rows[0]['japanese_name'])  # Explicit placeholder.
        self.assertEqual(len(report['unresolved']), 1)

    def test_placeholder_and_keyword_only_rules_are_not_complete(self):
        for text in ['カードテキスト', 'トランプル']:
            card = printing('partial', '2026-01-01', printed_text=text,
                            oracle_text='Trample\nLandfall — Whenever a land enters, draw a card.')
            rows, report = generate([card])
            self.assertIsNone(rows[0]['japanese_text'])
            self.assertTrue(report['unresolved'])

    def test_conflicting_translations_are_not_arbitrarily_chosen(self):
        card = printing('english', '2026-01-01', lang='en')
        payload = atomic(card, name='甲', text='トランプル')
        payload['data'][card['name']][0]['foreignData'].append({'language': 'Japanese', 'name': '乙'})
        rows, report = generate([card], payload)
        self.assertIsNone(rows[0]['japanese_name'])
        self.assertEqual(rows[0]['japanese_text'], 'トランプル')
        self.assertEqual(report['summary']['conflict_cards'], 1)

    def test_existing_valid_text_wins_over_other_source(self):
        card = printing('japanese', '2026-01-01')
        rows, report = generate([card], atomic(card, name='別名', text='異なる本文。'))
        self.assertEqual(rows[0]['japanese_text'], card['printed_text'])
        self.assertEqual(report['summary']['cards_supplemented'], 0)

    def test_oracle_id_name_and_layout_must_match(self):
        card = printing('english', '2026-01-01', lang='en')
        for field, value in [('name','Another Card'), ('layout','adventure'), ('identifiers',{'scryfallOracleId':'wrong'})]:
            payload = atomic(card, name='検証用', text='トランプル')
            payload['data'][card['name']][0][field] = value
            with self.subTest(field=field):
                rows, report = generate([card], payload)
                self.assertIsNone(rows[0]['japanese_name'])

    def test_adventure_face_name_is_never_promoted(self):
        card = printing('adv', '2026-01-01', lang='en', layout='adventure', name='Front // Spell',
                        card_faces=[{'name': 'Front', 'type_line':'Creature', 'oracle_text':'Trample'},
                                    {'name':'Spell','type_line':'Sorcery','oracle_text':'Draw a card.'}])
        payload = {'meta':{'date':'2026-09-24'}, 'data':{'Front // Spell':[
            {'identifiers':{'scryfallOracleId':card['oracle_id']},'name':card['name'],'faceName':'Spell','layout':'adventure',
             'foreignData':[{'language':'Japanese','name':'Front // 呪文','faceName':'呪文','text':'カード１枚を引く。'}]}]}}
        rows, _ = generate([card], payload)
        self.assertEqual(rows[0]['japanese_name'],'Front // 呪文')
        fs=json.loads(rows[0]['japanese_faces_json'])
        self.assertIsNone(fs[0]['printed_name']); self.assertEqual(fs[1]['printed_text'],'カード１枚を引く。')

    def test_aggregate_adventure_text_survives_name_enrichment(self):
        card = printing('adv', '2026-01-01', layout='adventure', name='Front // Spell', printed_name=None,
                        card_faces=[{'name':'Front','type_line':'Creature','oracle_text':'Trample','printed_text':'トランプル\n//ADV//\n呪文\nカード１枚を引く。'},
                                    {'name':'Spell','type_line':'Sorcery','oracle_text':'Draw a card.'}],
                        printed_text=None, printed_type_line=None)
        payload = {'meta':{'date':'2026-09-24'},'data':{card['name']:[
            {'identifiers':{'scryfallOracleId':card['oracle_id']},'name':card['name'],'faceName':'Front','layout':'adventure',
             'foreignData':[{'language':'Japanese','faceName':'本体'}]}]}}
        rows, report = generate([card], payload)
        self.assertIn('//ADV//',rows[0]['japanese_text'])
        self.assertEqual(len(report['aggregate_text_unverified']),1)

    def test_complete_translation_does_not_depend_on_input_order(self):
        cards=[printing('a','2026-01-01',printed_text=None),printing('b','2026-01-01',printed_type_line=None)]
        left,_=generate(cards);right,_=generate(cards[::-1])
        for column in e.COLUMNS.values():
            self.assertEqual(left[0][column],right[0][column])


class GalleryParserTests(unittest.TestCase):
    def test_set_discovery_uses_thumbnail_not_shared_parent_logo(self):
        raw='<a href="/products/card-gallery/123/"><div class="_thumb"><img src="/img_sys/cardSet/ABC_317.jpg"></div><div class="_logo"><img src="/img_sys/cardSet/XYZ_Logo.png"></div></a>'
        self.assertEqual(e.gallery_sets(raw),{'abc':[e.GALLERY_URL+'123/']})

    def test_detail_extracts_only_rules_and_preserves_mana_symbols(self):
        raw='''<div class="card-detail"><div class="card-info"><h2>《検証カード》</h2><p class="type">土地</p>
        <p class="text"><img src="/img_sys/cardImages/common/tap.png" alt="Tap">：<img src="/img_sys/cardImages/common/G.png" alt="Green">を加える。<br>カード１枚を引く。</p>
        <p class="text">Draw a card.</p></div></div><p class="notice">カードテキストは印刷カードのテキストをもとにしています。</p>'''
        fields=e.gallery_detail(raw,b)[0]
        self.assertEqual(fields['printed_text'],'{T}：{G}を加える。\nカード１枚を引く。')
        self.assertEqual(fields['printed_name'],'検証カード')

    def test_unknown_inline_image_fails_closed(self):
        with self.assertRaises(ValueError):
            e.html_text(e.BeautifulSoup('<p><img src="/other.png">本文</p>','html.parser').p)

    def test_exact_linked_multiverse_id_and_meld_component(self):
        card=printing('en','2026-01-01',lang='en',layout='meld',multiverse_ids=[42],set='abc')
        base=e.GALLERY_URL+'123/'
        fields='<div class="card-detail"><div class="card-info"><h2>《構成カード》</h2><p class="type">クリーチャー</p><p class="text">トランプル</p><p class="text">Trample</p></div></div>'
        class Client:
            sources={}
            def get(self,url):
                return {e.GALLERY_URL:b'<a href="/products/card-gallery/123/"><div class="_thumb"><img src="/cardSet/ABC_317.jpg"></div></a>',
                        base:b'<a href="42/">card</a>', base+'42/':(fields+fields.replace('構成カード','合体結果').replace('トランプル','飛行').replace('Trample','Flying')).encode()}[url]
        rows,report=generate([card],client=Client(),gallery=True)
        self.assertEqual(rows[0]['japanese_name'],'構成カード')
        self.assertEqual(report['applied'][0]['applied'][0]['source']['multiverse_id'],42)
        card['oracle_text']='Flying'
        rows,report=generate([card],client=Client(),gallery=True)
        self.assertEqual(rows[0]['japanese_name'],'合体結果')
        self.assertEqual(rows[0]['japanese_text'],'飛行')

    def test_network_failure_does_not_return_expired_cached_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = e.SourceClient(tmp, ttl=0, delay=0)
            url = 'https://example.invalid/source'
            key = hashlib.sha256(url.encode()).hexdigest()
            raw = b'old good source'
            (Path(tmp)/(key+'.bin')).write_bytes(raw)
            (Path(tmp)/(key+'.json')).write_text(json.dumps({'url':url,'downloaded_at':0,'sha256':hashlib.sha256(raw).hexdigest()}))
            with patch.object(e.urllib.request, 'urlopen', side_effect=OSError('source unavailable')), patch.object(e.time,'sleep'):
                with self.assertRaisesRegex(OSError,'source unavailable'):
                    client.get(url)

    def test_download_checksum_mismatch_stops_generation(self):
        class Client:
            def get(self,url):return b'wrong' if url.endswith('.xz') else b'0'*64
        with self.assertRaisesRegex(ValueError,'SHA-256'):
            e.load_atomic(Client())


if __name__ == '__main__':
    unittest.main()
