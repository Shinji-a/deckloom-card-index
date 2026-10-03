import contextlib
import hashlib
import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from scripts import build_index as b, japanese_enrichment as e, whisper_enrichment as w, previous_database as previous
from scripts.verify_japanese_cards import verify


HTML = '''<div class="whisper-cardlist-descript">Wisdom Guild</div>
<div class="card"><b><a href="http://whisper.wisdom-guild.net/card/TST001/">試験獣/Test Beast</a></b> （しけんじゅう）　(２)(緑)
<div>クリーチャー ― ビースト(Beast)　TST, コモン</div>
<p>トランプル</p><p>このクリーチャーが戦場に出たとき、カード１枚を引く。</p>
<div>3/3</div><div>Illus.Test (1/1)</div></div>'''.encode()
FACE = {'name': 'Test Beast', 'mana_cost': '{2}{G}', 'power': '3', 'toughness': '3',
        'oracle_text': 'Trample\nWhen this creature enters, draw a card.', 'type_line': 'Creature — Beast',
        'printed_name': '試験獣', 'printed_type_line': None, 'printed_text': None}
EMPTY_HTML = '''<html><body><div id="main"><h1>Test カードリスト</h1>
<div class="right"><a href="../TST.txt">テキスト形式</a></div>
<div class="whisper-cardlist-descript">このカードリストはデータベースから自動的に生成されました。</div>
</div><div id="bottom"><div id="copyrights">Wisdom Guild</div></div></body></html>'''.encode()


class WhisperTests(unittest.TestCase):
    def test_unique_complete_name_words_route_but_exact_match_wins(self):
        source = w.ORIGIN + '/cardlist/TheBrothersWarTransformersCards/'
        index = {w.set_key('TheBrothersWarTransformersCards'): source}
        self.assertEqual(w.resolve_set_url('Transformers', index), (source, 'unique_name_words'))
        for name in ('Transformer', 'War', 'Brothers Transformers'):
            self.assertIsNone(w.resolve_set_url(name, index)[0])
        other = w.ORIGIN + '/cardlist/TransformersCommander/'
        index['transformerscommander'] = other
        self.assertEqual(w.resolve_set_url('Transformers', index), (None, 'ambiguous'))
        exact = w.ORIGIN + '/cardlist/Transformers/'
        index['transformers'] = exact
        self.assertEqual(w.resolve_set_url('Transformers', index), (exact, 'exact'))

    def test_discovered_page_wrong_set_code_cannot_supply_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = w.Client(tmp, offline=True)
            pages = {w.INDEX: (''.join('<a href="/cardset/Set'+str(i)+'/">Set</a>' for i in range(10))+
                               '<a href="/cardset/SupplementTestBeastsCards/">Test</a>').encode(),
                     w.ORIGIN+'/cardlist/SupplementTestBeastsCards/': HTML}
            for url, raw in pages.items():
                page, meta = client.paths(url)
                page.write_bytes(raw)
                meta.write_text(json.dumps({'url':url,'sha256':hashlib.sha256(raw).hexdigest(),'fetched_at':time.time()}))
            db = sqlite3.connect(':memory:')
            db.execute('CREATE TABLE card_sets(oracle_id,set_code,set_name)')
            db.execute("INSERT INTO card_sets VALUES('id','wrong','Test Beasts')")
            face = {**FACE, 'printed_name':None}
            state = {'id':([face], {'applied':[], 'conflicts':[]})}
            report = {}
            w.apply(db.cursor(), {'id':{'layout':'normal'}}, state, b, e, report, client)
            self.assertIsNone(face['printed_name'])
            self.assertIsNone(face['printed_text'])
            self.assertFalse(report['whisper']['fallback_routes'][0]['verified_set_code'])
            self.assertEqual(report['whisper']['deferred_cards'], ['id'])
            self.assertEqual(client.requests, 0)
            db.close()

    def test_complete_empty_set_is_cached_without_halting_later_requests(self):
        self.assertEqual(w.parse_set(EMPTY_HTML), [])
        with tempfile.TemporaryDirectory() as tmp:
            pages = iter([EMPTY_HTML, HTML])
            client = w.Client(tmp, opener=lambda *a, **kw: io.BytesIO(next(pages)),
                              clock=lambda: 0, sleep=lambda seconds: None)
            empty_url = w.ORIGIN + '/cardlist/Empty/'
            self.assertEqual(client.get(empty_url, w.parse_set), [])
            self.assertEqual(client.get(empty_url, w.parse_set), [])
            self.assertEqual(client.requests, 1)
            self.assertTrue(client.get(w.ORIGIN + '/cardlist/Test/', w.parse_set))
            self.assertEqual(client.requests, 2)
            self.assertFalse(client.halted)
            self.assertEqual(client.errors, [])

    def test_unrecognized_or_broken_empty_pages_still_fail_closed(self):
        for raw in [EMPTY_HTML.replace(b'</html>', b''),
                    EMPTY_HTML.replace(b'id="copyrights"', b'id="missing"'),
                    EMPTY_HTML.replace(b'<h1>', b'<h2>').replace(b'</h1>', b'</h2>'),
                    EMPTY_HTML.replace(b'</h1>', b'</h1><div>Temporary error</div>'),
                    EMPTY_HTML.replace(b'</h1>', b'</h1><div class="card">broken card</div>')]:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    w.parse_set(raw)

    def test_negotiated_php_handler_can_return_validated_html(self):
        # The live server advertises application/x-httpd-php before executing it.
        # Reproduce that negotiation instead of returning HTML for every request.
        def negotiated(req, **kwargs):
            accepted = dict((part.strip().split(';')[0], part.strip())
                            for part in req.get_header('Accept', '').split(','))
            if not ({'application/x-httpd-php', 'application/*', '*/*'} & accepted.keys()):
                raise urllib.error.HTTPError(req.full_url, 406, 'Not Acceptable',
                    {'Alternates': '{"cardlist.php" 1 {type application/x-httpd-php}}'},
                    io.BytesIO(b'Available variants: cardlist.php'))
            self.assertIn('DeckLoom-CardIndex/', req.get_header('User-agent'))
            return io.BytesIO(HTML)
        with tempfile.TemporaryDirectory() as tmp:
            c = w.Client(tmp, opener=negotiated)
            records = c.get(w.ORIGIN+'/cardlist/Test/', w.parse_set)
            self.assertTrue(records)
            self.assertEqual(records[0]['heading'], '試験獣/Test Beast')
            self.assertEqual(c.requests, 1)
            self.assertEqual(c.errors, [])

    def test_http_error_preserves_diagnostic_details_and_stops_requests(self):
        def failed(req, **kwargs):
            raise urllib.error.HTTPError(req.full_url, 406, 'Not Acceptable',
                {'Content-Type': 'text/html', 'Alternates': 'application/x-httpd-php'},
                io.BytesIO(b'Available variants: cardset.php'))
        with tempfile.TemporaryDirectory() as tmp:
            c = w.Client(tmp, opener=failed)
            self.assertIsNone(c.get(w.INDEX, w.parse_index))
            self.assertIsNone(c.get(w.ORIGIN+'/cardlist/Test/', w.parse_set))
            self.assertEqual(c.requests, 1)
            self.assertEqual(c.errors[0]['status'], 406)
            self.assertIn('cardset.php', c.errors[0]['body_excerpt'])
            self.assertEqual(c.errors[0]['alternatives'], 'application/x-httpd-php')

    def test_invalid_success_response_keeps_bounded_diagnostic_and_halts(self):
        page = b'<title>Temporary unavailable</title>' + b'x' * 5000
        with tempfile.TemporaryDirectory() as tmp:
            c = w.Client(tmp, opener=lambda *a, **k: io.BytesIO(page))
            self.assertIsNone(c.get(w.ORIGIN+'/cardlist/Test/', w.parse_set))
            self.assertIsNone(c.get(w.INDEX, w.parse_index))
            self.assertEqual(c.requests, 1)
            self.assertEqual(c.errors[0]['response_bytes'], len(page))
            self.assertEqual(len(c.errors[0]['body_excerpt']), 2048)
            self.assertEqual(c.errors[0]['response_sha256'], hashlib.sha256(page).hexdigest())
            self.assertIsNone(c.cached(w.ORIGIN+'/cardlist/Test/'))

    def test_absent_rarity_does_not_discard_valid_legacy_card(self):
        raw = HTML.replace('TST, コモン'.encode(), b'TST, ')
        self.assertEqual(w.parse_set(raw), w.parse_set(HTML))
        with self.assertRaises(ValueError):
            w.parse_set(raw.replace(b'TST, ', b', '))

    def test_legacy_underscore_set_ids_parse_without_guessing_set_aliases(self):
        raw = HTML.replace(b'TST001', b'_BD001').replace(b'TST,', b'_BD,')
        records = w.parse_set(raw)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['set'], '_bd')
        self.assertTrue(records[0]['card_url'].endswith('/_BD001/'))
        self.assertEqual(w.candidates(records, {}, [FACE], {'btd'}, {}, b), [])
        self.assertEqual(len(w.candidates(records, {}, [FACE], {'_bd'}, {}, b)), 1)

    def test_parse_reading_symbols_and_complete_body(self):
        record = w.parse_set(HTML)[0]
        self.assertEqual(record['mana_cost'], '{2}{G}')
        self.assertEqual(len(record['text'].splitlines()), 2)
        self.assertEqual(w.symbols('（(２),(Ｔ)：(緑)を加える。）'), '（{2},{T}：{G}を加える。）')
        self.assertEqual(w.symbols('ビースト(Beast)'), 'ビースト(Beast)')

    def test_legacy_reading_dakuten_is_not_mistaken_for_mana(self):
        for reading in ['しけんう゛ぁー', 'しけんう\u3099ぁー', 'しけんゔぁー']:
            raw = HTML.replace('しけんじゅう'.encode(), reading.encode())
            self.assertEqual(w.parse_set(raw)[0]['mana_cost'], '{2}{G}')

    def test_english_printing_case_variants_still_require_same_words(self):
        records = w.parse_set(HTML)
        records[0]['heading'] = '試験獣/TEST BEAST'
        self.assertEqual(len(w.candidates(records, {}, [FACE], {'tst'}, {}, b)), 1)
        records[0]['heading'] = '試験獣/Test Beasts'
        self.assertEqual(w.candidates(records, {}, [FACE], {'tst'}, {}, b), [])

    def test_reject_challenge_or_incomplete_block(self):
        for raw in [b'<h1>Verify you are human</h1>', HTML.replace(b'<div>Illus.Test (1/1)</div>', b'')]:
            with self.assertRaises(ValueError):
                w.parse_set(raw)

    def test_shared_container_faces_keep_separate_rules_costs_and_stats(self):
        adventure = '''<b><a href="/card/TST001/">試験の出来事/Test Adventure</a></b> (青)
<div>インスタント ― 出来事(Adventure)　TST, コモン</div>
<p>占術１を行う。</p>'''.encode()
        multi = HTML.replace(b'<div>Illus.Test', adventure+b'<div>Illus.Test')
        records = w.parse_set(multi)
        self.assertEqual(len(records), 2)
        beast, spell = records
        self.assertEqual(beast['mana_cost'], '{2}{G}')
        self.assertIn('3/3', beast['stats'])
        self.assertNotIn('占術', beast['text'])
        self.assertEqual(spell['mana_cost'], '{U}')
        self.assertNotIn('3/3', spell['stats'])
        self.assertEqual(spell['text'], '占術１を行う。')
        faces = [dict(FACE), {'name':'Test Adventure','mana_cost':'{U}',
                 'type_line':'Instant — Adventure','oracle_text':'Scry 1.'}]
        audit = {'applied':[], 'conflicts':[]}
        e.apply_candidates(faces, w.candidates(records, {}, faces, {'tst'}, {}, b), b, audit)
        self.assertEqual(faces[1]['printed_name'], '試験の出来事')
        self.assertEqual(faces[1]['printed_text'], '占術１を行う。')
        self.assertEqual(faces[0]['printed_text'], beast['text'])
        with self.assertRaises(ValueError):
            w.parse_set(multi.replace(b'<div>Illus.Test (1/1)</div>', b''))

    def test_prepared_spell_marker_separates_body_and_binds_parent(self):
        block = ('<p>//準備//</p><p>試験の呪文/Test Spell</p><p>(青)</p>'
                 '<p>インスタント</p><p>占術１を行う。</p>').encode()
        raw = HTML.replace(b'<div>3/3</div>', block+b'<div>3/3</div>')
        records = w.parse_set(raw)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]['text'], w.parse_set(HTML)[0]['text'])
        self.assertIn('3/3', records[0]['stats'])
        self.assertEqual(records[1]['stats'], [])
        self.assertEqual(records[1]['text'], '占術１を行う。')
        self.assertEqual(records[1]['mana_cost'], '{U}')
        spell = {'name':'Test Spell','mana_cost':'{U}', 'type_line':'Instant','oracle_text':'Scry 1.'}
        candidates = w.candidates(records, {}, [FACE, spell], {'tst'}, {}, b)
        self.assertTrue(any(c['face']=='Test Spell' for c in candidates))
        self.assertEqual(w.candidates(records, {}, [spell], {'tst'}, {}, b), [])
        audit = {'applied':[], 'conflicts':[]}
        e.apply_candidates([dict(FACE), spell], candidates, b, audit)
        self.assertEqual(spell['printed_name'], '試験の呪文')
        self.assertEqual(spell['printed_text'], '占術１を行う。')

    def test_prepared_optional_marker_or_type_still_requires_cost_and_parent(self):
        parent = HTML.replace('カード１枚'.encode(), '準備済状態になり、カード１枚'.encode())
        for block in [
                '<p>試験の呪文/Test Spell</p><p>(青)</p><p>インスタント</p><p>占術１を行う。</p>',
                '<p>//準備//</p><p>試験の呪文/Test Spell</p><p>(青)</p><p>占術１を行う。</p>']:
            records = w.parse_set(parent.replace(b'<div>3/3</div>', block.encode()+b'<div>3/3</div>'))
            self.assertEqual(len(records), 2)
            self.assertNotIn('占術', records[0]['text'])
            spell = {'name':'Test Spell','mana_cost':'{U}', 'type_line':'Instant','oracle_text':'Scry 1.'}
            audit = {'applied':[], 'conflicts':[]}
            e.apply_candidates([dict(FACE), spell], w.candidates(records, {}, [FACE, spell], {'tst'}, {}, b), b, audit)
            self.assertEqual(spell['printed_text'], '占術１を行う。')
            self.assertEqual(w.candidates(records, {}, [FACE, {**spell,'mana_cost':'{R}'}], {'tst'}, {}, b)[-1]['face'], FACE['name'])

    def test_incomplete_prepared_spell_cannot_pollute_parent(self):
        block = '<p>//準備//</p><p>試験の呪文/Test Spell</p><p>(青)</p>'.encode()
        records = w.parse_set(HTML.replace(b'<div>3/3</div>',block+b'<div>3/3</div>'))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['text'], w.parse_set(HTML)[0]['text'])

    def test_identity_cost_pt_and_set_must_match(self):
        records = w.parse_set(HTML)
        self.assertEqual(len(w.candidates(records, {}, [FACE], {'tst'}, {}, b)), 1)
        for changes in [{'name': 'Another Beast'}, {'mana_cost': '{3}{G}'}, {'power': '4'}]:
            self.assertEqual(w.candidates(records, {}, [{**FACE, **changes}], {'tst'}, {}, b), [])
        self.assertEqual(w.candidates(records, {}, [FACE], {'bad'}, {}, b), [])

    def test_fully_untranslated_faces_are_eligible_without_relaxing_exclusions(self):
        self.assertTrue(w.target({'layout':'normal'}, [FACE], b, e))
        for layout in ('normal', 'transform', 'modal_dfc', 'meld', 'split', 'adventure'):
            with self.subTest(layout=layout):
                self.assertTrue(w.target({'layout':layout}, [{**FACE,'printed_name':None}], b, e))
        for layout in ('art_series', 'token', 'double_faced_token'):
            self.assertFalse(w.target({'layout':layout}, [FACE], b, e))
        complete = {**FACE, 'printed_type_line':'クリーチャー',
                    'printed_text':'トランプル\nこのクリーチャーが戦場に出たとき、カード１枚を引く。'}
        self.assertFalse(w.target({'layout':'normal'}, [complete], b, e))
        self.assertTrue(w.target({'layout':'adventure'}, [FACE, {**FACE,'name':'Other','printed_name':None}], b, e))

    def test_conflicting_variants_are_not_chosen(self):
        records = w.parse_set(HTML + HTML.replace('カード１枚'.encode(), 'カード２枚'.encode()))
        faces = [dict(FACE)]; audit = {'applied':[], 'conflicts':[]}
        e.apply_candidates(faces, w.candidates(records, {}, faces, {'tst'}, {}, b), b, audit)
        self.assertIsNone(faces[0]['printed_text'])
        self.assertTrue(audit['conflicts'])

    def test_single_lock_and_five_seconds_after_response_completion(self):
        now = [0.0]; active = [0]; maximum = [0]; starts = []; ends = []
        class Response(io.BytesIO):
            def __exit__(self, *args):
                now[0] += 2
                ends.append(now[0]); active[0] -= 1
                return super().__exit__(*args)
        def opener(*args, **kw):
            starts.append(now[0]); active[0] += 1; maximum[0] = max(maximum[0], active[0])
            time.sleep(.005)
            return Response(HTML)
        def sleep(seconds): now[0] += seconds
        with tempfile.TemporaryDirectory() as tmp:
            c = w.Client(tmp, opener=opener, clock=lambda:now[0], sleep=sleep)
            ts = [threading.Thread(target=c.get, args=(w.ORIGIN+'/cardlist/Set'+str(i)+'/',w.parse_set)) for i in range(3)]
            for t in ts:t.start()
            for t in ts:t.join()
            self.assertEqual(c.requests,3); self.assertEqual(maximum[0],1)
            self.assertTrue(all(starts[i+1]-ends[i] >= 5 for i in range(2)))

    def test_cache_repeat_is_zero_network_and_failure_preserves_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls=[]
            def opener(*args,**kw):calls.append(1);return io.BytesIO(HTML)
            c=w.Client(tmp,opener=opener)
            url=w.ORIGIN+'/cardlist/Test/'
            self.assertTrue(c.get(url,w.parse_set));self.assertTrue(c.get(url,w.parse_set))
            self.assertEqual(len(calls),1)
            def failed(*args,**kw):raise OSError('offline')
            c=w.Client(tmp,opener=failed)
            self.assertTrue(c.get(url,w.parse_set,ttl=-1));self.assertTrue(c.used[url]['stale'])
            self.assertIsNone(c.get(w.ORIGIN+'/cardlist/Other/',w.parse_set));self.assertEqual(c.requests,1)

    def test_invalid_refresh_does_not_poison_cache_and_no_retry_storm(self):
        with tempfile.TemporaryDirectory() as tmp:
            url=w.ORIGIN+'/cardlist/Test/'
            c=w.Client(tmp,opener=lambda *a,**k:io.BytesIO(HTML));c.get(url,w.parse_set)
            c=w.Client(tmp,opener=lambda *a,**k:io.BytesIO(b'robot check'))
            self.assertTrue(c.get(url,w.parse_set,ttl=-1))
            self.assertEqual(c.cached(url)[0],HTML)
            self.assertIsNone(c.get(w.ORIGIN+'/cardlist/Other/',w.parse_set))
            self.assertEqual(c.requests,1)

    def test_offline_mode_and_url_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=w.Client(tmp,offline=True,opener=lambda *a,**k:self.fail('network'))
            self.assertIsNone(c.get(w.INDEX,w.parse_index));self.assertEqual(c.requests,0)
            for url in ['http://whisper.wisdom-guild.net/cardset/',w.ORIGIN+'/card/Test/',w.ORIGIN+'/cardset/?x=1']:
                with self.assertRaises(ValueError):c.get(url,w.parse_index)
            with self.assertRaises(ValueError):w.NoRedirect().redirect_request(None,None,302,'',{},w.INDEX)

    def test_corrupt_cache_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=w.Client(tmp,opener=lambda *a,**k:io.BytesIO(HTML));url=w.ORIGIN+'/cardlist/Test/'
            c.get(url,w.parse_set);c.paths(url)[0].write_bytes(b'corrupt')
            self.assertIsNone(c.cached(url))

    def test_basic_and_vanilla_are_not_missing_text(self):
        for oracle,typ in [(None,'Creature'),('','Creature'),('({T}: Add {G}.)','Basic Land — Forest')]:
            self.assertFalse(b.japanese_text_required({'oracle_text':oracle,'type_line':typ}))
        self.assertTrue(b.japanese_text_required({'oracle_text':'Draw a card.','type_line':'Basic Land'}))

    def test_end_to_end_cached_set_fills_only_missing_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=w.Client(tmp,offline=True)
            def cache(url,data):
                p,m=c.paths(url);p.write_bytes(data);m.write_text(json.dumps({'url':url,'sha256':hashlib.sha256(data).hexdigest(),'fetched_at':time.time()}))
            cache(w.INDEX,(''.join('<a href="/cardset/Set'+str(i)+'/">Set</a>' for i in range(10))+'<a href="/cardset/Test/">Test</a>').encode())
            cache(w.ORIGIN+'/cardlist/Test/',HTML)
            db=sqlite3.connect(':memory:');db.execute('CREATE TABLE card_sets(oracle_id,set_code,set_name)');db.execute("INSERT INTO card_sets VALUES('id','tst','Test')")
            state={'id':([dict(FACE)],{'applied':[],'conflicts':[]})};report={}
            w.apply(db.cursor(),{'id':{'layout':'normal'}},state,b,e,report,c)
            self.assertIn('カード１枚',state['id'][0][0]['printed_text'])
            self.assertEqual(state['id'][0][0]['printed_name'],'試験獣')
            self.assertEqual(report['whisper']['requests'],0)
            self.assertTrue(all(x['source']['attribution']==w.ATTRIBUTION for x in state['id'][1]['applied']))

    def test_previous_source_is_retained_but_changed_rules_are_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);db=sqlite3.connect(root/'index.sqlite')
            db.execute('CREATE TABLE cards(oracle_id,english_name,layout,english_type_line,english_oracle_text,japanese_name,japanese_type_line,japanese_text,faces_json)')
            db.execute("INSERT INTO cards VALUES('id','Test Beast','normal','Creature','Trample','試験獣','クリーチャー','トランプル',NULL)")
            db.commit();db.close()
            (root/'manifest.json').write_text(json.dumps({'database_sha256':hashlib.sha256((root/'index.sqlite').read_bytes()).hexdigest(),'generated_at':'test'}))
            for oracle,expected in [('Trample','トランプル'),('Draw a card.',None)]:
                face={**FACE,'oracle_text':oracle};state={'id':([face],{'applied':[],'conflicts':[]})};report={}
                previous.retain({'id':{'layout':'normal','english_name':'Test Beast'}},state,b,e,report,tmp)
                self.assertEqual(face['printed_text'],expected)

    def test_fully_untranslated_layouts_roundtrip_sqlite_with_face_sources(self):
        for layout in ('normal', 'transform', 'split', 'adventure'):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                client = w.Client(root / 'cache', offline=True)
                first = {k:v for k,v in FACE.items() if not k.startswith('printed_')}
                second = {**first, 'name':'Test Other', 'mana_cost':'{U}',
                          'type_line':'Sorcery', 'oracle_text':'Draw two cards.',
                          'power':None, 'toughness':None}
                faces = [first] if layout == 'normal' else [first, second]
                other_html = HTML.replace(b'Test Beast', b'Test Other').replace(
                    '試験獣'.encode(), '試験呪文'.encode()).replace(
                    '(２)(緑)'.encode(), '(青)'.encode()).replace(
                    'クリーチャー ― ビースト(Beast)'.encode(), 'ソーサリー'.encode()).replace(
                    '<p>トランプル</p><p>このクリーチャーが戦場に出たとき、カード１枚を引く。</p>'.encode(),
                    '<p>カード２枚を引く。</p>'.encode()).replace(b'<div>3/3</div>', b'')
                pages = {w.INDEX: (''.join('<a href="/cardset/Set'+str(i)+'/">Set</a>' for i in range(10))+
                                  '<a href="/cardset/SupplementTestBeastsCards/">Test</a>').encode(),
                         w.ORIGIN+'/cardlist/SupplementTestBeastsCards/': HTML + (other_html if len(faces) == 2 else b'')}
                for url, raw in pages.items():
                    page, meta = client.paths(url)
                    page.write_bytes(raw)
                    meta.write_text(json.dumps({'url':url,'sha256':hashlib.sha256(raw).hexdigest(),'fetched_at':time.time()}))
                db = sqlite3.connect(root / 'index.sqlite')
                db.execute('CREATE TABLE cards(oracle_id PRIMARY KEY,english_name,layout,english_type_line,english_oracle_text,'
                           'japanese_name,japanese_type_line,japanese_text,faces_json,japanese_faces_json,mana_cost,power,toughness)')
                db.execute('CREATE TABLE card_sets(oracle_id,set_code,set_name)')
                db.execute('CREATE TABLE card_aliases(oracle_id,alias,alias_folded,lang,source)')
                db.execute('INSERT INTO cards VALUES(?,?,?,?,?,NULL,NULL,NULL,?,NULL,?,?,?)',
                           ('id',' // '.join(f['name'] for f in faces),layout,first['type_line'],first['oracle_text'],
                            json.dumps(faces) if len(faces) == 2 else None,first['mana_cost'],first['power'],first['toughness']))
                db.execute("INSERT INTO card_sets VALUES('id','tst','Test Beasts')")
                report = e.enrich(db.cursor(), b, e.Collector(b),
                                  atomic_payload={'meta':{'date':'test'},'data':{}}, whisper_client=client)
                db.commit(); db.close()
                with sqlite3.connect(root / 'index.sqlite') as saved:
                    saved.row_factory = sqlite3.Row
                    row = dict(saved.execute('SELECT * FROM cards').fetchone())
                    restored = e.row_faces(row)
                    self.assertEqual([f['name'] for f in restored], [f['name'] for f in faces])
                    self.assertEqual(e.issues(restored,b), [])
                    self.assertIn('カード１枚', restored[0]['printed_text'])
                    if len(faces) == 2:
                        self.assertEqual(restored[1]['printed_text'], 'カード２枚を引く。')
                        self.assertEqual(row['japanese_name'], '試験獣 // 試験呪文')
                    self.assertEqual(row['english_oracle_text'], first['oracle_text'])
                    sources = saved.execute('SELECT face,field,source_json FROM japanese_field_sources').fetchall()
                    self.assertEqual(len(sources), 3 * len(faces))
                    for face, field, source_json in sources:
                        source = json.loads(source_json)
                        self.assertEqual(source['kind'], 'wisdom_guild')
                        self.assertEqual(source['attribution'], w.ATTRIBUTION)
                        self.assertEqual(source['sha256'], hashlib.sha256(pages[source['url']]).hexdigest())
                self.assertEqual(report['whisper']['requests'], 0)
                self.assertTrue(report['whisper']['fallback_routes'][0]['verified_set_code'])
                self.assertEqual(report['unresolved'], [])
                checked = verify(root / 'index.sqlite', [row['english_name']], require_whisper=True)
                self.assertEqual(len(checked[0]['faces']), len(faces))
                self.assertEqual(verify(root / 'index.sqlite', [], set_code='tst'), checked)
                with self.assertRaises(ValueError):
                    verify(root / 'index.sqlite', [], set_code='absent')


if __name__ == '__main__':unittest.main()
