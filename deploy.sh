#!/bin/sh
# Construye la imagen y (re)crea el contenedor bunker-fonts en el host ZimaOS vía Portainer.
set -e
cd "$(dirname "$0")"
set -a; . /opt/data/.env; set +a
P=https://100.124.180.112:9443/api/endpoints/3/docker; H="X-API-Key: $PORTAINER_API_KEY"
tar -czf /opt/data/cache/scratch/bf-ctx.tgz --exclude=__pycache__ Dockerfile app
curl -sk -H "$H" -H 'Content-Type: application/x-tar' -X POST "$P/build?t=bunker-fonts:latest&rm=1" \
  --data-binary @/opt/data/cache/scratch/bf-ctx.tgz | grep -o '"\(stream\|error\)":"[^"]*' | tail -3
curl -sk -H "$H" -X DELETE "$P/containers/bunker-fonts?force=1" >/dev/null || true
cat > /opt/data/cache/scratch/bf-create.json <<'EOF'
{"Image":"bunker-fonts:latest","ExposedPorts":{"8000/tcp":{}},
 "HostConfig":{"RestartPolicy":{"Name":"unless-stopped"},
  "PortBindings":{"8000/tcp":[{"HostPort":"8099"}]},
  "Binds":["/media/Safe-Storage/Zima Drive/Dropbox/Fonts:/fonts:ro","/media/Safe-Storage/bunker-fonts:/store"]}}
EOF
curl -sk -H "$H" -H 'Content-Type: application/json' -X POST "$P/containers/create?name=bunker-fonts" -d @/opt/data/cache/scratch/bf-create.json
curl -sk -H "$H" -X POST "$P/containers/bunker-fonts/start" && echo started
