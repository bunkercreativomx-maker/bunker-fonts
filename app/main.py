"""Bunker Fonts — servidor de fuentes (índice, vista previa, colecciones, Google Fonts)."""
import os, re, io, time, json, sqlite3, hashlib, threading, subprocess, zipfile, datetime, logging, collections, shutil
from fastapi import FastAPI, HTTPException, Body
from fastapi.responses import FileResponse, Response, StreamingResponse, HTMLResponse

FONTS = os.environ.get("FONTS_DIR", "/fonts")
STORE = os.environ.get("STORE_DIR", "/store")
GOOGLE = os.path.join(STORE, "google")
CACHE = os.path.join(STORE, "cache")
DB = os.path.join(STORE, "fonts.db")
SYNC_HOUR = int(os.environ.get("SYNC_HOUR", "4"))
LOCAL_EVERY = int(os.environ.get("LOCAL_EVERY_MIN", "30")) * 60
EXT = (".ttf", ".otf", ".ttc", ".otc", ".woff", ".woff2")
MIME = {"ttf": "font/ttf", "otf": "font/otf", "woff": "font/woff", "woff2": "font/woff2"}
PREVIEW_RAW_MAX = 300_000
HERE = os.path.dirname(os.path.abspath(__file__))

log = logging.getLogger("fonts")
logging.getLogger("fontTools").setLevel(logging.WARNING)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
state = {"status": "iniciando", "detail": "", "last_index": None, "last_google": None, "error": None}
index_lock = threading.Lock()


def db():
    con = sqlite3.connect(DB, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("pragma journal_mode=wal")
    return con


def init_db():
    os.makedirs(CACHE, exist_ok=True)
    con = db()
    con.executescript("""
    create table if not exists faces(
      id text primary key, path text, idx int, source text, folder text, family text, family_key text,
      style text, weight int, italic int, var int, mode text, sample text, fmt text, size int, mtime real,
      hash text, category text, dup_of text, broken int default 0);
    create index if not exists faces_path on faces(path);
    create index if not exists faces_fam on faces(source, family_key);
    create table if not exists families(
      id text primary key, source text, family text, family_key text, folder text, category text,
      n_faces int, rep_face text, formats text, dup_google int, google_id text, mode text, sample text);
    create index if not exists fam_key on families(family_key);
    create table if not exists favs(family_id text primary key, at real);
    create table if not exists active(family_id text primary key, at real);
    create table if not exists collections(id integer primary key autoincrement, name text unique, at real);
    create table if not exists coll_items(cid int, family_id text, primary key(cid, family_id));
    create table if not exists meta(k text primary key, v text);
    """)
    for r in con.execute("select k, v from meta"):
        if r["k"] in state:
            state[r["k"]] = r["v"]
    con.commit(); con.close()


def set_meta(k, v):
    state[k] = v
    con = db(); con.execute("insert or replace into meta values(?,?)", (k, v)); con.commit(); con.close()


def now_iso():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


# ---------------- scanning ----------------

def sha1_file(p):
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def read_font(tt):
    n = tt["name"]
    fam = (n.getDebugName(16) or n.getDebugName(1) or "").strip()
    sub = (n.getDebugName(17) or n.getDebugName(2) or "Regular").strip()
    w, it = 400, 0
    if "OS/2" in tt:
        w = tt["OS/2"].usWeightClass or 400
        it = 1 if tt["OS/2"].fsSelection & 1 else 0
    if "italic" in sub.lower() or "oblique" in sub.lower():
        it = 1
    cmap = tt.getBestCmap() or {}
    upper = all(ord(c) in cmap for c in "ABEHOR")
    lower = all(ord(c) in cmap for c in "abeor")
    sample = ""
    if upper and lower:
        mode = "latin"
    elif upper:
        mode = "caps"
    else:
        mode = "sample"
        cps = [c for c in sorted(cmap) if c > 32 and not (0x7F <= c < 0xA0) and c not in (0xAD, 0xFEFF)]
        sample = "".join(chr(c) for c in cps[:48])
    return dict(family=fam, style=sub, weight=int(w), italic=it, var=int("fvar" in tt), mode=mode, sample=sample)


def scan_file(path):
    from fontTools.ttLib import TTFont, TTCollection
    low = path.lower()
    if low.endswith((".ttc", ".otc")):
        col = TTCollection(path, lazy=True)
        return [(i, read_font(tt)) for i, tt in enumerate(col.fonts)]
    return [(0, read_font(TTFont(path, lazy=True, fontNumber=0)))]


def local_files():
    for d, dirs, files in os.walk(FONTS):
        rel = os.path.relpath(d, FONTS)
        dirs[:] = [x for x in dirs if not x.startswith(".") and not (rel == "FontBase" and x == "Providers")]
        parts = [] if rel == "." else rel.split(os.sep)
        if parts and parts[0] == "FontBase":
            folder = parts[1] if len(parts) > 1 else "FontBase"
        else:
            folder = parts[0] if parts else "Raíz"
        for f in files:
            if f.lower().endswith(EXT) and not f.startswith("._"):
                yield os.path.join(d, f), folder, None, None


def google_files():
    for lic in ("ofl", "apache", "ufl"):
        base = os.path.join(GOOGLE, lic)
        if not os.path.isdir(base):
            continue
        for fam_dir in os.listdir(base):
            d = os.path.join(base, fam_dir)
            mp = os.path.join(d, "METADATA.pb")
            if not os.path.isfile(mp):
                continue
            txt = open(mp, encoding="utf-8", errors="ignore").read()
            m = re.search(r'^name:\s*"([^"]+)"', txt, re.M)
            c = re.search(r'^category:\s*"([^"]+)"', txt, re.M)
            name = m.group(1) if m else fam_dir
            cat = c.group(1) if c else ""
            for f in os.listdir(d):
                if f.lower().endswith(EXT):
                    yield os.path.join(d, f), "Google", name, cat


def index_all():
    with index_lock:
        state["status"] = "indexando"
        con = db()
        known = {}
        for r in con.execute("select path, mtime, size from faces"):
            known[r["path"]] = (r["mtime"], r["size"])
        seen, n_new, n_err = set(), 0, 0
        for src, gen in (("local", local_files()), ("google", google_files())):
            for path, folder, gname, cat in gen:
                seen.add(path)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if known.get(path) == (st.st_mtime, st.st_size):
                    continue
                con.execute("delete from faces where path=?", (path,))
                fmt = path.rsplit(".", 1)[-1].lower()
                stem = os.path.splitext(os.path.basename(path))[0]
                try:
                    h = sha1_file(path)
                    items = scan_file(path)
                    broken = 0
                except Exception as e:
                    h, broken = "", 1
                    items = [(0, dict(family=stem, style="?", weight=400, italic=0, var=0, mode="latin", sample=""))]
                    n_err += 1
                for idx, it in items:
                    fam = gname or it["family"] or stem
                    fid = hashlib.sha1(f"{path}|{idx}|{h}".encode()).hexdigest()[:16]
                    con.execute("insert or replace into faces values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                        fid, path, idx, src, folder, fam, fam.lower().strip(), it["style"], it["weight"], it["italic"],
                        it["var"], it["mode"], it["sample"], fmt, st.st_size, st.st_mtime, h, cat or "", None, broken))
                n_new += 1
                if n_new % 200 == 0:
                    con.commit(); state["detail"] = f"{n_new} archivos nuevos leídos"
        gone = [p for p in known if p not in seen]
        for p in gone:
            con.execute("delete from faces where path=?", (p,))
        con.commit()
        rebuild_families(con)
        con.close()
        set_meta("last_index", now_iso())
        state["status"] = "listo"; state["detail"] = f"+{n_new} / -{len(gone)} archivos, {n_err} dañados"
        log.info("index: new=%s gone=%s err=%s", n_new, len(gone), n_err)


def rebuild_families(con):
    faces = [dict(r) for r in con.execute("select * from faces where broken=0 order by source desc, path")]
    # exact duplicates: google copy wins, then first path
    first = {}
    dup_of = {}
    for f in sorted(faces, key=lambda f: (f["source"] != "google", f["path"])):
        k = (f["hash"], f["idx"])
        if k in first:
            dup_of[f["id"]] = first[k]
        else:
            first[k] = f["id"]
    groups = collections.defaultdict(list)
    for f in faces:
        f["dup_of"] = dup_of.get(f["id"])
        groups[(f["source"], f["family_key"])].append(f)
    gids = {k: hashlib.sha1(f"google:{k}".encode()).hexdigest()[:12] for (s, k) in groups if s == "google"}
    rows = []
    for (src, key), fs in groups.items():
        live = [f for f in fs if not f["dup_of"]] or fs
        rep = min(live, key=lambda f: (f["italic"], abs(f["weight"] - 400), f["var"], len(f["style"])))
        names = collections.Counter(f["family"] for f in fs)
        folders = collections.Counter(f["folder"] for f in fs)
        fid = hashlib.sha1(f"{src}:{key}".encode()).hexdigest()[:12]
        rows.append((fid, src, names.most_common(1)[0][0], key, folders.most_common(1)[0][0],
                     next((f["category"] for f in fs if f["category"]), ""), len(live), rep["id"],
                     ",".join(sorted({f["fmt"].upper() for f in live})),
                     1 if (src == "local" and key in gids) else 0, gids.get(key) if src == "local" else None,
                     rep["mode"], rep["sample"]))
    con.execute("begin")
    con.executemany("update faces set dup_of=? where id=?", [(dup_of.get(f["id"]), f["id"]) for f in faces])
    con.execute("delete from families")
    con.executemany("insert into families values(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.execute("commit")


# ---------------- google sync ----------------

def google_sync():
    state["status"] = "google"
    try:
        if not os.path.isdir(os.path.join(GOOGLE, ".git")):
            state["detail"] = "Descargando Google Fonts (primera vez, ~1 GB)…"
            tmp = GOOGLE + ".tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            subprocess.run(["git", "clone", "--depth", "1", "--single-branch",
                            "https://github.com/google/fonts.git", tmp], check=True, timeout=7200)
            os.rename(tmp, GOOGLE)
        else:
            state["detail"] = "Actualizando Google Fonts…"
            subprocess.run(["git", "-C", GOOGLE, "fetch", "--depth", "1", "origin", "main"], check=True, timeout=3600)
            subprocess.run(["git", "-C", GOOGLE, "reset", "--hard", "FETCH_HEAD"], check=True, timeout=600)
        set_meta("last_google", now_iso())
        state["error"] = None
    except Exception as e:
        state["error"] = f"Google sync: {e}"
        log.exception("google sync")


def warm_previews():
    con = db()
    ids = [r["rep_face"] for r in con.execute(
        "select f.rep_face from families f join faces x on x.id=f.rep_face where x.size>? or x.fmt in ('ttc','otc')",
        (PREVIEW_RAW_MAX,))]
    con.close()
    for i, fid in enumerate(ids):
        state["detail"] = f"Optimizando vistas previas {i}/{len(ids)}"
        try:
            preview_path(fid)
        except Exception:
            pass
    state["detail"] = ""


def worker():
    try:
        os.nice(10)
    except Exception:
        pass
    last_google_day = None
    index_all()
    while True:
        try:
            today = datetime.date.today()
            due = not os.path.isdir(os.path.join(GOOGLE, ".git")) or (
                datetime.datetime.now().hour >= SYNC_HOUR and last_google_day != today)
            if due:
                google_sync(); last_google_day = today
                index_all()
                warm_previews()
                state["status"] = "listo"
            else:
                index_all()
        except Exception as e:
            state["error"] = str(e); log.exception("worker")
        time.sleep(LOCAL_EVERY)


# ---------------- font files ----------------

def get_face(fid):
    con = db(); r = con.execute("select * from faces where id=?", (fid,)).fetchone(); con.close()
    if not r:
        raise HTTPException(404)
    return dict(r)


def full_path(fid):
    f = get_face(fid)
    if f["fmt"] in ("ttc", "otc"):
        out = os.path.join(CACHE, "ttc", fid + ".ttf")
        if not os.path.exists(out):
            from fontTools.ttLib import TTFont
            os.makedirs(os.path.dirname(out), exist_ok=True)
            tt = TTFont(f["path"], fontNumber=f["idx"]); tt.save(out + ".tmp"); os.rename(out + ".tmp", out)
        return out, "ttf"
    return f["path"], f["fmt"]


def preview_path(fid):
    f = get_face(fid)
    if f["size"] <= PREVIEW_RAW_MAX and f["fmt"] not in ("ttc", "otc"):
        return f["path"], f["fmt"]
    out = os.path.join(CACHE, "p", fid + ".woff2")
    if not os.path.exists(out):
        from fontTools import subset
        from fontTools.ttLib import TTFont
        os.makedirs(os.path.dirname(out), exist_ok=True)
        tt = TTFont(f["path"], fontNumber=f["idx"])
        uni = set(range(0x20, 0x250)) | set(range(0x2000, 0x2070)) | {0x20AC, 0x2122}
        uni |= {ord(c) for c in (f["sample"] or "")}
        opts = subset.Options(); opts.flavor = "woff2"; opts.layout_features = ["*"]; opts.name_IDs = ["*"]
        opts.notdef_outline = True; opts.hinting = False; opts.ignore_missing_unicodes = True
        s = subset.Subsetter(opts); s.populate(unicodes=uni); s.subset(tt)
        tt.flavor = "woff2"; tt.save(out + ".tmp"); os.rename(out + ".tmp", out)
    return out, "woff2"


CACHE_HDR = {"Cache-Control": "public, max-age=31536000, immutable", "Access-Control-Allow-Origin": "*"}

app = FastAPI(title="Bunker Fonts")


@app.on_event("startup")
def _start():
    init_db()
    threading.Thread(target=worker, daemon=True).start()


@app.get("/", response_class=HTMLResponse)
def home():
    return FileResponse(os.path.join(HERE, "static", "index.html"), headers={"Cache-Control": "no-cache"})


@app.get("/f/{fid}")
def font_full(fid: str):
    p, fmt = full_path(fid)
    return FileResponse(p, media_type=MIME.get(fmt, "application/octet-stream"), headers=CACHE_HDR)


@app.get("/p/{fid}")
def font_preview(fid: str):
    p, fmt = preview_path(fid)
    return FileResponse(p, media_type=MIME.get(fmt, "application/octet-stream"), headers=CACHE_HDR)


# ---------------- API ----------------

def fam_rows(where, args, order="f.family_key", offset=0, limit=60):
    con = db()
    total = con.execute(f"select count(*) from families f where {where}", args).fetchone()[0]
    rows = con.execute(f"""select f.*, x.style rep_style, (select 1 from favs where family_id=f.id) fav,
        (select 1 from active where family_id=f.id) act from families f join faces x on x.id=f.rep_face
        where {where} order by {order} limit ? offset ?""", args + [limit, offset]).fetchall()
    con.close()
    return {"total": total, "items": [dict(r) for r in rows]}


@app.get("/api/families")
def families(q: str = "", view: str = "all", folder: str = "", category: str = "", coll: int = 0,
             dups: int = 0, offset: int = 0, limit: int = 60):
    w, a = [], []
    if view == "google":
        w.append("f.source='google'")
    elif view == "mine":
        w.append("f.source='local' and f.dup_google=0")
    elif view == "favs":
        w.append("f.id in (select family_id from favs)")
    elif view == "active":
        w.append("f.id in (select family_id from active)")
    elif view == "dups":
        w.append("f.dup_google=1")
    if folder:
        w.append("f.source='local' and exists(select 1 from faces x where x.source='local' and x.family_key=f.family_key and x.folder=?)"); a.append(folder)
    if category:
        w.append("f.category=?"); a.append(category)
    if coll:
        w.append("f.id in (select family_id from coll_items where cid=?)"); a.append(coll)
    if not dups and view not in ("dups",) and not folder:
        w.append("f.dup_google=0")
    if q:
        w.append("f.family_key like ?"); a.append(f"%{q.lower().strip()}%")
    return fam_rows(" and ".join(w) or "1", a, offset=max(0, offset), limit=min(200, max(1, limit)))


@app.get("/api/family/{fid}")
def family(fid: str):
    con = db()
    f = con.execute("select * from families where id=?", (fid,)).fetchone()
    if not f:
        raise HTTPException(404)
    faces = [dict(r) for r in con.execute(
        "select id, path, idx, style, weight, italic, var, fmt, size, folder, mode, sample, dup_of from faces "
        "where source=? and family_key=? and broken=0 order by dup_of is not null, italic, weight, style",
        (f["source"], f["family_key"]))]
    for x in faces:
        x["file"] = os.path.basename(x.pop("path"))
    colls = [r[0] for r in con.execute("select cid from coll_items where family_id=?", (fid,))]
    out = dict(f); out["faces"] = faces; out["collections"] = colls
    out["fav"] = bool(con.execute("select 1 from favs where family_id=?", (fid,)).fetchone())
    out["act"] = bool(con.execute("select 1 from active where family_id=?", (fid,)).fetchone())
    if f["source"] == "google":
        out["local_copies"] = con.execute("select id from families where google_id=?", (fid,)).fetchone()
        out["local_copies"] = out["local_copies"][0] if out["local_copies"] else None
    con.close()
    return out


@app.get("/api/family/{fid}/zip")
def family_zip(fid: str):
    d = family(fid)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        names = set()
        for x in d["faces"]:
            if x["dup_of"]:
                continue
            p = get_face(x["id"])["path"]
            n = os.path.basename(p)
            if n not in names:
                names.add(n); z.write(p, n)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", d["family"]) or "font"
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{safe}.zip"'})


@app.get("/api/stats")
def stats():
    con = db()
    one = lambda s, *a: con.execute(s, a).fetchone()[0]
    out = {
        "all": one("select count(*) from families where dup_google=0"),
        "google": one("select count(*) from families where source='google'"),
        "mine": one("select count(*) from families where source='local' and dup_google=0"),
        "dups": one("select count(*) from families where dup_google=1"),
        "favs": one("select count(*) from favs"),
        "active": one("select count(*) from active"),
        "files": one("select count(*) from faces"),
        "folders": [dict(r) for r in con.execute(
            "select folder name, count(distinct family_key) n from faces where source='local' group by folder order by n desc")],
        "categories": [dict(r) for r in con.execute(
            "select category name, count(*) n from families where source='google' and category!='' group by category order by n desc")],
        "collections": [dict(r) for r in con.execute(
            "select c.id, c.name, (select count(*) from coll_items i where i.cid=c.id) n from collections c order by c.name")],
        "state": state,
    }
    con.close()
    return out


@app.post("/api/fav/{fid}")
def toggle_fav(fid: str):
    return _toggle("favs", fid)


@app.post("/api/active/{fid}")
def toggle_active(fid: str):
    return _toggle("active", fid)


def _toggle(table, fid):
    con = db()
    if con.execute(f"select 1 from {table} where family_id=?", (fid,)).fetchone():
        con.execute(f"delete from {table} where family_id=?", (fid,)); on = False
    else:
        con.execute(f"insert into {table} values(?,?)", (fid, time.time())); on = True
    con.commit(); con.close()
    return {"on": on}


@app.post("/api/collections")
def new_collection(body: dict = Body(...)):
    name = (body.get("name") or "").strip()[:60]
    if not name:
        raise HTTPException(400, "nombre vacío")
    con = db()
    con.execute("insert or ignore into collections(name, at) values(?,?)", (name, time.time()))
    cid = con.execute("select id from collections where name=?", (name,)).fetchone()[0]
    con.commit(); con.close()
    return {"id": cid}


@app.delete("/api/collections/{cid}")
def del_collection(cid: int):
    con = db(); con.execute("delete from coll_items where cid=?", (cid,)); con.execute("delete from collections where id=?", (cid,))
    con.commit(); con.close()
    return {"ok": True}


@app.post("/api/collections/{cid}/toggle/{fid}")
def coll_toggle(cid: int, fid: str):
    con = db()
    if con.execute("select 1 from coll_items where cid=? and family_id=?", (cid, fid)).fetchone():
        con.execute("delete from coll_items where cid=? and family_id=?", (cid, fid)); on = False
    else:
        con.execute("insert into coll_items values(?,?)", (cid, fid)); on = True
    con.commit(); con.close()
    return {"on": on}


@app.post("/api/collections/{cid}/activate")
def coll_activate(cid: int, on: int = 1):
    con = db()
    ids = [r[0] for r in con.execute("select family_id from coll_items where cid=?", (cid,))]
    for i in ids:
        if on:
            con.execute("insert or ignore into active values(?,?)", (i, time.time()))
        else:
            con.execute("delete from active where family_id=?", (i,))
    con.commit(); con.close()
    return {"n": len(ids)}


@app.post("/api/reindex")
def reindex():
    if index_lock.locked():
        return {"ok": False, "msg": "ya está indexando"}
    threading.Thread(target=index_all, daemon=True).start()
    return {"ok": True}


@app.get("/api/manifest")
def manifest():
    """Para el agente de escritorio: todas las fuentes activas con su URL y hash."""
    con = db()
    rows = con.execute("""select x.id, x.path, x.fmt, x.hash, x.idx, f.family, f.id fam from active a
        join families f on f.id=a.family_id join faces x on x.source=f.source and x.family_key=f.family_key
        where x.broken=0 and x.dup_of is null""").fetchall()
    con.close()
    out = []
    for r in rows:
        ext = "ttf" if r["fmt"] in ("ttc", "otc") else r["fmt"]
        base = os.path.splitext(os.path.basename(r["path"]))[0]
        name = f"{base}-{r['idx']}.{ext}" if r["fmt"] in ("ttc", "otc") else f"{base}.{ext}"
        out.append({"id": r["id"], "family": r["family"], "family_id": r["fam"], "file": name,
                    "hash": r["hash"], "url": f"/f/{r['id']}"})
    return {"generated": now_iso(), "fonts": out}
