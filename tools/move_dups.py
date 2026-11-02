# Mueve (no borra) duplicados fuera de la vista: corre en un contenedor con /media/Safe-Storage montado en /ss.
import os, sqlite3, collections, datetime, sys
SS = "/ss"
FONTS = SS + "/Zima Drive/Dropbox/Fonts"
STORE = SS + "/bunker-fonts"
DUP = FONTS + "/_Duplicados"
DRY = "--dry" in sys.argv
c = sqlite3.connect(STORE + "/fonts.db")
rows = c.execute("""select x.path, x.dup_of is not null, f.dup_google from faces x
  join families f on f.source=x.source and f.family_key=x.family_key where x.source='local'""").fetchall()
per = collections.defaultdict(list)
for p, exact, gdup in rows:
    per[p].append(bool(exact) or bool(gdup))
moves = [p for p, flags in per.items() if all(flags)]
log = []
for p in sorted(moves):
    src = FONTS + p[len("/fonts"):]
    rel = os.path.relpath(src, FONTS)
    dst = os.path.join(DUP, rel)
    if not os.path.exists(src):
        continue
    log.append(rel)
    if not DRY:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.rename(src, dst)
print("archivos movidos a _Duplicados:", len(log))
bk = STORE + "/_respaldo-FontBase"
for name in ("Providers", ".fontbase"):
    s = FONTS + "/FontBase/" + name
    if os.path.exists(s):
        print("respaldo:", name)
        if not DRY:
            os.makedirs(bk, exist_ok=True); os.rename(s, bk + "/" + name)
if not DRY and log:
    with open(DUP + "/LEEME.txt", "a") as f:
        f.write(f"Movidos por Bunker Fonts {datetime.datetime.now():%Y-%m-%d %H:%M}. Son copias de fuentes que ya existen en Google o copias exactas.\n"
                "Para regresar una, muévela a su carpeta original (misma ruta).\n\n" + "\n".join(log) + "\n")
