import json, os, sys, time, urllib.request, urllib.error, ssl
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.stdout.reconfigure(encoding="utf-8")
ROOT=Path(os.environ.get("SQUAD_MAP_ROOT") or Path(__file__).resolve().parents[1])
catalog=json.loads((ROOT/"tools/maps_catalog.json").read_text(encoding="utf-8"))
out=ROOT/".tmp/maps_source"
out.mkdir(parents=True, exist_ok=True)
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"
ctx=ssl._create_unverified_context()
files=["heightmap.png","basemap.webp","topomap.webp","3d/trees.json","3d/trees.bin"]

def fetch(m, rel):
    slug=m["slug"]
    dst=out/slug/rel
    if dst.exists() and dst.stat().st_size>0:
        return slug, rel, dst.stat().st_size, "cached"
    dst.parent.mkdir(parents=True, exist_ok=True)
    url="https://squadcalc.app/api"+m["path"]+rel
    last=""
    for attempt in range(3):
        try:
            req=urllib.request.Request(url, headers={"User-Agent":UA})
            with urllib.request.urlopen(req, timeout=120, context=ctx) as r:
                data=r.read()
            dst.write_bytes(data)
            return slug, rel, len(data), "ok"
        except urllib.error.HTTPError as e:
            if e.code==404:
                return slug, rel, 0, "404"
            last=str(e)
        except Exception as e:
            last=str(e)
        time.sleep(1.5)
    return slug, rel, 0, "ERR "+last

jobs=[]
with ThreadPoolExecutor(max_workers=4) as ex:
    for m in catalog:
        for rel in files:
            jobs.append(ex.submit(fetch,m,rel))
    for f in as_completed(jobs):
        slug,rel,size,status=f.result()
        print(f"{slug:14s} {rel:20s} {size:9d} {status}", flush=True)
