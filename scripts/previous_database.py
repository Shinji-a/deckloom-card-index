"""Verified previous release: durable source cache and conservative field retention."""
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
from pathlib import Path


def prepare(directory='previous'):
    root = Path(directory)
    manifest = json.loads((root / 'manifest.json').read_text())
    zipped = root / manifest['database_gzip']
    if hashlib.sha256(zipped.read_bytes()).hexdigest() != manifest['database_gzip_sha256']:
        raise ValueError('Previous release gzip hash mismatch')
    data = gzip.decompress(zipped.read_bytes())
    if hashlib.sha256(data).hexdigest() != manifest['database_sha256']:
        raise ValueError('Previous release SQLite hash mismatch')
    (root / 'index.sqlite').write_bytes(data)
    cache = root / 'whisper-cache.tar.gz'
    if manifest.get('whisper_cache_sha256'):
        if not cache.exists() or hashlib.sha256(cache.read_bytes()).hexdigest() != manifest['whisper_cache_sha256']:
            raise ValueError('Previous WHISPER cache missing or hash mismatch')
        import re
        with tarfile.open(cache) as archive:
            entries = archive.getmembers()
            if sum(x.size for x in entries) > 150 * 1024 * 1024:
                raise ValueError('Source cache too large')
            for member in entries:
                if not member.isfile() or not re.fullmatch(r'[0-9a-f]{64}\.(?:html|json)', member.name):
                    raise ValueError('Invalid source cache member')
                target = Path('.whisper-cache') / member.name
                target.parent.mkdir(exist_ok=True)
                # Release snapshot is authoritative and not dependent on Actions cache eviction.
                with archive.extractfile(member) as src, target.open('wb') as dst:
                    shutil.copyfileobj(src, dst)


def retain(rows, state, helpers, e, report, directory=None):
    directory = directory or os.environ.get('DECKLOOM_PREVIOUS_DIR')
    report['previous_release'] = {'enabled': bool(directory), 'retained_fields': 0, 'changed_english_skipped': 0}
    if not directory:
        return
    root = Path(directory)
    manifest = json.loads((root / 'manifest.json').read_text())
    path = root / 'index.sqlite'
    if hashlib.sha256(path.read_bytes()).hexdigest() != manifest['database_sha256']:
        raise ValueError('Previous DB hash mismatch')
    db = sqlite3.connect(f'file:{path.resolve()}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    has_sources = db.execute("SELECT 1 FROM sqlite_master WHERE name='japanese_field_sources'").fetchone()
    sources = {(r[0], r[1], r[2]): json.loads(r[3]) for r in db.execute('SELECT * FROM japanese_field_sources')} if has_sources else {}
    legacy_sources = {}
    audit_path = root / 'japanese-enrichment.json'
    if not has_sources and audit_path.exists():
        audit = json.loads(audit_path.read_text())
        if audit.get('summary') != manifest.get('automatic_japanese_enrichment'):
            raise ValueError('Previous report does not match manifest')
        legacy_sources = {(card['oracle_id'], f['face'], f['field'], f['value_sha256']): f['source']
                          for card in audit['applied'] for f in card['applied']}
    for record in db.execute('SELECT * FROM cards'):
        old = dict(record)
        oid = old['oracle_id']
        if oid not in rows or old['layout'] != rows[oid]['layout'] or old['english_name'] != rows[oid]['english_name']:
            continue
        previous = {f['name']: f for f in e.row_faces(old)}
        faces, audit = state[oid]
        before = len(audit['applied'])
        for face in faces:
            prior = previous.get(face['name'])
            if not prior:
                continue
            fields = {}
            for key in e.FIELDS:
                if key == 'printed_text' and e.normalized(prior.get('oracle_text')) != e.normalized(face.get('oracle_text')):
                    report['previous_release']['changed_english_skipped'] += 1
                    continue
                if key == 'printed_type_line' and prior.get('type_line') != face.get('type_line'):
                    continue
                fields[key] = prior.get(key)
                original = sources.get((oid, face['name'], key))
                if not original and fields[key]:
                    original = legacy_sources.get((oid, face['name'], key, hashlib.sha256(fields[key].encode()).hexdigest()))
                source = original or {'kind': 'previous_release',
                    'url': 'https://github.com/Shinji-a/deckloom-card-index/releases/tag/card-index-latest',
                    'database_sha256': manifest['database_sha256'], 'generated_at': manifest['generated_at'],
                    'japanese_scryfall_id': old.get('japanese_scryfall_id')}
                e.apply_candidates(faces, [{'face': face['name'], 'fields': {key: fields[key]},
                                            'source': source}], helpers, audit)
        report['previous_release']['retained_fields'] += len(audit['applied']) - before
    db.close()


def package(directory='dist'):
    root = Path(directory)
    path = root / 'whisper-cache.tar.gz'
    with tarfile.open(path, 'w:gz') as archive:
        for f in sorted(Path('.whisper-cache').glob('*')):
            if f.suffix in ('.html', '.json') and f.is_file():
                archive.add(f, arcname=f.name)
    return {'whisper_cache': path.name, 'whisper_cache_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


if __name__ == '__main__':
    prepare()
