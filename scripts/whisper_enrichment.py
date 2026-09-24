"""Set-level Wisdom Guild enrichment; serial, cached, attributed and fail closed."""
import hashlib
import json
import os
import re
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import urllib.error
from collections import defaultdict
from pathlib import Path

from bs4 import BeautifulSoup

ORIGIN = 'https://whisper.wisdom-guild.net'
INDEX = ORIGIN + '/cardset/'
ATTRIBUTION = '日本語補完データは Wisdom Guild / WHISPER より転載。独自の日本語訳を含み、公式の印刷本文とは限りません。'
POLICY = 'https://www.wisdom-guild.net/welcome/'
MIN_INTERVAL = 5.0
PAGE_TTL = 30 * 86400
INDEX_TTL = 7 * 86400
MAX_REQUESTS = 50


def allowed_url(url):
    p = urllib.parse.urlsplit(url)
    return (p.scheme == 'https' and p.netloc == 'whisper.wisdom-guild.net'
            and not p.query and not p.fragment
            and (p.path == '/cardset/' or re.fullmatch(r'/cardlist/[A-Za-z0-9]+/', p.path)))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Redirects would otherwise introduce unpaced requests, possibly to another host.
        raise ValueError('WHISPER redirect deferred: ' + str(code))


class Client:
    def __init__(self, directory='.whisper-cache', offline=False, opener=None,
                 clock=time.monotonic, sleep=time.sleep):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.offline = offline
        self.open = opener or urllib.request.build_opener(NoRedirect()).open
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()
        self.last_finished = None
        self.requests = 0
        self.halted = False
        self.used = {}
        self.errors = []

    def paths(self, url):
        key = hashlib.sha256(url.encode()).hexdigest()
        return self.directory / (key + '.html'), self.directory / (key + '.json')

    def cached(self, url):
        page, meta = self.paths(url)
        if not page.exists() or not meta.exists():
            return None
        try:
            data, info = page.read_bytes(), json.loads(meta.read_text())
            if info['url'] != url or hashlib.sha256(data).hexdigest() != info['sha256']:
                return None
            return data, info
        except (OSError, ValueError, KeyError):
            return None

    def get(self, url, parser, ttl=PAGE_TTL):
        if not allowed_url(url):
            raise ValueError('Unsupported WHISPER URL')
        # Lock covers pacing, complete response consumption, validation and cache writes.
        with self.lock:
            cached = self.cached(url)
            if cached and (self.offline or time.time() - cached[1]['fetched_at'] < ttl):
                parsed = parser(cached[0])
                self.used[url] = {**cached[1], 'cache': True, 'stale': False}
                return parsed
            if not self.offline and not self.halted and self.requests < MAX_REQUESTS:
                try:
                    if self.last_finished is not None:
                        self.sleep(max(0, MIN_INTERVAL - (self.clock() - self.last_finished)))
                    req = urllib.request.Request(url, headers={
                        'User-Agent': 'DeckLoom-CardIndex/0.49 (+https://github.com/Shinji-a/deckloom-card-index)',
                        # Apache negotiates this public route as application/x-httpd-php
                        # before PHP returns HTML. HTML-only Accept causes a 406.
                        # Prefer HTML, allow the handler variant, then validate the body.
                        'Accept': 'text/html, */*;q=0.1'})
                    self.requests += 1
                    try:
                        with self.open(req, timeout=45) as response:
                            data = response.read(2 * 1024 * 1024 + 1)
                    finally:
                        self.last_finished = self.clock()
                    if len(data) > 2 * 1024 * 1024:
                        raise ValueError('WHISPER page too large')
                    parsed = parser(data)  # Never replace good cache with a challenge/error page.
                    info = {'url': url, 'sha256': hashlib.sha256(data).hexdigest(), 'fetched_at': time.time()}
                    page, meta = self.paths(url)
                    page.with_suffix('.tmp').write_bytes(data)
                    page.with_suffix('.tmp').replace(page)
                    meta.write_text(json.dumps(info))
                    self.used[url] = {**info, 'cache': False, 'stale': False}
                    return parsed
                except (OSError, ValueError) as exc:
                    error = {'url': url, 'reason': str(exc)[:250]}
                    if isinstance(exc, urllib.error.HTTPError):
                        error.update(status=exc.code,
                                     content_type=exc.headers.get('Content-Type', ''),
                                     alternatives=exc.headers.get('Alternates', '')[:1000])
                        try:
                            error['body_excerpt'] = exc.read(2048).decode('utf-8', 'replace')
                        except OSError:
                            pass
                        finally:
                            exc.close()
                    self.errors.append(error)
                    print('WHISPER stopped:', json.dumps(error, ensure_ascii=False), flush=True)
                    # No automatic retries; stop all further network calls this run on failure.
                    self.halted = True
            if cached:
                parsed = parser(cached[0])
                self.used[url] = {**cached[1], 'cache': True, 'stale': True}
                return parsed
            return None


def set_key(value):
    value = re.sub(r'[^a-z0-9]', '', unicodedata.normalize('NFKC', value or '').casefold())
    return re.sub(r'^magicthegathering', '', value)


def parse_index(raw):
    soup = BeautifulSoup(raw, 'html.parser')
    found = defaultdict(set)
    for a in soup.select('a[href]'):
        p = urllib.parse.urlsplit(urllib.parse.urljoin(INDEX, a['href']))
        m = re.fullmatch(r'/cardset/([A-Za-z0-9]+)/', p.path)
        if p.hostname == 'whisper.wisdom-guild.net' and m:
            slug = m[1]
            found[set_key(slug)].add(ORIGIN + '/cardlist/' + slug + '/')
    if len(found) < 10:
        raise ValueError('WHISPER set index missing or challenge page')
    return {key: next(iter(urls)) for key, urls in found.items() if len(urls) == 1}


def symbols(value):
    # Only convert known mana/tap symbols; English creature-type annotations stay intact.
    def convert(m):
        s = unicodedata.normalize('NFKC', m[1])
        for jp, en in [('白','W'),('青','U'),('黒','B'),('赤','R'),('緑','G'),('◇','C'),('氷','S'),('Φ','P')]:
            s = s.replace(jp, en)
        return '{' + s + '}' if re.fullmatch(r'(?:\d+|[WUBRGCXYZTSQEP]|[WUBRG2]/[WUBRGP])', s) else m[0]
    return re.sub(r'[（(]([^()（）]+)[）)]', convert, value)


def parse_set(raw):
    soup = BeautifulSoup(raw, 'html.parser')
    if not soup.select_one('.whisper-cardlist-descript'):
        raise ValueError('WHISPER card list header missing or challenge page')
    records = []
    for card in soup.select('div.card'):
        # Separate faces may be separate blocks. Never flatten nested card containers.
        if card.select_one('div.card'):
            continue
        headings = card.select('b a[href]')
        if len(headings) != 1:
            continue
        a = headings[0]
        match = re.search(r'/card/([A-Za-z0-9]+)/', a['href'])
        if not match:
            continue
        direct = card.find_all(['p', 'div'], recursive=False)
        paragraphs = [symbols(x.get_text('', strip=False).strip()) for x in direct if x.name == 'p']
        divs = [x.get_text(' ', strip=True) for x in direct if x.name == 'div']
        if not divs or not any(x.startswith('Illus.') for x in divs):
            continue  # Incomplete block cannot prove a complete rules field.
        type_match = re.fullmatch(r'(.*?)\s+([A-Z0-9]+),\s*(.+)', divs[0])
        if not type_match:
            continue
        cost_parts = []
        for node in a.parent.next_siblings:
            if getattr(node, 'name', None):
                break
            cost_parts.append(str(node))
        cost_raw = re.sub(r'^\s*（[ぁ-ゖゝゞー]+）', '', ''.join(cost_parts))
        cost = symbols(cost_raw).strip()
        if cost and not re.fullmatch(r'(?:\{[^{}]+\})+', cost):
            continue
        records.append({'heading': a.get_text('', strip=True), 'set': type_match[2].lower(),
                        'type': type_match[1], 'text': '\n'.join(p for p in paragraphs if p),
                        'mana_cost': cost, 'stats': divs[1:], 'card_url': a['href'].replace('http:', 'https:')})
    if not records:
        raise ValueError('WHISPER card list contains no validated blocks')
    return records


def target(row, faces, helpers, e):
    if row['layout'] in ('art_series', 'token', 'double_faced_token'):
        return False
    missing = e.issues(faces, helpers)
    if not any(i['field'] in ('printed_name', 'printed_text') for i in missing):
        return False
    # Whole-card name AND rules absent: deliberately deferred. Partial faces remain eligible.
    return any(helpers.usable_japanese_value(f, k) for f in faces for k in ('printed_name','printed_text'))


def candidates(records, row, faces, set_codes, source, helpers):
    result = []
    for face in faces:
        for record in records:
            suffix = '/' + face['name']
            if record['set'] not in set_codes or not record['heading'].endswith(suffix):
                continue
            jpname = record['heading'][:-len(suffix)]
            if not helpers.JAPANESE_CHAR.search(jpname) or '仮訳' in jpname:
                continue
            if (face.get('mana_cost') or '') != record['mana_cost']:
                continue
            if face.get('power') is not None and face.get('toughness') is not None:
                if str(face['power']) + '/' + str(face['toughness']) not in record['stats']:
                    continue
            result.append({'face': face['name'], 'fields': {
                'printed_name': jpname, 'printed_type_line': record['type'], 'printed_text': record['text']},
                'source': {**source, 'card_url': record['card_url'], 'set': record['set'],
                           'match': 'exact_english_face_set_mana_and_available_pt'}})
    return result


def apply(cur, rows, state, helpers, e, report, client=None):
    client = client or Client(offline=os.environ.get('DECKLOOM_WHISPER_OFFLINE') == '1')
    info = report['whisper'] = {'attribution': ATTRIBUTION, 'policy_url': POLICY,
                              'max_parallel_requests': 1, 'minimum_interval_seconds': MIN_INTERVAL,
                              'request_limit': MAX_REQUESTS, 'page_cache_days': 30,
                              'offline': client.offline, 'sets_checked': [], 'deferred_cards': []}
    pending = {oid for oid, row in rows.items() if target(row, state[oid][0], helpers, e)}
    info['eligible_cards'] = len(pending)
    if not pending:
        info.update(requests=0, sources=[], errors=[])
        return
    index = client.get(INDEX, parse_index, INDEX_TTL)
    if index is None:
        info.update(requests=client.requests, sources=list(client.used.values()), errors=client.errors,
                    deferred_cards=sorted(pending))
        return
    by_url = defaultdict(set)
    card_sets = defaultdict(set)
    for oid, code, name in cur.execute('SELECT oracle_id,set_code,set_name FROM card_sets'):
        if oid in pending:
            card_sets[oid].add(code)
            url = index.get(set_key(name))
            if url:
                by_url[url].add(oid)
    while pending and by_url:
        # Prefer cached pages, then the largest number of still-missing cards; stable tie break.
        url = min(by_url, key=lambda u: (not bool(client.cached(u)), -len(by_url[u] & pending), u))
        ids = by_url.pop(url) & pending
        if not ids:
            continue
        records = client.get(url, parse_set)
        if records is None:
            continue
        info['sets_checked'].append(url)
        meta = client.used[url]
        source = {'kind': 'wisdom_guild', 'url': url, 'sha256': meta['sha256'],
                  'fetched_at': meta['fetched_at'], 'stale_cache': meta['stale'],
                  'attribution': ATTRIBUTION, 'official_printed_text': False}
        for oid in sorted(ids):
            faces, audit = state[oid]
            e.apply_candidates(faces, candidates(records, rows[oid], faces, card_sets[oid], source, helpers), helpers, audit)
            if not target(rows[oid], faces, helpers, e):
                pending.remove(oid)
        print('WHISPER sets:', len(info['sets_checked']), 'requests:', client.requests,
              'remaining candidates:', len(pending), flush=True)
    info.update(requests=client.requests, sources=list(client.used.values()), errors=client.errors,
                deferred_cards=sorted(pending))
