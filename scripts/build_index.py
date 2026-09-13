import gzip
import hashlib
import io
import json
import os
import re
import sqlite3
import urllib.request
from datetime import datetime, timezone

API_URL = "https://api.scryfall.com/bulk-data"
HEADERS = {
    "User-Agent": "DeckLoom-CardIndex/0.3",
    "Accept": "application/json;q=0.9,*/*;q=0.8",
}

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20,
}

LEGALITY_COLUMNS = [
    "standard", "pioneer", "modern", "legacy", "pauper", "vintage",
    "commander", "oathbreaker", "brawl", "historic", "timeless",
    "duel", "premodern", "predh",
]


def request(url):
    req = urllib.request.Request(url, headers=HEADERS)
    return urllib.request.urlopen(req, timeout=180)


def get_all_cards_info():
    with request(API_URL) as response:
        data = json.load(response)
    for item in data["data"]:
        if item["type"] == "all_cards":
            return item
    raise RuntimeError("Scryfall all_cards bulk data not found")


def join_faces(card, key, separator=" // "):
    value = card.get(key)
    if value not in (None, "", []):
        if isinstance(value, list):
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return str(value)

    values = []
    for face in card.get("card_faces") or []:
        face_value = face.get(key)
        if face_value not in (None, "", []):
            if isinstance(face_value, list):
                face_value = "".join(face_value)
            values.append(str(face_value))
    return separator.join(values) if values else None


def english_oracle_text(card):
    return join_faces(card, "oracle_text", "\n//\n")


def japanese_name(card):
    return join_faces(card, "printed_name", " // ")


def japanese_type(card):
    return join_faces(card, "printed_type_line", " // ")


def japanese_text(card):
    return join_faces(card, "printed_text", "\n//\n")


def face_json(card, localized=False):
    faces = card.get("card_faces") or []
    if not faces:
        return None

    result = []
    for face in faces:
        item = {
            "name": face.get("name"),
            "type_line": face.get("type_line"),
            "oracle_text": face.get("oracle_text"),
            "mana_cost": face.get("mana_cost"),
            "colors": face.get("colors"),
            "power": face.get("power"),
            "toughness": face.get("toughness"),
            "loyalty": face.get("loyalty"),
            "defense": face.get("defense"),
        }
        if localized:
            item.update({
                "printed_name": face.get("printed_name"),
                "printed_type_line": face.get("printed_type_line"),
                "printed_text": face.get("printed_text"),
            })
        result.append(item)

    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def related_parts_json(card):
    parts = card.get("all_parts") or []
    if not parts:
        return None
    slim = [
        {
            "id": p.get("id"),
            "component": p.get("component"),
            "name": p.get("name"),
            "type_line": p.get("type_line"),
            "uri": p.get("uri"),
        }
        for p in parts
    ]
    return json.dumps(slim, ensure_ascii=False, separators=(",", ":"))


def color_string(values):
    order = "WUBRG"
    values = set(values or [])
    return "".join(c for c in order if c in values)


def card_colors(card):
    colors = card.get("colors")
    if colors is not None:
        return color_string(colors)
    combined = set()
    for face in card.get("card_faces") or []:
        combined.update(face.get("colors") or [])
    return color_string(combined)


def card_copy_limit(card, oracle_text):
    type_line = card.get("type_line") or ""
    if "Basic" in type_line and "Land" in type_line:
        return -1, "basic_land"

    text = (oracle_text or "").lower()
    if "a deck can have any number of cards named" in text:
        return -1, "any_number"

    match = re.search(r"a deck can have up to ([a-z0-9-]+) cards named", text)
    if match:
        raw = match.group(1)
        if raw.isdigit():
            return int(raw), "up_to_n"
        if raw in NUMBER_WORDS:
            return NUMBER_WORDS[raw], "up_to_n"

    return 1, "singleton"


def preferred_score(card):
    lang = card.get("lang")
    games = set(card.get("games") or [])
    paper = "paper" in games
    if lang == "ja" and paper:
        return 60
    if lang == "ja":
        return 50
    if lang == "en" and paper:
        return 40
    if lang == "en":
        return 30
    if paper:
        return 20
    return 10


def japanese_score(card):
    if card.get("lang") != "ja":
        return 0
    return 20 if "paper" in set(card.get("games") or []) else 10


def should_replace(new_score, new_date, old_score, old_date):
    if new_score > old_score:
        return True
    if new_score < old_score:
        return False
    return (new_date or "") > (old_date or "")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def create_database(download_uri, bulk_updated_at):
    os.makedirs("dist", exist_ok=True)
    db_path = "dist/deckloom-card-index.sqlite"
    gz_path = db_path + ".gz"

    for path in (db_path, gz_path, "dist/manifest.json"):
        if os.path.exists(path):
            os.remove(path)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript("""
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=MEMORY;

        CREATE TABLE cards (
            oracle_id TEXT PRIMARY KEY,
            preferred_scryfall_id TEXT NOT NULL,
            preferred_lang TEXT,
            preferred_set_code TEXT,
            preferred_collector_number TEXT,
            preferred_released_at TEXT,
            preferred_score INTEGER NOT NULL DEFAULT 0,

            english_name TEXT NOT NULL,
            japanese_name TEXT,
            english_type_line TEXT,
            japanese_type_line TEXT,
            english_oracle_text TEXT,
            japanese_text TEXT,

            mana_cost TEXT,
            mana_value REAL,
            colors TEXT,
            color_identity TEXT,
            power TEXT,
            toughness TEXT,
            loyalty TEXT,
            defense TEXT,
            produced_mana TEXT,
            layout TEXT,
            keywords_json TEXT,
            legalities_json TEXT,
            faces_json TEXT,
            japanese_faces_json TEXT,
            related_parts_json TEXT,

            reserved INTEGER NOT NULL DEFAULT 0,
            copy_limit INTEGER NOT NULL DEFAULT 1,
            copy_limit_reason TEXT NOT NULL DEFAULT 'singleton',

            is_legendary INTEGER NOT NULL DEFAULT 0,
            is_creature INTEGER NOT NULL DEFAULT 0,
            is_land INTEGER NOT NULL DEFAULT 0,
            is_basic_land INTEGER NOT NULL DEFAULT 0,
            is_artifact INTEGER NOT NULL DEFAULT 0,
            is_enchantment INTEGER NOT NULL DEFAULT 0,
            is_planeswalker INTEGER NOT NULL DEFAULT 0,
            is_battle INTEGER NOT NULL DEFAULT 0,
            is_instant INTEGER NOT NULL DEFAULT 0,
            is_sorcery INTEGER NOT NULL DEFAULT 0,
            is_companion INTEGER NOT NULL DEFAULT 0,
            is_background INTEGER NOT NULL DEFAULT 0,

            latest_set_code TEXT,
            latest_set_name TEXT,
            latest_rarity TEXT,
            latest_released_at TEXT,

            japanese_scryfall_id TEXT,
            japanese_set_code TEXT,
            japanese_collector_number TEXT,
            japanese_released_at TEXT,
            japanese_score INTEGER NOT NULL DEFAULT 0,

            legal_standard TEXT,
            legal_pioneer TEXT,
            legal_modern TEXT,
            legal_legacy TEXT,
            legal_pauper TEXT,
            legal_vintage TEXT,
            legal_commander TEXT,
            legal_oathbreaker TEXT,
            legal_brawl TEXT,
            legal_historic TEXT,
            legal_timeless TEXT,
            legal_duel TEXT,
            legal_premodern TEXT,
            legal_predh TEXT
        );

        CREATE TABLE card_sets (
            oracle_id TEXT NOT NULL,
            set_code TEXT NOT NULL,
            set_name TEXT,
            rarities TEXT,
            first_released_at TEXT,
            last_released_at TEXT,
            PRIMARY KEY (oracle_id, set_code)
        );

        CREATE INDEX idx_cards_english_name ON cards(english_name COLLATE NOCASE);
        CREATE INDEX idx_cards_japanese_name ON cards(japanese_name);
        CREATE INDEX idx_cards_mana_value ON cards(mana_value);
        CREATE INDEX idx_cards_color_identity ON cards(color_identity);
        CREATE INDEX idx_cards_commander ON cards(legal_commander);
        CREATE INDEX idx_cards_type_flags ON cards(is_creature, is_land, is_artifact, is_enchantment);
        CREATE INDEX idx_card_sets_set ON card_sets(set_code);
        CREATE INDEX idx_card_sets_oracle ON card_sets(oracle_id);
    """)

    seen_oracles = set()
    english_seen = set()
    state = {}
    total_printings = 0
    japanese_printings = 0

    print("Downloading gzipped JSONL from Scryfall...")
    print(download_uri)

    with request(download_uri) as response:
        with gzip.GzipFile(fileobj=response) as gz:
            with io.TextIOWrapper(gz, encoding="utf-8") as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    total_printings += 1
                    card = json.loads(line)
                    oracle_id = card.get("oracle_id")
                    if not oracle_id:
                        continue

                    release = card.get("released_at") or ""
                    score = preferred_score(card)
                    jp_score = japanese_score(card)
                    oracle_text = english_oracle_text(card)
                    keywords = card.get("keywords") or []
                    type_line = card.get("type_line") or ""
                    copy_limit, copy_reason = card_copy_limit(card, oracle_text)
                    legalities = card.get("legalities") or {}

                    canonical = (
                        card.get("name") or "",
                        type_line or None,
                        oracle_text,
                        join_faces(card, "mana_cost"),
                        card.get("cmc"),
                        card_colors(card),
                        color_string(card.get("color_identity") or []),
                        join_faces(card, "power"),
                        join_faces(card, "toughness"),
                        join_faces(card, "loyalty"),
                        join_faces(card, "defense"),
                        color_string(card.get("produced_mana") or []),
                        card.get("layout"),
                        json.dumps(keywords, ensure_ascii=False, separators=(",", ":")),
                        json.dumps(legalities, ensure_ascii=False, separators=(",", ":")),
                        face_json(card, localized=False),
                        related_parts_json(card),
                        1 if card.get("reserved") else 0,
                        copy_limit,
                        copy_reason,
                        1 if "Legendary" in type_line else 0,
                        1 if "Creature" in type_line else 0,
                        1 if "Land" in type_line else 0,
                        1 if "Basic" in type_line and "Land" in type_line else 0,
                        1 if "Artifact" in type_line else 0,
                        1 if "Enchantment" in type_line else 0,
                        1 if "Planeswalker" in type_line else 0,
                        1 if "Battle" in type_line else 0,
                        1 if "Instant" in type_line else 0,
                        1 if "Sorcery" in type_line else 0,
                        1 if "Companion" in keywords else 0,
                        1 if "Background" in type_line else 0,
                    )

                    if oracle_id not in seen_oracles:
                        preferred = (
                            card.get("id"), card.get("lang"), card.get("set"),
                            card.get("collector_number"), release, score,
                        )
                        legality_values = [legalities.get(k) for k in LEGALITY_COLUMNS]
                        cur.execute(f"""
                            INSERT INTO cards (
                                oracle_id,
                                preferred_scryfall_id, preferred_lang, preferred_set_code,
                                preferred_collector_number, preferred_released_at, preferred_score,
                                english_name, english_type_line, english_oracle_text,
                                mana_cost, mana_value, colors, color_identity, power, toughness,
                                loyalty, defense, produced_mana, layout, keywords_json,
                                legalities_json, faces_json, related_parts_json, reserved,
                                copy_limit, copy_limit_reason,
                                is_legendary, is_creature, is_land, is_basic_land, is_artifact,
                                is_enchantment, is_planeswalker, is_battle, is_instant, is_sorcery,
                                is_companion, is_background,
                                latest_set_code, latest_set_name, latest_rarity, latest_released_at,
                                legal_standard, legal_pioneer, legal_modern, legal_legacy,
                                legal_pauper, legal_vintage, legal_commander, legal_oathbreaker,
                                legal_brawl, legal_historic, legal_timeless, legal_duel,
                                legal_premodern, legal_predh
                            ) VALUES (
                                ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, ?, ?,
                                {','.join('?' for _ in LEGALITY_COLUMNS)}
                            )
                        """, (
                            oracle_id, *preferred, *canonical,
                            card.get("set"), card.get("set_name"), card.get("rarity"), release,
                            *legality_values,
                        ))
                        seen_oracles.add(oracle_id)
                        state[oracle_id] = {
                            "preferred_score": score,
                            "preferred_date": release,
                            "jp_score": 0,
                            "jp_date": "",
                            "latest_date": release,
                        }
                    elif card.get("lang") == "en" and oracle_id not in english_seen:
                        legality_values = [legalities.get(k) for k in LEGALITY_COLUMNS]
                        assignments = ", ".join(f"legal_{k} = ?" for k in LEGALITY_COLUMNS)
                        cur.execute(f"""
                            UPDATE cards SET
                                english_name = ?, english_type_line = ?, english_oracle_text = ?,
                                mana_cost = ?, mana_value = ?, colors = ?, color_identity = ?,
                                power = ?, toughness = ?, loyalty = ?, defense = ?, produced_mana = ?,
                                layout = ?, keywords_json = ?, legalities_json = ?, faces_json = ?,
                                related_parts_json = ?, reserved = ?, copy_limit = ?,
                                copy_limit_reason = ?, is_legendary = ?, is_creature = ?,
                                is_land = ?, is_basic_land = ?, is_artifact = ?, is_enchantment = ?,
                                is_planeswalker = ?, is_battle = ?, is_instant = ?, is_sorcery = ?,
                                is_companion = ?, is_background = ?, {assignments}
                            WHERE oracle_id = ?
                        """, (*canonical, *legality_values, oracle_id))
                        english_seen.add(oracle_id)

                    st = state[oracle_id]

                    if should_replace(score, release, st["preferred_score"], st["preferred_date"]):
                        cur.execute("""
                            UPDATE cards SET
                                preferred_scryfall_id = ?, preferred_lang = ?, preferred_set_code = ?,
                                preferred_collector_number = ?, preferred_released_at = ?,
                                preferred_score = ?
                            WHERE oracle_id = ?
                        """, (
                            card.get("id"), card.get("lang"), card.get("set"),
                            card.get("collector_number"), release, score, oracle_id,
                        ))
                        st["preferred_score"] = score
                        st["preferred_date"] = release

                    if release > st["latest_date"]:
                        cur.execute("""
                            UPDATE cards SET
                                latest_set_code = ?, latest_set_name = ?, latest_rarity = ?,
                                latest_released_at = ?
                            WHERE oracle_id = ?
                        """, (
                            card.get("set"), card.get("set_name"), card.get("rarity"),
                            release, oracle_id,
                        ))
                        st["latest_date"] = release

                    if card.get("lang") == "ja":
                        japanese_printings += 1
                        if should_replace(jp_score, release, st["jp_score"], st["jp_date"]):
                            cur.execute("""
                                UPDATE cards SET
                                    japanese_name = ?, japanese_type_line = ?, japanese_text = ?,
                                    japanese_faces_json = ?, japanese_scryfall_id = ?,
                                    japanese_set_code = ?, japanese_collector_number = ?,
                                    japanese_released_at = ?, japanese_score = ?
                                WHERE oracle_id = ?
                            """, (
                                japanese_name(card), japanese_type(card), japanese_text(card),
                                face_json(card, localized=True), card.get("id"), card.get("set"),
                                card.get("collector_number"), release, jp_score, oracle_id,
                            ))
                            st["jp_score"] = jp_score
                            st["jp_date"] = release

                    rarity = card.get("rarity") or ""
                    cur.execute("""
                        INSERT INTO card_sets (
                            oracle_id, set_code, set_name, rarities,
                            first_released_at, last_released_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(oracle_id, set_code) DO UPDATE SET
                            set_name = excluded.set_name,
                            rarities = CASE
                                WHEN instr(',' || card_sets.rarities || ',', ',' || excluded.rarities || ',') = 0
                                THEN card_sets.rarities || ',' || excluded.rarities
                                ELSE card_sets.rarities
                            END,
                            first_released_at = MIN(card_sets.first_released_at, excluded.first_released_at),
                            last_released_at = MAX(card_sets.last_released_at, excluded.last_released_at)
                    """, (
                        oracle_id, card.get("set"), card.get("set_name"), rarity,
                        release, release,
                    ))

                    if total_printings % 25000 == 0:
                        conn.commit()
                        print(
                            f"Printings: {total_printings:,} | "
                            f"Unique cards: {len(seen_oracles):,} | "
                            f"Japanese printings: {japanese_printings:,}"
                        )

    conn.commit()
    cur.execute("ANALYZE")
    conn.commit()
    cur.execute("VACUUM")
    conn.close()

    with open(db_path, "rb") as src:
        with gzip.open(gz_path, "wb", compresslevel=9) as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)

    db_bytes = os.path.getsize(db_path)
    gz_bytes = os.path.getsize(gz_path)
    manifest = {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scryfall_bulk_updated_at": bulk_updated_at,
        "unique_cards": len(seen_oracles),
        "japanese_printings_processed": japanese_printings,
        "total_printings_processed": total_printings,
        "database": os.path.basename(db_path),
        "database_bytes": db_bytes,
        "database_sha256": sha256_file(db_path),
        "database_gzip": os.path.basename(gz_path),
        "database_gzip_bytes": gz_bytes,
        "database_gzip_sha256": sha256_file(gz_path),
    }
    with open("dist/manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print("\nDone!")
    print(f"Unique cards: {len(seen_oracles):,}")
    print(f"Japanese printings: {japanese_printings:,}")
    print(f"SQLite: {db_bytes / 1024 / 1024:.2f} MB")
    print(f"Gzip: {gz_bytes / 1024 / 1024:.2f} MB")


def main():
    bulk = get_all_cards_info()
    download_uri = bulk.get("jsonl_download_uri")
    if not download_uri:
        raise RuntimeError("jsonl_download_uri not found in Scryfall bulk-data response")

    print("Scryfall all_cards")
    print("Updated:", bulk.get("updated_at"))
    print("Compressed size:", bulk.get("compressed_size", "unknown"))
    create_database(download_uri, bulk.get("updated_at"))


if __name__ == "__main__":
    main()
