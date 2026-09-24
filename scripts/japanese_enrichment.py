"""Automatic, provenance-bearing Japanese enrichment. No per-card overrides or OCR."""
import copy
import hashlib
import json
import lzma
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import threading
from concurrent.futures import ThreadPoolExecutor
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

MTGJSON_URL = 'https://mtgjson.com/api/v5/AtomicCards.json.xz'
GALLERY_URL = 'https://mtg-jp.com/products/card-gallery/'
FIELDS = ('printed_name', 'printed_type_line', 'printed_text')
COLUMNS = dict(zip(FIELDS, ('japanese_name', 'japanese_type_line', 'japanese_text')))
PLACEHOLDERS = {'カードテキスト', 'カード名', 'テキスト', '日本語名', '日本語テキスト', '準備中', '不明', 'N/A', 'null'}


def normalized(value):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', value or '')).strip()


def without_reminder(text):
    text = unicodedata.normalize('NFKC', text or '')
    previous = None
    while previous != text:
        previous, text = text, re.sub(r'\([^()]*\)', '', text)
    return text.strip()


def content_issue(value, key, oracle='', helpers=None):
    if not str(value or '').strip():
        return 'missing'
    if normalized(value) in PLACEHOLDERS:
        return 'placeholder'
    if helpers:
        issue = helpers.japanese_language_issue(value, key, oracle)
        if issue:
            return issue
    if key == 'printed_text':
        ja, en = without_reminder(value), without_reminder(oracle)
        # An English rules sentence cannot be represented by katakana keywords alone.
        # This is a narrow omission detector, not a translation completeness proof.
        if (re.search(r'[.!]', en) and not re.fullmatch(r'\{T\}: Add (?:\{[WUBRGC]\})+\.', en)
                and re.fullmatch(r'[\sァ-ヺー、,・/0-9{}A-Z:+−-]+', ja or '')):
            return 'keyword_only'
    return None


class SourceClient:
    """Cache downloaded source bytes, never hand-authored card data.

    Failed refreshes are fatal: do not publish a DB silently missing a source.
    Cache content is verified against its stored hash before reuse.
    """
    def __init__(self, directory='.source-cache', ttl=7 * 86400, delay=0.3):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.ttl, self.delay, self.last_request = ttl, delay, 0
        self.sources = {}
        self.lock = threading.Lock()

    def cached(self, url):
        key = hashlib.sha256(url.encode()).hexdigest()
        meta_path = self.directory / (key + '.json')
        return meta_path.exists() and time.time() - json.loads(meta_path.read_text())['downloaded_at'] < self.ttl

    def get(self, url, max_bytes=64 * 1024 * 1024):
        key = hashlib.sha256(url.encode()).hexdigest()
        path, meta_path = self.directory / (key + '.bin'), self.directory / (key + '.json')
        if path.exists() and meta_path.exists():
            meta = json.loads(meta_path.read_text())
            data = path.read_bytes()
            if (meta['url'] == url and time.time() - meta['downloaded_at'] < (86400 if url.startswith('https://mtgjson.com/') else self.ttl)
                    and hashlib.sha256(data).hexdigest() == meta['sha256']):
                self.sources[url] = meta
                return data
        for attempt in range(3):
            with self.lock:
                time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
                self.last_request = time.monotonic()
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'DeckLoom-CardIndex/0.49 (+https://github.com/Shinji-a/deckloom-card-index)', 'Accept': '*/*'})
                with urllib.request.urlopen(req, timeout=60) as response:
                    data = response.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise ValueError('Source exceeds size limit: ' + url)
                break
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise
                time.sleep(2 ** attempt)
            except (OSError, TimeoutError):
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        meta = {'url': url, 'downloaded_at': time.time(), 'sha256': hashlib.sha256(data).hexdigest()}
        tmp = path.with_suffix('.tmp'); tmp.write_bytes(data); tmp.replace(path)
        meta_path.write_text(json.dumps(meta))
        self.sources[url] = meta
        return data


def load_atomic(client):
    raw = client.get(MTGJSON_URL)
    checksum = client.get(MTGJSON_URL + '.sha256').decode().strip().split()[0]
    if hashlib.sha256(raw).hexdigest() != checksum:
        raise ValueError('MTGJSON SHA-256 mismatch')
    payload = json.loads(lzma.decompress(raw))
    if not payload.get('meta', {}).get('date') or len(payload.get('data', {})) < 1000:
        raise ValueError('Unexpected MTGJSON payload')
    return payload, {'url': MTGJSON_URL, 'sha256': checksum, **payload['meta']}


def atomic_candidates(payload, rows):
    """Require Oracle ID, layout and English face identity. Never match fuzzy names."""
    result, rejected = defaultdict(list), Counter()
    for records in payload['data'].values():
        for record in records:
            oid = record.get('identifiers', {}).get('scryfallOracleId')
            row = rows.get(oid)
            if not row:
                continue
            faces = json.loads(row.get('faces_json') or '[]')
            name = record.get('faceName') or record.get('name')
            expected = [f['name'] for f in faces] if faces else [row['english_name']]
            if name not in expected or record.get('layout') != row['layout']:
                rejected['identity_or_layout_mismatch'] += 1
                continue
            index = expected.index(name)
            for foreign in record.get('foreignData') or []:
                if foreign.get('language') != 'Japanese':
                    continue
                jpname = foreign.get('faceName')
                if not jpname:
                    parts = (foreign.get('name') or '').split(' // ')
                    jpname = parts[index] if len(parts) == len(expected) else None
                result[oid].append({'face': name, 'fields': dict(zip(FIELDS, (jpname, foreign.get('type'), foreign.get('text')))),
                                    'source': {'kind': 'mtgjson', 'url': MTGJSON_URL,
                                               'oracle_id': oid, 'face': name, 'date': payload['meta']['date']}})
    return result, dict(rejected)


def gallery_sets(raw):
    soup = BeautifulSoup(raw, 'html.parser'); result = defaultdict(set)
    for a in soup.select('a[href]'):
        href = a['href']
        if not re.fullmatch(r'/products/card-gallery/\d+/', href):
            continue
        # Thumbnails identify the actual product; logos may be shared with a parent set.
        img = a.select_one('._thumb img')
        if img:
            match = re.search(r'/cardSet/([A-Za-z0-9]+)_', img.get('src', ''))
            if match:
                result[match[1].lower()].add(urllib.parse.urljoin(GALLERY_URL, href))
    if not result:
        raise ValueError('Official gallery index format changed')
    return {code: sorted(urls) for code, urls in result.items()}


def gallery_links(raw, base):
    soup = BeautifulSoup(raw, 'html.parser'); result = {}
    for a in soup.select('a[href]'):
        url = urllib.parse.urljoin(base, a['href'])
        m = re.fullmatch(re.escape(base) + r'(\d+)/', url)
        if m:
            result[int(m[1])] = url
    return result


def html_text(node):
    node = copy.copy(node)
    for img in node.select('img'):
        src = img.get('src', '')
        m = re.search(r'/cardImages/common/([^/.]+)\.png', src, re.I)
        if not m:
            raise ValueError('Unknown inline image')
        symbol = m[1].upper()
        symbol = {'TAP': 'T', 'UNTAP': 'Q', 'SNOW': 'S', 'WHITE': 'W', 'BLUE': 'U', 'BLACK': 'B', 'RED': 'R', 'GREEN': 'G', 'COLORLESS': 'C', 'TICKET': 'TK'}.get(symbol, symbol)
        if re.fullmatch('[WUBRG2][WUBRGP]', symbol):
            symbol = '/'.join(symbol)
        if not re.fullmatch(r'(?:\d+|[WUBRGXYZCTQSEP]|TK|CHAOS|[WUBRG2]/[WUBRGP])', symbol):
            raise ValueError('Unknown mana symbol: ' + symbol)
        img.replace_with('{' + symbol + '}')
    for br in node.select('br'):
        br.replace_with('\n')
    return '\n'.join(line.strip() for line in node.get_text().splitlines() if line.strip())


def gallery_detail(raw, helpers):
    soup = BeautifulSoup(raw, 'html.parser'); result = []
    for block in soup.select('.card-detail'):
        info = block.select_one('.card-info')
        if not info or not info.select_one('h2'):
            raise ValueError('Official gallery card structure changed')
        name = info.select_one('h2').get_text(strip=True).strip('《》')
        typ = info.select_one('p.type')
        texts = [html_text(n) for n in info.select('p.text')]
        japanese = [t for t in texts if helpers.JAPANESE_CHAR.search(t) and not content_issue(t, 'printed_text', helpers=helpers)]
        if len(japanese) > 1:
            raise ValueError('Ambiguous official Japanese text')
        fields = dict(zip(FIELDS, (name, typ.get_text(strip=True) if typ else None, japanese[0] if japanese else None)))
        fields['_english_text'] = next((t for t in texts if re.search('[A-Za-z]', t) and not helpers.JAPANESE_CHAR.search(t)), '')
        result.append(fields)
    if not result:
        raise ValueError('Official gallery has no card detail blocks')
    return result


def row_faces(row):
    canonical = json.loads(row.get('faces_json') or '[]')
    if not canonical:
        return [{'name': row['english_name'], 'type_line': row['english_type_line'],
                 'oracle_text': row['english_oracle_text'], **{key: row[col] for key, col in COLUMNS.items()}}]
    localized = json.loads(row.get('japanese_faces_json') or '[]')
    by_name = {f['name']: f for f in localized}
    faces = []
    for base in canonical:
        face = dict(base)
        face.update({key: by_name.get(base['name'], {}).get(key) for key in FIELDS})
        faces.append(face)
    # Preserve explicit root aggregates by distributing only unambiguous separators.
    for key, column in COLUMNS.items():
        root = row.get(column) or ''
        parts = root.split(' // ' if key != 'printed_text' else '\n//\n')
        if len(parts) == len(faces):
            for face, value in zip(faces, parts):
                if not face.get(key):
                    face[key] = value
    return faces


def issues(faces, helpers):
    found = []
    aggregate_adventure = any('//ADV//' in (f.get('printed_text') or '') for f in faces)
    for face in faces:
        for key in FIELDS:
            if key == 'printed_text' and (not helpers.japanese_text_required(face) or aggregate_adventure):
                # Aggregate coverage is explicitly reported separately, not proven complete.
                continue
            issue = content_issue(face.get(key), key, face.get('oracle_text'), helpers)
            if issue:
                found.append({'face': face['name'], 'field': key, 'reason': issue})
    return found


class Collector:
    """Keep source candidates and exact English printing IDs during the bulk pass."""
    def __init__(self, helpers):
        self.helpers = helpers
        self.japanese = defaultdict(list)
        self.printings = defaultdict(dict)

    def observe(self, card):
        oid = card.get('oracle_id')
        if not oid or self.helpers.is_token_object(card):
            return
        if card.get('lang') == 'en':
            identity = (card.get('set'), card.get('collector_number'))
            self.printings[oid][identity] = {'set': card['set'], 'ids': card.get('multiverse_ids') or [], 'collector_number': card.get('collector_number'),
                                            'released_at': card.get('released_at', ''), 'scryfall_id': card['id']}
        if card.get('lang') != 'ja':
            return
        faces = card.get('card_faces') or [card]
        names = self.helpers.japanese_face_names(faces)
        for index, face in enumerate(faces):
            fields = {key: face.get(key) for key in FIELDS}
            fields['printed_name'] = names[index]
            if any(value for value in fields.values()):
                self.japanese[oid].append({'face': face['name'], 'fields': fields,
                    'source': {'kind': 'scryfall_other_printing', 'scryfall_id': card['id'],
                               'url': 'https://api.scryfall.com/cards/' + card['id'],
                               'released_at': card.get('released_at', '')}})


def apply_candidates(faces, candidates, helpers, audit):
    for face in faces:
        for key in FIELDS:
            reason = content_issue(face.get(key), key, face.get('oracle_text'), helpers)
            if not reason:
                continue
            if key == 'printed_text' and any('//ADV//' in (f.get(key) or '') for f in faces):
                continue
            valid = [c for c in candidates if c['face'] == face['name'] and
                     not content_issue(c['fields'].get(key), key, face.get('oracle_text'), helpers)]
            values = defaultdict(list)
            for c in valid:
                value = c['fields'][key]
                if key == 'printed_name':
                    value = helpers.normalize_japanese_card_name(value)
                values[normalized(value)].append((value, c['source']))
            if len(values) == 1:
                value, source = next(iter(values.values()))[0]
                face[key] = value
                audit['applied'].append({'face': face['name'], 'field': key, 'previous_issue': reason,
                                         'value_sha256': hashlib.sha256(value.encode()).hexdigest(), 'source': source})
            elif len(values) > 1:
                audit['conflicts'].append({'face': face['name'], 'field': key,
                                          'candidate_count': len(values), 'sources': [c['source'] for c in valid]})


def save_row(cur, row, faces, helpers):
    if row.get('faces_json'):
        card = {'card_faces': faces}
        changes = {'japanese_faces_json': helpers.face_json(card, localized=True)}
        for key, column in COLUMNS.items():
            # Preserve valid combined Adventure text until a structural parser can verify it.
            if key == 'printed_text' and '//ADV//' in (row.get(column) or ''):
                continue
            changes[column] = {'printed_name': helpers.japanese_name, 'printed_type_line': helpers.japanese_type,
                               'printed_text': helpers.japanese_text}[key](card)
    else:
        changes = {column: helpers.usable_japanese_value(faces[0], key) for key, column in COLUMNS.items()}
        changes['japanese_name'] = helpers.normalize_japanese_card_name(changes['japanese_name'])
    helpers.update_dict(cur, 'cards', changes, 'oracle_id', row['oracle_id'])
    for alias in [f.get('printed_name') for f in faces] + [changes.get('japanese_name')]:
        if alias and helpers.JAPANESE_CHAR.search(alias):
            alias = helpers.normalize_japanese_card_name(alias)
            cur.execute('INSERT OR IGNORE INTO card_aliases (oracle_id,alias,alias_folded,lang,source) VALUES (?,?,?,?,?)',
                        (row['oracle_id'], alias, alias.casefold(), 'ja', 'automatic_source'))


def apply_gallery(rows, state, collector, client, helpers, report):
    sets = gallery_sets(client.get(GALLERY_URL))
    needed = [oid for oid in rows if issues(state[oid][0], helpers)]
    needed.sort(key=lambda oid: (
        not bool(rows[oid].get('japanese_scryfall_id')),
        not any(i['field'] in ('printed_name', 'printed_text') for i in issues(state[oid][0], helpers)),
        -int(re.sub(r'\D', '', rows[oid]['first_released_at'] or '0')), oid))
    print('Cards with remaining field gaps before official HTML:', len(needed), flush=True)
    options = {oid: sorted(collector.printings[oid].values(), key=lambda x: (x['released_at'], x['scryfall_id']), reverse=True) for oid in needed}
    bases = sorted({url for oid in needed for p in options[oid] for url in sets.get(p['set'], [])})
    print('Official gallery set indexes:', len(bases), flush=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        pages = dict(zip(bases, pool.map(client.get, bases)))
    links, names = {}, {}
    for base, raw in pages.items():
        links[base] = gallery_links(raw, base)
        names[base] = defaultdict(set)
        for anchor in BeautifulSoup(raw, 'html.parser').select('a[href]'):
            img = anchor.select_one('img[alt]')
            url = urllib.parse.urljoin(base, anchor['href'])
            if img and url in links[base].values():
                name = normalized(helpers.normalize_japanese_card_name(img['alt']))
                names[base][name].add(url)
    jobs = {}; card_jobs = defaultdict(list); spent = 0; deferred = set()
    for oid in needed:
        faces, _ = state[oid]
        known_name = helpers.usable_japanese_value(faces[0], 'printed_name')
        known_name = normalized(helpers.normalize_japanese_card_name(known_name))
        seen = set()
        for printing in options[oid]:
            for base in sets.get(printing['set'], []):
                url = links[base].get(printing['ids'][0]) if printing['ids'] else None
                match = 'multiverse_id'
                if not url and known_name:
                    found = names[base].get(known_name, set())
                    if len(found) == 1:
                        url = next(iter(found)); match = 'exact_japanese_name_and_english_rules'
                if not url or url in seen:
                    continue
                seen.add(url)
                if url not in jobs:
                    cached = getattr(client, 'cached', lambda url: False)(url)
                    if not cached and spent >= report['gallery_budget']:
                        deferred.add(oid)
                        continue
                    spent += not cached
                    jobs[url] = None
                card_jobs[oid].append((url, printing, match))
                # Bound edition probing; another edition can be retried in later runs.
                if len(card_jobs[oid]) >= 2:
                    break
            if len(card_jobs[oid]) >= 2:
                break
    print('Official detail pages:', len(jobs), 'uncached:', spent, flush=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        for index, (url, raw) in enumerate(zip(jobs, pool.map(client.get, jobs)), 1):
            try:
                jobs[url] = (gallery_detail(raw, helpers), hashlib.sha256(raw).hexdigest())
            except ValueError as exc:
                report['source_errors'].append({'url': url, 'reason': str(exc)})
            if index % 25 == 0:
                print('Official detail pages parsed:', index, '/', len(jobs), flush=True)
    for oid in needed:
        row = rows[oid]; faces, detail = state[oid]
        for url, printing, match in card_jobs[oid]:
            if not jobs[url] or not issues(faces, helpers):
                continue
            parsed, digest = jobs[url]
            if row['layout'] == 'meld' and len(faces) == 1:
                parsed = parsed[:1]
            if len(parsed) != len(faces):
                report['source_errors'].append({'url': url, 'reason': 'face_count_mismatch'})
                continue
            # Modern galleries use internal IDs, so name matches also require exact
            # English rules on every face (whitespace/punctuation normalization only).
            if match != 'multiverse_id' and any(
                    normalized(without_reminder(p['_english_text'])).replace('—', '-').replace('−', '-') !=
                    normalized(without_reminder(f.get('oracle_text'))).replace('—', '-').replace('−', '-')
                    for p, f in zip(parsed, faces)):
                report['source_errors'].append({'url': url, 'reason': 'english_rules_mismatch'})
                continue
            cs = [{'face': f['name'], 'fields': p, 'source': {
                'kind': 'mtg_jp_html', 'url': url, 'sha256': digest,
                'scryfall_id': printing['scryfall_id'], 'match': match,
                'multiverse_id': printing['ids'][0] if match == 'multiverse_id' else None,
                'face_index': i}} for i, (f, p) in enumerate(zip(faces, parsed))]
            apply_candidates(faces, cs, helpers, detail)
    report['gallery_details_checked'] = len(jobs)
    report['gallery_uncached_requests'] = spent
    report['gallery_deferred_cards'] = sorted(deferred)
    report['gallery_budget_exhausted'] = bool(deferred)


def enrich(cur, helpers, collector, client=None, atomic_payload=None, gallery=True):
    client = client or SourceClient()
    payload, atomic_source = load_atomic(client) if atomic_payload is None else (atomic_payload, {'kind': 'test_fixture'})
    columns = [x[0] for x in cur.execute('SELECT * FROM cards LIMIT 0').description]
    rows = {r[0]: dict(zip(columns, r)) for r in cur.execute('SELECT * FROM cards ORDER BY oracle_id')}
    candidates, rejected = atomic_candidates(payload, rows)
    report = {'policy': 'automatic-only-no-OCR', 'mtgjson': atomic_source, 'identity_rejections': rejected,
              'applied': [], 'conflicts': [], 'unresolved': [], 'aggregate_text_unverified': [],
              'source_errors': [], 'sources': [], 'gallery_budget': int(os.environ.get('DECKLOOM_GALLERY_BUDGET', '400'))}
    state = {}
    for oid, row in rows.items():
        faces = row_faces(row); detail = {'oracle_id': oid, 'english_name': row['english_name'], 'applied': [], 'conflicts': []}
        apply_candidates(faces, collector.japanese[oid], helpers, detail)
        apply_candidates(faces, candidates[oid], helpers, detail)
        state[oid] = (faces, detail)
    if gallery:
        apply_gallery(rows, state, collector, client, helpers, report)
    for oid, row in rows.items():
        faces, detail = state[oid]
        if detail['applied']:
            save_row(cur, row, faces, helpers)
            report['applied'].append(detail)
        if detail['conflicts']:
            report['conflicts'].append({k: v for k, v in detail.items() if k != 'applied'})
        missing = issues(faces, helpers)
        if missing:
            report['unresolved'].append({'oracle_id': oid, 'english_name': row['english_name'], 'issues': missing})
        if any('//ADV//' in (f.get('printed_text') or '') for f in faces):
            report['aggregate_text_unverified'].append({'oracle_id': oid, 'english_name': row['english_name']})
    report['sources'] = list(client.sources.values())
    report['summary'] = {'cards_supplemented': len(report['applied']), 'fields_supplemented': sum(len(x['applied']) for x in report['applied']),
                         'unresolved_cards': len(report['unresolved']), 'conflict_cards': len(report['conflicts']),
                         'aggregate_text_unverified_cards': len(report['aggregate_text_unverified']),
                         'fields_by_source': dict(Counter(f['source']['kind'] for a in report['applied'] for f in a['applied']))}
    print('Automatic Japanese enrichment:', report['summary'], flush=True)
    return report
