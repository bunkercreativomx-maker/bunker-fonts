#!/bin/sh
# Uso: sh run_oneoff.sh script.py [args]  — corre un script python en un contenedor temporal con /media/Safe-Storage en /ss (rw).
set -e
set -a; . /opt/data/.env; set +a
P=https://100.124.180.112:9443/api/endpoints/3/docker; H="X-API-Key: $PORTAINER_API_KEY"
S=$(base64 -w0 "$1"); shift
BODY=$(python3 -c "import json,sys;print(json.dumps({'Image':'bunker-fonts:latest','Env':['S='+sys.argv[1]],'Entrypoint':['sh','-c'],'Cmd':['echo \$S | base64 -d > /tmp/s.py; python /tmp/s.py '+' '.join(sys.argv[2:])],'HostConfig':{'Binds':['/media/Safe-Storage:/ss']}}))" "$S" "$@")
curl -sk -H "$H" -X DELETE "$P/containers/bf-oneoff?force=1" >/dev/null || true
ID=$(curl -sk -H "$H" -H 'Content-Type: application/json' -X POST "$P/containers/create?name=bf-oneoff" -d "$BODY" | python3 -c "import sys,json;print(json.load(sys.stdin)['Id'])")
curl -sk -H "$H" -X POST "$P/containers/$ID/start"; curl -sk -m 1700 -H "$H" -X POST "$P/containers/$ID/wait"; echo
curl -sk -H "$H" "$P/containers/$ID/logs?stdout=1&stderr=1" | python3 -c "
import sys;b=sys.stdin.buffer.read();o=b'';i=0
while i+8<=len(b):
  n=int.from_bytes(b[i+4:i+8],'big');o+=b[i+8:i+8+n];i+=8+n
print(o.decode(errors='replace'))"
curl -sk -H "$H" -X DELETE "$P/containers/$ID?force=1" >/dev/null
