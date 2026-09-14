#!/usr/bin/env python3
from __future__ import annotations
import argparse, concurrent.futures, gzip, hashlib, io, json, shutil, sys, threading, time, urllib.request, zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
import build_index

HEADERS={"User-Agent":"DeckLoom-ImagePack/0.1","Accept":"application/json;q=0.9,*/*;q=0.8"}
IMG_HEADERS={"User-Agent":"DeckLoom-ImagePack/0.1","Accept":"image/*,*/*;q=0.8"}

class RateLimiter:
    def __init__(self,rps=8.0):
        self.interval=1.0/rps; self.lock=threading.Lock(); self.next_at=0.0
    def wait(self):
        with self.lock:
            now=time.monotonic(); at=max(now,self.next_at); self.next_at=at+self.interval
        if at>now: time.sleep(at-now)
LIMITER=RateLimiter(8.0)

@dataclass
class Pick:
    score:int; released:str; sid:str; urls:list[str]

@dataclass(frozen=True)
class Task:
    category:str; object_key:str; sid:str; face:int; url:str
    @property
    def relative_path(self): return f"{self.category}/{self.sid}-f{self.face}.jpg"

def request(url,image=False,timeout=180):
    if image: LIMITER.wait()
    return urllib.request.urlopen(urllib.request.Request(url,headers=IMG_HEADERS if image else HEADERS),timeout=timeout)

def sha256_bytes(data): return hashlib.sha256(data).hexdigest()
def sha256_file(path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""): h.update(chunk)
    return h.hexdigest()

def small_urls(card):
    top=(card.get("image_uris") or {}).get("small")
    if top: return [top]
    return [u for face in card.get("card_faces") or [] if (u:=(face.get("image_uris") or {}).get("small"))]

def select_printings(uri):
    cards={}; tokens={}; stats=Counter()
    with request(uri) as r, gzip.GzipFile(fileobj=r) as gz, io.TextIOWrapper(gz,encoding="utf-8") as s:
        for line in s:
            if not line.strip(): continue
            stats["bulk_rows"]+=1; card=json.loads(line); urls=small_urls(card)
            if not urls:
                stats["rows_without_small_image"]+=1; continue
            sid=card.get("id") or ""; released=card.get("released_at") or ""; score=build_index.preferred_score(card)
            if build_index.is_token_object(card):
                key=build_index.token_key(card); old=tokens.get(key)
                if old is None or build_index.should_replace(score,released,old.score,old.released):
                    tokens[key]=Pick(score,released,sid,urls)
                continue
            oid=card.get("oracle_id")
            if not oid:
                stats["rows_without_oracle_id"]+=1; continue
            old=cards.get(oid)
            if old is None or build_index.should_replace(score,released,old.score,old.released):
                cards[oid]=Pick(score,released,sid,urls)
    stats["selected_cards"]=len(cards); stats["selected_tokens"]=len(tokens)
    return cards,tokens,dict(stats)

def build_tasks(cards,tokens):
    out=[]
    for key,p in cards.items():
        for face,url in enumerate(p.urls):
            out.append(Task("card_primary" if face==0 else "card_extra_face",key,p.sid,face,url))
    for key,p in tokens.items():
        for face,url in enumerate(p.urls):
            out.append(Task("token_primary" if face==0 else "token_extra_face",key,p.sid,face,url))
    out.sort(key=lambda x:(x.category,x.object_key,x.sid,x.face,x.url))
    seen=set(); unique=[]
    for t in out:
        if t.url not in seen: seen.add(t.url); unique.append(t)
    return unique

def download_one(task,root,retries=3):
    path=root/task.relative_path; path.parent.mkdir(parents=True,exist_ok=True); err=None
    for attempt in range(retries):
        try:
            with request(task.url,image=True,timeout=90) as r: data=r.read()
            if not data: raise RuntimeError("empty response")
            path.write_bytes(data); return task,len(data),sha256_bytes(data),None
        except Exception as exc:
            err=f"{type(exc).__name__}: {exc}"
            if attempt+1<retries: time.sleep(attempt+1)
    return task,0,None,err

def human_bytes(value):
    n=float(value)
    for unit in ["B","KiB","MiB","GiB","TiB"]:
        if n<1024 or unit=="TiB": return f"{n:.2f} {unit}"
        n/=1024

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--workers",type=int,default=4); ap.add_argument("--max-images",type=int,default=0); ap.add_argument("--output-dir",default="dist/image-pack"); a=ap.parse_args()
    if not 1<=a.workers<=8 or a.max_images<0: ap.error("workers=1..8, max-images>=0")
    out=Path(a.output_dir); shutil.rmtree(out,ignore_errors=True); root=out/"images"; root.mkdir(parents=True,exist_ok=True)
    started=time.monotonic(); bulk=build_index.get_all_cards_info(); uri=bulk.get("jsonl_download_uri") or bulk.get("download_uri")
    if not uri: raise RuntimeError("Scryfall all_cards download URI not found")
    cards,tokens,selection=select_printings(uri); all_tasks=build_tasks(cards,tokens); tasks=all_tasks[:a.max_images] if a.max_images else all_tasks
    print(f"cards={len(cards):,} tokens={len(tokens):,} full={len(all_tasks):,} run={len(tasks):,}")
    entries={}; failures=[]; completed=0; lock=threading.Lock()
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as ex:
        futures=[ex.submit(download_one,t,root) for t in tasks]
        for fut in concurrent.futures.as_completed(futures):
            t,size,digest,err=fut.result()
            if err:
                failures.append({"category":t.category,"object_key":t.object_key,"scryfall_id":t.sid,"face":t.face,"source_url":t.url,"error":err})
            else:
                entries[t.relative_path]={"category":t.category,"object_key":t.object_key,"scryfall_id":t.sid,"face":t.face,"source_url":t.url,"bytes":size,"sha256":digest}
            with lock:
                completed+=1
                if completed%250==0 or completed==len(tasks): print(f"{completed:,}/{len(tasks):,} failed={len(failures):,}")
    if failures:
        (out/"failed.json").write_text(json.dumps(failures,ensure_ascii=False,indent=2),encoding="utf-8"); return 1
    zpath=out/"deckloom-small-images.zip"
    with zipfile.ZipFile(zpath,"w",compression=zipfile.ZIP_STORED) as z:
        for p in sorted(x for x in root.rglob("*") if x.is_file()): z.write(p,p.relative_to(root))
    raw=sum(p.stat().st_size for p in root.rglob("*") if p.is_file()); zipped=zpath.stat().st_size; elapsed=time.monotonic()-started
    counts=Counter(v["category"] for v in entries.values())
    manifest={"schema_version":1,"generated_at":datetime.now(timezone.utc).isoformat(),"scryfall_bulk_updated_at":bulk.get("updated_at"),"is_full_pack":a.max_images==0,"selection":selection,"image_count":len(entries),"raw_bytes":raw,"archive":zpath.name,"archive_bytes":zipped,"archive_sha256":sha256_file(zpath),"categories":dict(counts),"entries":{k:entries[k] for k in sorted(entries)}}
    (out/"image-manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,separators=(",",":")),encoding="utf-8")
    summary=["# DeckLoom small image pack","",f"- Scryfall Bulk updated: {bulk.get('updated_at','unknown')}",f"- Images: {len(entries):,}",f"- Raw size: {human_bytes(raw)}",f"- ZIP size: {human_bytes(zipped)}",f"- ZIP SHA-256: {manifest['archive_sha256']}",f"- Elapsed: {elapsed/60:.1f} min",f"- Full pack: {manifest['is_full_pack']}"]
    (out/"summary.md").write_text("\n".join(summary)+"\n",encoding="utf-8"); print("\n".join(summary))
    return 0

if __name__=="__main__": raise SystemExit(main())
