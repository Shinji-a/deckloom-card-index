#!/usr/bin/env python3
from __future__ import annotations
import argparse, gzip, io, json, random, sys, time, urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
import build_index

HEADERS={"User-Agent":"DeckLoom-WebP-Lab/0.1","Accept":"application/json;q=0.9,*/*;q=0.8"}
IMG_HEADERS={"User-Agent":"DeckLoom-WebP-Lab/0.1","Accept":"image/*,*/*;q=0.8"}

# Cards/decks the user has actually worked with in DeckLoom chats.
# Missing names are harmless; representative cards fill the remainder.
PREFERRED_NAMES={
"Peter Parker","Amazing Spider-Man","Elven Chorus","Cryptolith Rite","Folk Hero",
"Up the Beanstalk","Dazzling Theater","Prop Room","Resourceful Defense","Spider-UK",
"Spider-Man, Web-Slinger","Jarvis, Loyal Butler","Silk, Web Weaver","Bast, Feline Familiar",
"Captain America","Vedalken Orrery","Leyline of Anticipation","Wilderness Reclamation",
"Shang-Chi","SP//dr","Raise the Palisade","Urza's Ruinous Blast",
"Zur, Eternal Schemer","Slogurk, the Overslime","Chiss-Goria, Forge Tyrant",
"Life from the Loam","Azusa, Lost but Seeking","Oracle of Mul Daya","Druid Class",
"New Perspectives","Platinum Angel","Wand of Wonder","Mind's Eye",
}

RARITY_RANK={"common":0,"uncommon":1,"rare":2,"mythic":3,"special":4,"bonus":5}

def req(url,image=False):
    return urllib.request.urlopen(urllib.request.Request(url,headers=IMG_HEADERS if image else HEADERS),timeout=180)

def image_url(card):
    u=(card.get("image_uris") or {}).get("normal")
    if u: return u
    for f in card.get("card_faces") or []:
        u=(f.get("image_uris") or {}).get("normal")
        if u: return u
    return None

def canonical_names(card):
    out={card.get("name") or ""}
    out.update(f.get("name") or "" for f in card.get("card_faces") or [])
    return out

def printing_key(card):
    # Prefer a normal, low-rarity, old printing. This intentionally rejects the
    # usual "newest Japanese printing" representative policy for visual QA.
    # Oldest release dominates; rarity breaks ties. Digital/non-paper is last.
    paper=0 if "paper" in set(card.get("games") or []) else 1
    date=card.get("released_at") or "9999-99-99"
    rarity=RARITY_RANK.get(card.get("rarity") or "",99)
    promo=1 if card.get("promo") else 0
    borderless=1 if card.get("border_color")=="borderless" else 0
    return (paper,date,rarity,promo,borderless,card.get("collector_number") or "",card.get("id") or "")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--count",type=int,default=500)
    ap.add_argument("--width",type=int,default=360)
    ap.add_argument("--quality",type=int,default=80)
    ap.add_argument("--output-dir",default="dist/webp-lab")
    a=ap.parse_args()
    out=Path(a.output_dir); imgdir=out/"images"; imgdir.mkdir(parents=True,exist_ok=True)
    bulk=build_index.get_all_cards_info(); uri=bulk.get("jsonl_download_uri") or bulk.get("download_uri")
    best={}; preferred=set(); rows=0
    with req(uri) as r, gzip.GzipFile(fileobj=r) as gz, io.TextIOWrapper(gz,encoding="utf-8") as s:
        for line in s:
            if not line.strip(): continue
            rows+=1; card=json.loads(line)
            oid=card.get("oracle_id"); url=image_url(card)
            if not oid or not url: continue
            old=best.get(oid)
            if old is None or printing_key(card)<printing_key(old): best[oid]=card
            if canonical_names(card)&PREFERRED_NAMES: preferred.add(oid)
    chosen=[best[x] for x in sorted(preferred) if x in best]
    # Deterministic representative fill, not random between runs.
    remaining=[c for oid,c in sorted(best.items()) if oid not in preferred]
    chosen.extend(remaining[:max(0,a.count-len(chosen))]); chosen=chosen[:a.count]
    raw=webp=failed=0; categories=Counter(); started=time.monotonic(); report=[]
    for i,card in enumerate(chosen,1):
        url=image_url(card); sid=card["id"]
        try:
            with req(url,image=True) as r: data=r.read()
            raw+=len(data)
            src=out/f"{sid}.src"; src.write_bytes(data)
            with Image.open(src) as im:
                im=im.convert("RGB")
                h=round(im.height*a.width/im.width)
                im=im.resize((a.width,h),Image.Resampling.LANCZOS)
                dst=imgdir/f"{i:03d}-{sid}.webp"
                im.save(dst,"WEBP",quality=a.quality,method=6)
            size=dst.stat().st_size; webp+=size; src.unlink()
            categories[card.get("rarity") or "unknown"]+=1
            report.append({"index":i,"name":card.get("name"),"set":card.get("set"),"released_at":card.get("released_at"),"rarity":card.get("rarity"),"scryfall_id":sid,"source_bytes":len(data),"webp_bytes":size})
        except Exception as e:
            failed+=1; report.append({"index":i,"name":card.get("name"),"error":str(e)})
        if i%50==0: print(f"{i}/{len(chosen)} failed={failed}")
        time.sleep(0.11)
    elapsed=time.monotonic()-started
    estimate=webp/max(1,len(chosen)-failed)*41579
    summary=f"""# DeckLoom 360px WebP lab

- Requested: {len(chosen)}
- Preferred user cards found: {min(len(preferred),len(chosen))}
- Failed: {failed}
- Width: {a.width}px
- WebP quality: {a.quality}
- Source normal bytes: {raw:,}
- WebP bytes: {webp:,}
- Average WebP: {webp/max(1,len(chosen)-failed):,.1f} bytes
- Estimated 41,579-image pack: {estimate/1024/1024:.2f} MiB
- Source/WebP ratio: {(webp/raw if raw else 0):.3f}
- Elapsed: {elapsed/60:.1f} min
- Selection: user's previously used cards first; remainder deterministic representative cards.
- Printing preference: paper first, then oldest release, then lowest rarity; promo/borderless lose ties.
"""
    (out/"summary.md").write_text(summary,encoding="utf-8")
    (out/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(summary)

if __name__=="__main__": main()
