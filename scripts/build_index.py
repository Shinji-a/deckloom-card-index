import gzip
import hashlib
import io
import json
import os
import re
import sqlite3
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone

API_URL = "https://api.scryfall.com/bulk-data"
HEADERS = {
    "User-Agent": "DeckLoom-CardIndex/0.4",
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

# UIで直接使う基本カード・タイプ名。サブタイプは日本語印刷から自動推測する。
CARD_TYPE_JA = {
    "Creature": "クリーチャー",
    "Artifact": "アーティファクト",
    "Enchantment": "エンチャント",
    "Instant": "インスタント",
    "Sorcery": "ソーサリー",
    "Land": "土地",
    "Planeswalker": "プレインズウォーカー",
    "Battle": "バトル",
    "Kindred": "同族",
}

# Scryfall keywords は英語の正規語のみなので、よく使うものは確定辞書を持つ。
# 辞書に無いものは日本語印刷テキストから安全に推測できた場合だけ自動補完し、
# それも無理ならアプリ側で英語をそのまま表示する。
KEYWORD_JA = {
    "Flying": "飛行",
    "First strike": "先制攻撃",
    "Double strike": "二段攻撃",
    "Deathtouch": "接死",
    "Defender": "防衛",
    "Flash": "瞬速",
    "Haste": "速攻",
    "Hexproof": "呪禁",
    "Indestructible": "破壊不能",
    "Lifelink": "絆魂",
    "Menace": "威迫",
    "Reach": "到達",
    "Trample": "トランプル",
    "Vigilance": "警戒",
    "Ward": "護法",
    "Prowess": "果敢",
    "Cycling": "サイクリング",
    "Flashback": "フラッシュバック",
    "Kicker": "キッカー",
    "Buyback": "バイバック",
    "Cascade": "続唱",
    "Convoke": "召集",
    "Delve": "探査",
    "Dredge": "発掘",
    "Equip": "装備",
    "Affinity": "親和",
    "Modular": "接合",
    "Ninjutsu": "忍術",
    "Reconfigure": "換装",
    "Mutate": "変容",
    "Disturb": "降霊",
    "Escape": "脱出",
    "Foretell": "予顕",
    "Suspend": "待機",
    "Unearth": "蘇生",
    "Retrace": "回顧",
    "Madness": "マッドネス",
    "Miracle": "奇跡",
    "Persist": "頑強",
    "Undying": "不死",
    "Exalted": "賛美",
    "Evoke": "想起",
    "Bestow": "授与",
    "Exploit": "濫用",
    "Dash": "疾駆",
    "Emerge": "現出",
    "Embalm": "不朽",
    "Eternalize": "永遠",
    "Mentor": "教導",
    "Afterlife": "死後",
    "Spectacle": "絢爛",
    "Escape": "脱出",
    "Companion": "相棒",
    "Encore": "再演",
    "Training": "訓練",
    "Blitz": "奇襲",
    "Backup": "賛助",
    "Bargain": "協約",
    "Discover": "発見",
    "Craft": "作製",
    "Plot": "計画",
}

# 現行Oracleで代表的な複数語サブタイプ。通常のサブタイプは空白1語。
MULTIWORD_SUBTYPES = {
    "Time Lord",
}


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


def numeric_value(value):
    if value is None:
        return None
    text = str(value).strip()
    if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return float(text)
    return None


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


def split_after_dash(type_line):
    if not type_line:
        return None
    for sep in (" — ", "—", " – ", "–"):
        if sep in type_line:
            return type_line.split(sep, 1)[1].strip()
    return None


def tokenize_english_subtypes(text):
    if not text:
        return []
    words = text.split()
    result = []
    i = 0
    max_words = max((len(x.split()) for x in MULTIWORD_SUBTYPES), default=1)
    while i < len(words):
        match = None
        match_len = 0
        for length in range(min(max_words, len(words) - i), 1, -1):
            candidate = " ".join(words[i:i + length])
            if candidate in MULTIWORD_SUBTYPES:
                match = candidate
                match_len = length
                break
        if match:
            result.append(match)
            i += match_len
        else:
            result.append(words[i])
            i += 1
    return result


def tokenize_japanese_subtypes(text):
    if not text:
        return []
    return [x.strip() for x in re.split(r"[・･]", text) if x.strip()]


def collect_subtype_votes(card, subtype_votes):
    if card.get("lang") != "ja":
        return

    pairs = [(card.get("type_line"), card.get("printed_type_line"))]
    for face in card.get("card_faces") or []:
        pairs.append((face.get("type_line"), face.get("printed_type_line")))

    for english_line, japanese_line in pairs:
        en_sub = split_after_dash(english_line)
        ja_sub = split_after_dash(japanese_line)
        if not en_sub or not ja_sub:
            continue

        english_parts = tokenize_english_subtypes(en_sub)
        japanese_parts = tokenize_japanese_subtypes(ja_sub)
        if len(english_parts) != len(japanese_parts):
            continue

        for canonical, localized in zip(english_parts, japanese_parts):
            if canonical and localized:
                subtype_votes[canonical][localized] += 1


def extract_japanese_keyword_label(text):
    if not text:
        return None
    line = next((x.strip() for x in text.splitlines() if x.strip()), None)
    if not line:
        return None
    # reminder text / cost / parameter 以降を落として能力名だけを残す
    label = re.split(r"[（(\{—―]", line, maxsplit=1)[0].strip()
    label = re.sub(r"[0-9０-９]+$", "", label).strip(" ：:")
    if not label or len(label) > 24:
        return None
    return label


def collect_keyword_votes(card, keyword_votes):
    if card.get("lang") != "ja":
        return
    keywords = card.get("keywords") or []
    if len(keywords) != 1:
        return

    keyword = keywords[0]
    english = english_oracle_text(card) or ""
    localized = japanese_text(card) or ""
    if not english.lower().startswith(keyword.lower()):
        return

    candidate = extract_japanese_keyword_label(localized)
    if candidate:
        keyword_votes[keyword][candidate] += 1


def is_token_object(card):
    return (
        card.get("set_type") == "token"
        or card.get("layout") in {"token", "double_faced_token", "emblem"}
    )


def token_key(card):
    raw = "\u241f".join([
        card.get("name") or "",
        card.get("type_line") or "",
        english_oracle_text(card) or "",
        join_faces(card, "power") or "",
        join_faces(card, "toughness") or "",
        card_colors(card),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def image_urls(card):
    uris = card.get("image_uris") or {}
    if not uris:
        for face in card.get("card_faces") or []:
            if face.get("image_uris"):
                uris = face["image_uris"]
                break
    return uris.get("normal"), uris.get("small")


def insert_dict(cur, table, values):
    columns = list(values.keys())
    placeholders = ",".join("?" for _ in columns)
    cur.execute(
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
        [values[c] for c in columns],
    )


def update_dict(cur, table, values, where_column, where_value):
    assignments = ",".join(f"{column} = ?" for column in values)
    cur.execute(
        f"UPDATE {table} SET {assignments} WHERE {where_column} = ?",
        [*values.values(), where_value],
    )


def canonical_card_data(card):
    oracle_text = english_oracle_text(card)
    type_line = card.get("type_line") or ""
    keywords = card.get("keywords") or []
    legalities = card.get("legalities") or {}
    copy_limit, copy_reason = card_copy_limit(card, oracle_text)
    power = join_faces(card, "power")
    toughness = join_faces(card, "toughness")

    values = {
        "english_name": card.get("name") or "",
        "english_type_line": type_line or None,
        "english_oracle_text": oracle_text,
        "mana_cost": join_faces(card, "mana_cost"),
        "mana_value": card.get("cmc"),
        "colors": card_colors(card),
        "color_identity": color_string(card.get("color_identity") or []),
        "power": power,
        "power_numeric": numeric_value(power),
        "toughness": toughness,
        "toughness_numeric": numeric_value(toughness),
        "loyalty": join_faces(card, "loyalty"),
        "defense": join_faces(card, "defense"),
        "produced_mana": color_string(card.get("produced_mana") or []),
        "layout": card.get("layout"),
        "keywords_json": json.dumps(keywords, ensure_ascii=False, separators=(",", ":")),
        "legalities_json": json.dumps(legalities, ensure_ascii=False, separators=(",", ":")),
        "faces_json": face_json(card, localized=False),
        "related_parts_json": related_parts_json(card),
        "reserved": 1 if card.get("reserved") else 0,
        "copy_limit": copy_limit,
        "copy_limit_reason": copy_reason,
        "is_legendary": 1 if "Legendary" in type_line else 0,
        "is_creature": 1 if "Creature" in type_line else 0,
        "is_land": 1 if "Land" in type_line else 0,
        "is_basic_land": 1 if "Basic" in type_line and "Land" in type_line else 0,
        "is_artifact": 1 if "Artifact" in type_line else 0,
        "is_enchantment": 1 if "Enchantment" in type_line else 0,
        "is_planeswalker": 1 if "Planeswalker" in type_line else 0,
        "is_battle": 1 if "Battle" in type_line else 0,
        "is_instant": 1 if "Instant" in type_line else 0,
        "is_sorcery": 1 if "Sorcery" in type_line else 0,
        "is_companion": 1 if "Companion" in keywords else 0,
        "is_background": 1 if "Background" in type_line else 0,
    }
    for legality in LEGALITY_COLUMNS:
        values[f"legal_{legality}"] = legalities.get(legality)
    return values


def process_token(cur, card, token_states, token_term_votes):
    key = token_key(card)
    sid = card.get("id")
    release = card.get("released_at") or ""
    score = preferred_score(card)
    jp_score = japanese_score(card)
    normal, small = image_urls(card)

    cur.execute(
        "INSERT OR REPLACE INTO token_aliases (scryfall_id, token_key) VALUES (?, ?)",
        (sid, key),
    )

    if key not in token_states:
        insert_dict(cur, "tokens", {
            "token_key": key,
            "english_name": card.get("name") or "Token",
            "english_type_line": card.get("type_line"),
            "english_oracle_text": english_oracle_text(card),
            "power": join_faces(card, "power"),
            "toughness": join_faces(card, "toughness"),
            "colors": card_colors(card),
            "preferred_scryfall_id": sid,
            "preferred_lang": card.get("lang"),
            "preferred_score": score,
            "preferred_released_at": release,
            "preferred_image_normal": normal,
            "preferred_image_small": small,
        })
        token_states[key] = {
            "preferred_score": score,
            "preferred_date": release,
            "jp_score": 0,
            "jp_date": "",
        }

    st = token_states[key]
    if should_replace(score, release, st["preferred_score"], st["preferred_date"]):
        update_dict(cur, "tokens", {
            "preferred_scryfall_id": sid,
            "preferred_lang": card.get("lang"),
            "preferred_score": score,
            "preferred_released_at": release,
            "preferred_image_normal": normal,
            "preferred_image_small": small,
        }, "token_key", key)
        st["preferred_score"] = score
        st["preferred_date"] = release

    if card.get("lang") == "ja":
        jp_name = japanese_name(card)
        if jp_name:
            token_term_votes[card.get("name") or "Token"][jp_name] += 1
        if should_replace(jp_score, release, st["jp_score"], st["jp_date"]):
            update_dict(cur, "tokens", {
                "japanese_name": jp_name,
                "japanese_type_line": japanese_type(card),
                "japanese_text": japanese_text(card),
                "japanese_scryfall_id": sid,
                "japanese_score": jp_score,
                "japanese_released_at": release,
                "japanese_image_normal": normal,
                "japanese_image_small": small,
            }, "token_key", key)
            st["jp_score"] = jp_score
            st["jp_date"] = release


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
            power_numeric REAL,
            toughness TEXT,
            toughness_numeric REAL,
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

            first_released_at TEXT,
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

        CREATE TABLE display_terms (
            category TEXT NOT NULL,
            canonical TEXT NOT NULL,
            japanese TEXT,
            source TEXT NOT NULL,
            source_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (category, canonical)
        );

        CREATE TABLE tokens (
            token_key TEXT PRIMARY KEY,
            english_name TEXT NOT NULL,
            japanese_name TEXT,
            english_type_line TEXT,
            japanese_type_line TEXT,
            english_oracle_text TEXT,
            japanese_text TEXT,
            power TEXT,
            toughness TEXT,
            colors TEXT,
            preferred_scryfall_id TEXT,
            preferred_lang TEXT,
            preferred_score INTEGER NOT NULL DEFAULT 0,
            preferred_released_at TEXT,
            preferred_image_normal TEXT,
            preferred_image_small TEXT,
            japanese_scryfall_id TEXT,
            japanese_score INTEGER NOT NULL DEFAULT 0,
            japanese_released_at TEXT,
            japanese_image_normal TEXT,
            japanese_image_small TEXT
        );

        CREATE TABLE token_aliases (
            scryfall_id TEXT PRIMARY KEY,
            token_key TEXT NOT NULL
        );

        CREATE INDEX idx_cards_english_name ON cards(english_name COLLATE NOCASE);
        CREATE INDEX idx_cards_japanese_name ON cards(japanese_name);
        CREATE INDEX idx_cards_mana_value ON cards(mana_value);
        CREATE INDEX idx_cards_power_numeric ON cards(power_numeric);
        CREATE INDEX idx_cards_toughness_numeric ON cards(toughness_numeric);
        CREATE INDEX idx_cards_color_identity ON cards(color_identity);
        CREATE INDEX idx_cards_commander ON cards(legal_commander);
        CREATE INDEX idx_cards_first_release ON cards(first_released_at);
        CREATE INDEX idx_cards_type_flags ON cards(is_creature, is_land, is_artifact, is_enchantment);
        CREATE INDEX idx_card_sets_set ON card_sets(set_code);
        CREATE INDEX idx_card_sets_oracle ON card_sets(oracle_id);
        CREATE INDEX idx_display_terms_category ON display_terms(category, japanese, canonical);
        CREATE INDEX idx_tokens_english_name ON tokens(english_name COLLATE NOCASE);
        CREATE INDEX idx_tokens_japanese_name ON tokens(japanese_name);
        CREATE INDEX idx_token_aliases_key ON token_aliases(token_key);
    """)

    seen_oracles = set()
    english_seen = set()
    card_state = {}
    token_states = {}
    subtype_votes = defaultdict(Counter)
    subtypes_seen = set()
    keyword_votes = defaultdict(Counter)
    token_term_votes = defaultdict(Counter)
    keywords_seen = set()
    total_printings = 0
    japanese_printings = 0
    token_printings = 0

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

                    for keyword in card.get("keywords") or []:
                        keywords_seen.add(keyword)

                    subtype_lines = [card.get("type_line")]
                    subtype_lines.extend(face.get("type_line") for face in card.get("card_faces") or [])
                    for type_line in subtype_lines:
                        en_sub = split_after_dash(type_line)
                        if en_sub:
                            subtypes_seen.update(tokenize_english_subtypes(en_sub))

                    if card.get("lang") == "ja":
                        collect_subtype_votes(card, subtype_votes)
                        collect_keyword_votes(card, keyword_votes)

                    if is_token_object(card):
                        token_printings += 1
                        process_token(cur, card, token_states, token_term_votes)
                        continue

                    oracle_id = card.get("oracle_id")
                    if not oracle_id:
                        continue

                    release = card.get("released_at") or ""
                    score = preferred_score(card)
                    jp_score = japanese_score(card)
                    canonical = canonical_card_data(card)

                    if oracle_id not in seen_oracles:
                        row = {
                            "oracle_id": oracle_id,
                            "preferred_scryfall_id": card.get("id"),
                            "preferred_lang": card.get("lang"),
                            "preferred_set_code": card.get("set"),
                            "preferred_collector_number": card.get("collector_number"),
                            "preferred_released_at": release,
                            "preferred_score": score,
                            **canonical,
                            "first_released_at": release,
                            "latest_set_code": card.get("set"),
                            "latest_set_name": card.get("set_name"),
                            "latest_rarity": card.get("rarity"),
                            "latest_released_at": release,
                        }
                        insert_dict(cur, "cards", row)
                        seen_oracles.add(oracle_id)
                        card_state[oracle_id] = {
                            "preferred_score": score,
                            "preferred_date": release,
                            "jp_score": 0,
                            "jp_date": "",
                            "first_date": release,
                            "latest_date": release,
                        }
                    elif card.get("lang") == "en" and oracle_id not in english_seen:
                        update_dict(cur, "cards", canonical, "oracle_id", oracle_id)
                        english_seen.add(oracle_id)

                    st = card_state[oracle_id]

                    if release and (not st["first_date"] or release < st["first_date"]):
                        update_dict(cur, "cards", {"first_released_at": release}, "oracle_id", oracle_id)
                        st["first_date"] = release

                    if should_replace(score, release, st["preferred_score"], st["preferred_date"]):
                        update_dict(cur, "cards", {
                            "preferred_scryfall_id": card.get("id"),
                            "preferred_lang": card.get("lang"),
                            "preferred_set_code": card.get("set"),
                            "preferred_collector_number": card.get("collector_number"),
                            "preferred_released_at": release,
                            "preferred_score": score,
                        }, "oracle_id", oracle_id)
                        st["preferred_score"] = score
                        st["preferred_date"] = release

                    if release > st["latest_date"]:
                        update_dict(cur, "cards", {
                            "latest_set_code": card.get("set"),
                            "latest_set_name": card.get("set_name"),
                            "latest_rarity": card.get("rarity"),
                            "latest_released_at": release,
                        }, "oracle_id", oracle_id)
                        st["latest_date"] = release

                    if card.get("lang") == "ja":
                        japanese_printings += 1
                        if should_replace(jp_score, release, st["jp_score"], st["jp_date"]):
                            update_dict(cur, "cards", {
                                "japanese_name": japanese_name(card),
                                "japanese_type_line": japanese_type(card),
                                "japanese_text": japanese_text(card),
                                "japanese_faces_json": face_json(card, localized=True),
                                "japanese_scryfall_id": card.get("id"),
                                "japanese_set_code": card.get("set"),
                                "japanese_collector_number": card.get("collector_number"),
                                "japanese_released_at": release,
                                "japanese_score": jp_score,
                            }, "oracle_id", oracle_id)
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
                            f"Japanese: {japanese_printings:,} | Tokens: {token_printings:,}"
                        )

    # 表示用日本語辞書を構築
    for canonical, localized in CARD_TYPE_JA.items():
        cur.execute("""
            INSERT OR REPLACE INTO display_terms
            (category, canonical, japanese, source, source_count)
            VALUES ('card_type', ?, ?, 'manual', 0)
        """, (canonical, localized))

    for canonical in sorted(subtypes_seen):
        if canonical in SUBTYPE_JA:
            localized = SUBTYPE_JA[canonical]
            source = "manual"
            count = 0
        elif subtype_votes.get(canonical):
            localized, count = subtype_votes[canonical].most_common(1)[0]
            source = "inferred"
        else:
            localized = None
            source = "fallback_en"
            count = 0
        cur.execute("""
            INSERT OR REPLACE INTO display_terms
            (category, canonical, japanese, source, source_count)
            VALUES ('subtype', ?, ?, ?, ?)
        """, (canonical, localized, source, count))

    for canonical in sorted(keywords_seen):
        if canonical in KEYWORD_JA:
            localized = KEYWORD_JA[canonical]
            source = "manual"
            count = 0
        elif keyword_votes.get(canonical):
            localized, count = keyword_votes[canonical].most_common(1)[0]
            source = "inferred"
        else:
            localized = None
            source = "fallback_en"
            count = 0
        cur.execute("""
            INSERT OR REPLACE INTO display_terms
            (category, canonical, japanese, source, source_count)
            VALUES ('keyword', ?, ?, ?, ?)
        """, (canonical, localized, source, count))

    # トークン名は日本語版トークンが存在する場合のみ翻訳を入れる。
    cur.execute("SELECT DISTINCT english_name FROM tokens ORDER BY english_name")
    for (canonical,) in cur.fetchall():
        votes = token_term_votes.get(canonical)
        if votes:
            localized, count = votes.most_common(1)[0]
            source = "scryfall_ja"
        else:
            localized = None
            count = 0
            source = "fallback_en"
        cur.execute("""
            INSERT OR REPLACE INTO display_terms
            (category, canonical, japanese, source, source_count)
            VALUES ('token', ?, ?, ?, ?)
        """, (canonical, localized, source, count))

    conn.commit()
    cur.execute("ANALYZE")
    conn.commit()
    cur.execute("VACUUM")

    display_terms_count = cur.execute("SELECT COUNT(*) FROM display_terms").fetchone()[0]
    token_count = cur.execute("SELECT COUNT(*) FROM tokens").fetchone()[0]
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
        "schema_version": 3,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scryfall_bulk_updated_at": bulk_updated_at,
        "unique_cards": len(seen_oracles),
        "japanese_printings_processed": japanese_printings,
        "token_printings_processed": token_printings,
        "unique_tokens": token_count,
        "display_terms": display_terms_count,
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
    print(f"Unique tokens: {token_count:,}")
    print(f"Display terms: {display_terms_count:,}")
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
