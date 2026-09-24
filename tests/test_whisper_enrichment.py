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


HTML = '''<div class="whisper-cardlist-descript">Wisdom Guild</div>
<div class="card"><b><a href="http://whisper.wisdom-guild.net/card/TST001/">試験獣/Test Beast</a></b> （しけんじゅう）　(２)(緑)
<div>クリーチャー ― ビースト(Beast)　TST, コモン</div>
<p>トランプル</p><p>このクリーチャーが戦場に出たとき、カード１枚を引く。</p>
<div>3/3</div><div>Illus.Test (1/1)</div></div>'''.encode()
FACE = {'name': 'Test Beast', 'mana_cost': '{2}{G}', 'power': '3', 'toughness': '3',
        'oracle_text': 'Trample\nWhen this creature enters, draw a card.', 'type_line': 'Creature — Beast',
        'printed_name': '試験獣', 'printed_type_line': None, 'printed_text': None}


class WhisperTests(unittest.TestCase):
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

    def test_parse_reading_symbols_and_complete_body(self):
        record = w.parse_set(HTML)[0]
        self.assertEqual(record['mana_cost'], '{2}{G}')
        self.assertEqual(len(record['text'].splitlines()), 2)
        self.assertEqual(w.symbols('（(２),(Ｔ)：(緑)を加える。）'), '（{2},{T}：{G}を加える。）')
        self.assertEqual(w.symbols('ビースト(Beast)'), 'ビースト(Beast)')

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

    def test_identity_cost_pt_and_set_must_match(self):
        records = w.parse_set(HTML)
        self.assertEqual(len(w.candidates(records, {}, [FACE], {'tst'}, {}, b)), 1)
        for changes in [{'name': 'Another Beast'}, {'mana_cost': '{3}{G}'}, {'power': '4'}]:
            self.assertEqual(w.candidates(records, {}, [{**FACE, **changes}], {'tst'}, {}, b), [])
        self.assertEqual(w.candidates(records, {}, [FACE], {'bad'}, {}, b), [])

    def test_missing_both_is_deferred_but_partial_face_is_eligible(self):
        self.assertTrue(w.target({'layout':'normal'}, [FACE], b, e))
        self.assertFalse(w.target({'layout':'normal'}, [{**FACE,'printed_name':None}], b, e))
        self.assertFalse(w.target({'layout':'art_series'}, [FACE], b, e))
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


if __name__ == '__main__':unittest.main()
