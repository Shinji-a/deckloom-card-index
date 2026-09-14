#!/usr/bin/env python3
from __future__ import annotations
import argparse, concurrent.futures, gzip, io, json, shutil, sys, threading, time, urllib.request, zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_index

HEADERS = {"User-Agent":"DeckLoom-ImagePackMeasure/0.1","Accept":"application/json;q=0.9,*/*;q=0.8"}
IMG_HEADERS = {"User-Agent":"DeckLoom-ImagePackMeasure/0.1","Accept":"image/*,*/*;q=0.8"}

class Limiter:
    def __init__(self, rps=8.0):
        self.interval=1.0/rps; self.lock=threading.Lock(); self.next_at=0.0
    def wait(self):
        with self.lock:
            now=time.monotonic(); at=max(now,self.next_at); self.next_at=at+self.interval
        if at>now: time.sleep(at-now)
LIMITER=Limiter(8.0)

@dataclass
class Pick:
    score:int
    released:str
    sid:str
    urls:list[str]

@dataclass(frozen=True)
class Task:
    category:str
    key:str
    sid:str
    face:int
    url:str

def req(url, image=False, timeout=180):
    if image: LIMITER.wait()
    return urllib.request.urlopen(urllib.request.Request(url, headers=IMG_HEADERS if image else HEADERS), timeout=timeout)

def small_urls(card):
    u=(card.get("image_uris") or {}).get("small")
    if u: return [u]
    return [u for f in card.get("card_faces") or [] if (u:=(f.get("image_uris") or {}).get("small"))]

def select(uri):
    cards={}; tokens={}; stats=Counter()
    with req(uri) as r, gzip.GzipFile(fileobj=r) as gz, io.TextIOWrapper(gz, encoding="utf-8") as s:
        for line in s:
            if not line.strip(): continue
            stats["bulk_rows"]+=1
            c=json.loads(line); urls=small_urls(c)
            if not urls:
                stats["rows_without_small_image"]+=1; continue
            sid=c.get("id") or ""; released=c.get("released_at") or ""; score=build_index.preferred_score(c)
            if build_index.is_token_object(c):
                key=build_index.token_key(c); old=tokens.get(key)
                if old is None or build_index.should_replace(score,released,old.score,old.released):
                    tokens[key]=Pick(score,released,sid,urls)
                continue
            oid=c.get("oracle_id")
            if not oid:
                stats["rows_without_oracle_id"]+=1; continue
            old=cards.get(oid)
            if old is None or build_index.should_replace(score,released,old.score,old.released):
                cards[oid]=Pick(score,released,sid,urls)
    stats["selected_cards"]=len(cards); stats["selected_tokens"]=len(tokens)
    return cards,tokens,dict(stats)

def tasks_for(cards,tokens):
    out=[]
    for key,p in cards.items():
        for i,u in enumerate(p.urls): out.append(Task("card_primary" if i==0 else "card_extra_face",key,p.sid,i,u))
    for key,p in tokens.items():
        for i,u in enumerate(p.urls): out.append(Task("token_primary" if i==0 else "token_extra_face",key,p.sid,i,u))
    out.sort(key=lambda x:(x.category,x.key,x.sid,x.face,x.url))
    seen=set(); unique=[]
    for t in out:
        if t.url not in seen: seen.add(t.url); unique.append(t)
    return unique

def download(t, root, retries=3):
    d=root/t.category; d.mkdir(parents=True, exist_ok=True)
    p=d/f"{t.sid}-f{t.face}.jpg"; err=None
    for n in range(retries):
        try:
            with req(t.url, image=True, timeout=90) as r: data=r.read()
            if not data: raise RuntimeError("empty response")
            p.write_bytes(data); return t,len(data),None
        except Exception as e:
            err=f"{type(e).__name__}: {e}"
            if n+1<retries: time.sleep(n+1)
    return t,0,err

def h(n):
    x=float(n)
    for u in ["B","KiB","MiB","GiB","TiB"]:
        if x<1024 or u=="TiB": return f"{x:.2f} {u}"
        x/=1024

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--workers",type=int,default=4)
    ap.add_argument("--max-images",type=int,default=0)
    ap.add_argument("--output-dir",default="dist/image-pack-measure")
    a=ap.parse_args()
    if not 1<=a.workers<=8 or a.max_images<0: ap.error("workers=1..8, max-images>=0")

    out=Path(a.output_dir); shutil.rmtree(out,ignore_errors=True); imgs=out/"images"; imgs.mkdir(parents=True)
    started=time.monotonic()
    bulk=build_index.get_all_cards_info(); uri=bulk.get("jsonl_download_uri") or bulk.get("download_uri")
    if not uri: raise RuntimeError("Scryfall all_cards download URI not found")
    cards,tokens,sel=select(uri); tasks=tasks_for(cards,tokens); full=len(tasks)
    if a.max_images: tasks=tasks[:a.max_images]
    print(f"cards={len(cards):,} tokens={len(tokens):,} full_images={full:,} this_run={len(tasks):,}")

    sizes={}; failures=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs=[ex.submit(download,t,imgs) for t in tasks]
        for i,f in enumerate(concurrent.futures.as_completed(futs),1):
            t,size,err=f.result()
            if err: failures.append((t,err))
            else: sizes[t.url]=size
            if i%250==0 or i==len(tasks): print(f"{i:,}/{len(tasks):,} failed={len(failures):,}")

    raw=sum(p.stat().st_size for p in imgs.rglob("*") if p.is_file())
    zpath=out/"measure.zip"
    with zipfile.ZipFile(zpath,"w",zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for p in sorted(x for x in imgs.rglob("*") if x.is_file()): z.write(p,p.relative_to(imgs))
    zipped=zpath.stat().st_size; elapsed=time.monotonic()-started
    counts=Counter(t.category for t in tasks); bybytes=Counter()
    for t in tasks: bybytes[t.category]+=sizes.get(t.url,0)
    downloaded=len(tasks)-len(failures); avg=raw/downloaded if downloaded else 0

    report={"generated_at":datetime.now(timezone.utc).isoformat(),"bulk_updated_at":bulk.get("updated_at"),
      "selection":sel,"measurement":{"full_image_count":full,"requested_images":len(tasks),"downloaded_images":downloaded,
      "failed_images":len(failures),"max_images":a.max_images,"workers":a.workers,"raw_bytes":raw,"zip_bytes":zipped,
      "average_bytes_per_downloaded_image":avg,"elapsed_seconds":elapsed,"category_counts":dict(counts),"category_bytes":dict(bybytes)}}
    (out/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    md=["# DeckLoom small image pack measurement","",
      f"- Scryfall Bulk updated: {bulk.get('updated_at','unknown')}",
      f"- Selected unique cards: {len(cards):,}",
      f"- Selected unique tokens/emblems: {len(tokens):,}",
      f"- Full unique small-image URLs: {full:,}",
      f"- Requested images: {len(tasks):,}",
      f"- Downloaded images: {downloaded:,}",
      f"- Failed images: {len(failures):,}",
      f"- Raw image size: **{h(raw)}** ({raw:,} bytes)",
      f"- ZIP size: **{h(zipped)}** ({zipped:,} bytes)",
      f"- Average image size: {h(int(avg)) if downloaded else 'n/a'}",
      f"- Elapsed: {elapsed/60:.1f} min","",
      "| Category | Images | Raw size |","|---|---:|---:|"]
    for c in sorted(counts): md.append(f"| {c} | {counts[c]:,} | {h(bybytes[c])} |")
    if a.max_images: md+=["",f"> Limited measurement: max_images={a.max_images}"]
    (out/"report.md").write_text("\n".join(md)+"\n",encoding="utf-8")
    (out/"failed.txt").write_text("\n".join(f"{t.category}\t{t.sid}\tface={t.face}\t{t.url}\t{e}" for t,e in failures)+("\n" if failures else ""),encoding="utf-8")
    print((out/"report.md").read_text())
    shutil.rmtree(imgs,ignore_errors=True); zpath.unlink(missing_ok=True)
    return 1 if failures else 0

if __name__=="__main__":
    raise SystemExit(main())
