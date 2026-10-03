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
                data = None
                response_info = {}
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
                            response_info = {'status': getattr(response, 'status', None),
                                             'content_type': getattr(response, 'headers', {}).get('Content-Type', '')}
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
                    if data is not None:
                        error.update(response_info, response_bytes=len(data),
                                     response_sha256=hashlib.sha256(data).hexdigest(),
                                     body_excerpt=data[:2048].decode('utf-8', 'replace'))
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


def set_words(value):
    value = unicodedata.normalize('NFKC', value or '')
    value = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', value)
    value = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', value)
    return re.findall(r'[a-z0-9]+', value.casefold())


def resolve_set_url(name, index):
    exact = index.get(set_key(name))
    if exact:
        return exact, 'exact'
    words = set_words(name)
    # Discover a candidate only from an existing index link. A unique complete
    # word sequence handles shorter source names, never arbitrary substrings.
    # Short/generic and ambiguous names remain unresolved. This is routing only:
    # the page's set code and every card's identity must still match below.
    if not words or len(''.join(words)) < 6:
        return None, 'unmapped'
    urls = set()
    for url in index.values():
        slug = urllib.parse.urlsplit(url).path.strip('/').split('/')[-1]
        source_words = set_words(slug)
        if any(source_words[i:i + len(words)] == words
               for i in range(len(source_words) - len(words) + 1)):
            urls.add(url)
    if len(urls) == 1:
        return next(iter(urls)), 'unique_name_words'
    return None, 'ambiguous' if urls else 'unmapped'


def english_face_key(value):
    return unicodedata.normalize('NFKC', value).casefold()


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
    # Some published set indexes link to a complete, generated list with zero
    # cards. Accept only the observed empty-list template, not an arbitrary
    # page from which no card blocks could be parsed.
    if not soup.select('div.card'):
        main = soup.select_one('#main')
        heading = main.find('h1', recursive=False) if main else None
        desc = main.select_one('.whisper-cardlist-descript') if main else None
        toolbar = main.select_one('div.right') if main else None
        children = main.find_all(recursive=False) if main else []
        complete = re.search(rb'</body>\s*</html>\s*$', raw, re.I)
        if (heading and heading.get_text(strip=True).endswith('カードリスト')
                and desc and '自動的に生成されました' in desc.get_text()
                and toolbar and any(re.fullmatch(r'\.\./[A-Z0-9]+\.txt', a.get('href', ''))
                                    for a in toolbar.select('a[href]'))
                and len(children) == 3 and all(x in (heading, toolbar, desc) for x in children)
                and soup.select_one('#bottom #copyrights') and complete):
            return []
        raise ValueError('WHISPER empty card list is incomplete or unrecognized')
    records = []
    for card in soup.select('div.card'):
        # Separate faces may be separate blocks. Never flatten nested card containers.
        if card.select_one('div.card'):
            continue
        headings = card.select('b a[href]')
        if not headings or any(a.parent.name != 'b' or a.parent.parent is not card for a in headings):
            continue
        footer = card.find_all('div', recursive=False)
        if not any(x.get_text(' ', strip=True).startswith('Illus.') for x in footer):
            continue
        # Adventure/split/transform faces can share a card container and footer.
        # Bound each face at the next direct heading; never mix rules or P/T.
        for a in headings:
            match = re.search(r'/card/(_?[A-Za-z0-9]+)/', a['href'])
            if not match:
                continue
            direct = []
            cost_parts = []
            before_type = True
            for node in a.parent.next_siblings:
                name = getattr(node, 'name', None)
                if name == 'b' and node.select_one('a[href]'):
                    break
                if name:
                    before_type = False
                    if name in ('p', 'div'):
                        direct.append(node)
                elif before_type:
                    cost_parts.append(str(node))
            paragraphs = [symbols(x.get_text('', strip=False).strip()) for x in direct if x.name == 'p']
            divs = [x.get_text(' ', strip=True) for x in direct if x.name == 'div']
            if not divs:
                continue
            type_match = re.fullmatch(r'(.*?)\s+(_?[A-Z0-9]+),\s*(.*)', divs[0])
            if not type_match:
                continue
            # Older readings use spacing dakuten, e.g. う゛, not only composed ゔ.
            cost_raw = re.sub(r'^\s*（[ぁ-ゟー]+）', '', ''.join(cost_parts))
            cost = symbols(cost_raw).strip()
            if cost and not re.fullmatch(r'(?:\{[^{}]+\})+', cost):
                continue
            record = {'heading': a.get_text('', strip=True), 'set': type_match[2].lower(),
                      'type': type_match[1], 'text': '\n'.join(p for p in paragraphs if p),
                      'mana_cost': cost, 'stats': divs[1:], 'card_url': a['href'].replace('http:', 'https:')}
            spell_type = r'(?:インスタント|ソーサリー)(?:\s*[—―].+)?'
            markers = [i for i, p in enumerate(paragraphs) if p == '//準備//']
            # Some source blocks omit the marker; require a complete bilingual
            # heading/cost/type sequence and prepared rules in the parent.
            implicit = [i for i in range(1, len(paragraphs) - 3)
                        if '/' in paragraphs[i]
                        and re.fullmatch(r'(?:\{[^{}]+\})+', paragraphs[i + 1])
                        and re.fullmatch(spell_type, paragraphs[i + 2])
                        and any('準備' in p for p in paragraphs[:i])]
            boundaries = markers or implicit
            if boundaries:
                boundary = boundaries[0]
                record['text'] = '\n'.join(p for p in paragraphs[:boundary] if p)
                records.append(record)
                spell = paragraphs[boundary + (1 if markers else 0):]
                if (len(boundaries) == 1 and len(spell) >= 3 and '/' in spell[0]
                        and re.fullmatch(r'(?:\{[^{}]+\})+', spell[1])):
                    has_type = bool(re.fullmatch(spell_type, spell[2]))
                    body = spell[3:] if has_type else spell[2:]
                    if body:
                        records.append({**record, 'heading': spell[0], 'mana_cost': spell[1],
                                        'type': spell[2] if has_type else None,
                                        'text': '\n'.join(p for p in body if p),
                                        'stats': [], 'parent_heading': record['heading']})
            else:
                records.append(record)
    if not records:
        raise ValueError('WHISPER card list contains no validated blocks')
    return records


def target(row, faces, helpers, e):
    if row['layout'] in ('art_series', 'token', 'double_faced_token'):
        return False
    missing = e.issues(faces, helpers)
    if not any(i['field'] in ('printed_name', 'printed_text') for i in missing):
        return False
    # Eligibility must not depend on already having Japanese: identity is checked
    # against the canonical English faces, set, mana cost and available P/T below.
    # Each accepted field is still validated independently by apply_candidates.
    return True


def candidates(records, row, faces, set_codes, source, helpers):
    result = []
    parent_names = {english_face_key(face['name']) for face in faces}
    for face in faces:
        expected_name = english_face_key(face['name'])
        for record in records:
            if record.get('parent_heading') and not re.match(r'^(Instant|Sorcery)(?:$| —)', face.get('type_line') or ''):
                continue
            if (record.get('parent_heading') and
                    english_face_key(record['parent_heading'].rpartition('/')[2]) not in parent_names):
                continue
            jpname, separator, english = record['heading'].rpartition('/')
            # Historical printings use AEther where canonical Oracle uses Aether.
            # Compare case-insensitively; do not fuzzy-match different words.
            if (record['set'] not in set_codes or not separator
                    or english_face_key(english) != expected_name):
                continue
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
                           'match': 'casefold_english_face_exact_set_mana_and_available_pt'}})
    return result


def apply(cur, rows, state, helpers, e, report, client=None):
    client = client or Client(offline=os.environ.get('DECKLOOM_WHISPER_OFFLINE') == '1')
    info = report['whisper'] = {'attribution': ATTRIBUTION, 'policy_url': POLICY,
                              'eligibility_policy': 'missing-name-or-rules-including-fully-untranslated',
                              'set_route_policy': 'exact-or-unique-name-words-with-verified-set-code',
                              'fallback_routes': [], 'unmapped_sets': [],
                              'max_parallel_requests': 1, 'minimum_interval_seconds': MIN_INTERVAL,
                              'request_limit': MAX_REQUESTS, 'page_cache_days': 30,
                              'offline': client.offline, 'sets_checked': [], 'empty_sets': [], 'deferred_cards': []}
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
    fallback_routes = {}
    unmapped_sets = set()
    for oid, code, name in cur.execute('SELECT oracle_id,set_code,set_name FROM card_sets'):
        if oid in pending:
            card_sets[oid].add(code)
            url, match = resolve_set_url(name, index)
            if url:
                by_url[url].add(oid)
                if match != 'exact':
                    fallback_routes[(code, name, url)] = {
                        'set_code': code, 'set_name': name, 'url': url,
                        'match': match, 'verified_set_code': False}
            else:
                unmapped_sets.add((code, name, match))
    info['fallback_routes'] = [fallback_routes[k] for k in sorted(fallback_routes)]
    info['unmapped_sets'] = [{'set_code': code, 'set_name': name, 'reason': match}
                             for code, name, match in sorted(unmapped_sets)]
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
        parsed_codes = {r['set'] for r in records}
        for route in info['fallback_routes']:
            if route['url'] == url:
                route['verified_set_code'] = route['set_code'] in parsed_codes
        if not records:
            info['empty_sets'].append(url)
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
