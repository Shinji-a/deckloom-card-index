"""Reviewed Japanese translations when Scryfall lacks a printing or face field.

These are translations of particular printed cards, never fabricated Scryfall
printings. Oracle identity, layout, and face names must match before applying.
"""
import json
import re
from pathlib import Path

KEYS = {'printed_name', 'printed_type_line', 'printed_text'}


def load(path=None):
    path = Path(path) if path else Path(__file__).resolve().parents[1] / 'data/japanese-card-supplements.json'
    payload = json.loads(path.read_text(encoding='utf-8'))
    if payload.get('schema_version') != 1:
        raise ValueError('Unsupported supplement schema')
    seen = set()
    for item in payload['cards']:
        oid = item['oracle_id']
        if oid in seen or not oid or not item['english_name'] or not item['layout']:
            raise ValueError('Invalid supplement identity')
        seen.add(oid)
        if not item['reviewed_at'] or not item['faces']:
            raise ValueError('Missing supplement provenance')
        names = set()
        for face in item['faces']:
            if not face['name'] or face['name'] in names:
                raise ValueError('Invalid supplement face identity')
            names.add(face['name'])
            source = face['source']
            if (source['kind'] not in {'printed_card_image', 'official_card_gallery'} or not source['url'].startswith('https://')
                    or not re.fullmatch('[0-9a-f]{64}', source['sha256'])):
                raise ValueError('Invalid supplement image provenance')
            fields = face['fields']
            if source['kind'] == 'official_card_gallery' and (
                    set(fields) != {'printed_name'} or not source['url'].startswith('https://mtg-jp.com/products/card-gallery/')
                    or not isinstance(source.get('multiverse_id'), int) or not source.get('scryfall_id')):
                raise ValueError('Gallery supplements require exact identity and name-only fields')
            if not fields or set(fields) - KEYS:
                raise ValueError('Invalid supplement fields')
            for value in fields.values():
                if not isinstance(value, str) or not re.search('[\u3040-\u30ff\u3400-\u9fff]', value):
                    raise ValueError('Supplement must contain reviewed Japanese')
    return payload['cards']


def apply(cur, helpers, entries=None):
    report = {'applied': [], 'not_observed': []}
    for item in load() if entries is None else entries:
        names = [d[0] for d in cur.execute('SELECT * FROM cards LIMIT 0').description]
        values = cur.execute('SELECT * FROM cards WHERE oracle_id=?', (item['oracle_id'],)).fetchone()
        if values is None:
            report['not_observed'].append(item['oracle_id'])
            continue
        row = dict(zip(names, values))
        if row['english_name'] != item['english_name'] or row['layout'] != item['layout']:
            raise ValueError('Supplement card identity mismatch: ' + item['oracle_id'])
        canonical = json.loads(row['faces_json'] or '[]')
        localized = json.loads(row['japanese_faces_json'] or '[]')
        if canonical:
            if localized and [f['name'] for f in localized] != [f['name'] for f in canonical]:
                raise ValueError('Supplement face order mismatch')
            faces = localized or canonical
        else:
            faces = [{'name': row['english_name'], 'oracle_text': row['english_oracle_text'],
                      'printed_name': row['japanese_name'], 'printed_type_line': row['japanese_type_line'],
                      'printed_text': row['japanese_text']}]
        applied = []
        for supplement in item['faces']:
            matches = [f for f in faces if f['name'] == supplement['name']]
            if len(matches) != 1:
                raise ValueError('Supplement face identity mismatch: ' + supplement['name'])
            face = matches[0]
            for key, value in supplement['fields'].items():
                if helpers.usable_japanese_value(face, key) is None:
                    if helpers.japanese_language_issue(value, key):
                        raise ValueError('Supplement contains untranslated content')
                    face[key] = value
                    applied.append({'face': face['name'], 'field': key, 'source': supplement['source']})
        if not applied:
            continue
        if canonical:
            card = {'card_faces': faces}
            # A real root aggregate may contain additional text not in face fields.
            # Only regenerate a column whose corresponding face field was filled.
            changes = {'japanese_faces_json': helpers.face_json(card, localized=True)}
            for key, column, fn in [('printed_name', 'japanese_name', helpers.japanese_name),
                                    ('printed_type_line', 'japanese_type_line', helpers.japanese_type),
                                    ('printed_text', 'japanese_text', helpers.japanese_text)]:
                if any(a['field'] == key for a in applied):
                    changes[column] = fn(card)
        else:
            face = faces[0]
            changes = {'japanese_name': helpers.japanese_name(face),
                       'japanese_type_line': helpers.japanese_type(face),
                       'japanese_text': helpers.japanese_text(face)}
        helpers.update_dict(cur, 'cards', changes, 'oracle_id', item['oracle_id'])
        aliases = [f.get('printed_name') for f in faces] + [changes.get('japanese_name')]
        for alias in aliases:
            if alias and helpers.JAPANESE_CHAR.search(alias):
                alias = helpers.normalize_japanese_card_name(alias)
                cur.execute('INSERT OR IGNORE INTO card_aliases (oracle_id,alias,alias_folded,lang,source) VALUES (?,?,?,?,?)',
                            (item['oracle_id'], alias, alias.casefold(), 'ja', 'reviewed_image'))
        report['applied'].append({'oracle_id': item['oracle_id'], 'english_name': item['english_name'],
                                  'reviewed_at': item['reviewed_at'], 'fields': applied})
    return report
