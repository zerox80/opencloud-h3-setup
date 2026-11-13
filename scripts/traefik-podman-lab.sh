#!/usr/bin/env bash
# Real Traefik + forked Quinn routing with loopback-only high ports. No host service edits.
set -Eeuo pipefail
if [[ $# != 1 || ! -x $1 ]]; then echo "Usage: $0 /absolute/path/to/rpxy" >&2; exit 2; fi
binary=$(realpath "$1")
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
lab=$(mktemp -d "${TMPDIR:-/tmp}/traefik-quinn-lab.XXXXXXXX")
chmod 755 "$lab"
name="traefik-quinn-lab-$$"
traefik="$name-edge"; quinn="$name-quinn"; router="$name-router"; web="$name-web"
TRAEFIK=docker.io/library/traefik@sha256:44b61e76a251fe586d17d881fc900452d0a7b4a4b08c97ce6eac86d45b4c8b9d
RPXY=docker.io/jqtype/rpxy@sha256:d6777c0800dbe036e9efcc6480ae7eada5ae215ed85ea0d19443ddee0b08ce81
ROUTER=docker.io/jqtype/rpxy-l4@sha256:2fd0cb93958174095bb43c8cf0d0e2eb5a71ccd91e0b3cb026eab8b126adafb0
CURL=docker.io/ymuski/curl-http3@sha256:9eadfcaa6e541ef61880795c3044f7a603dd62fe5be5a3b45b5e154025c783de
cleanup() {
  podman rm -f -t 1 "$router" "$quinn" "$traefik" "$web" >/dev/null 2>&1 || true
  podman network rm "$name" >/dev/null 2>&1 || true
  rm -rf -- "$lab"
}
trap cleanup EXIT
python3 - <<'PY'
import socket
for kind, port in [(socket.SOCK_STREAM,24440),(socket.SOCK_STREAM,24441),(socket.SOCK_DGRAM,24441),(socket.SOCK_DGRAM,24442),(socket.SOCK_DGRAM,24443)]:
 s=socket.socket(socket.AF_INET,kind);s.bind(('127.0.0.1',port));s.close()
PY
# A disposable private CA is trusted only inside this lab's clients and proxy.
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj /CN=Traefik-Quinn-Lab-CA \
  -keyout "$lab/ca.key" -out "$lab/ca.crt" -addext basicConstraints=critical,CA:TRUE >/dev/null 2>&1
openssl req -newkey rsa:2048 -nodes -subj /CN=cloud.dummy -keyout "$lab/privkey.pem" \
  -out "$lab/server.csr" >/dev/null 2>&1
printf '%s\n' 'subjectAltName=DNS:cloud.dummy,DNS:other.dummy' 'extendedKeyUsage=serverAuth' >"$lab/server.ext"
openssl x509 -req -in "$lab/server.csr" -CA "$lab/ca.crt" -CAkey "$lab/ca.key" -CAcreateserial \
  -days 2 -extfile "$lab/server.ext" -out "$lab/fullchain.pem" >/dev/null 2>&1
chmod 644 "$lab/privkey.pem"
cat >"$lab/nginx.conf" <<'EOF'
events {}
http {
 access_log off;
 server { listen 80; location / { return 200 'DUMMY_BACKEND'; } }
}
EOF
cat >"$lab/dynamic.yml" <<EOF
tls:
  certificates:
    - certFile: /lab/fullchain.pem
      keyFile: /lab/privkey.pem
http:
  routers:
    cloud:
      rule: Host(\`cloud.dummy\`)
      service: web
      middlewares: [existing, h3]
      tls: {}
    protected:
      rule: Host(\`cloud.dummy\`) && PathPrefix(\`/protected\`)
      service: web
      middlewares: [auth]
      tls: {}
    other:
      rule: Host(\`other.dummy\`)
      service: web
      middlewares: [other]
      tls: {}
  services:
    web:
      loadBalancer:
        servers: [{url: 'http://$web:80'}]
  middlewares:
    existing:
      headers:
        customResponseHeaders: {X-Existing: preserved}
    h3:
      headers:
        customResponseHeaders: {Alt-Svc: 'h3=":443"; ma=60'}
    other:
      headers:
        customResponseHeaders: {X-Other: traefik}
    auth:
      basicAuth:
        users: ['test:unused-hash-rejects-unauthenticated']
EOF
python3 - "$repo" "$lab" <<'PY'
import sys,pathlib
sys.path.insert(0,sys.argv[1])
import traefik_setup
pathlib.Path(sys.argv[2],'rpxy.toml').write_text(traefik_setup.proxy_config('cloud.dummy',24440,24441))
PY
cat >"$lab/l4.toml" <<'EOF'
listen_port = 24443
listen_address_v4 = '127.0.0.1'
listen_ipv6 = false
[protocols.opencloud]
protocol = 'quic'
target = ['127.0.0.1:24441']
server_names = ['cloud.dummy']
[protocols.fallback]
protocol = 'quic'
target = ['127.0.0.1:24442']
EOF
podman network create "$name" >/dev/null
podman run -d --name "$web" --network "$name" --security-opt label=disable \
  -v "$lab/nginx.conf:/etc/nginx/nginx.conf:ro" docker.io/library/nginx:1.29-alpine >/dev/null
start_traefik() {
  podman run -d --name "$traefik" --network "$name" --security-opt label=disable \
    -p 127.0.0.1:24440:443/tcp -p "127.0.0.1:$1:443/udp" -v "$lab:/lab:ro" "$TRAEFIK" \
    --entrypoints.websecure.address=:443 --entrypoints.websecure.http3.advertisedport=24443 \
    --providers.file.filename=/lab/dynamic.yml --log.level=WARN >/dev/null
}
start_traefik 24443
podman run -d --name "$quinn" --network host --security-opt label=disable \
  --add-host cloud.dummy:127.0.0.1 -e LOG_LEVEL=warn \
  -v "$lab:/certs:ro" -v "$lab/ca.crt:/usr/local/share/ca-certificates/lab.crt:ro" \
  -v "$lab/rpxy.toml:/etc/rpxy.toml:ro" -v "$binary:/rpxy/bin/rpxy:ro" "$RPXY" >/dev/null
request() {
  local domain=$1 port=$2 protocol=$3 path=$4
  podman run --rm --network host --security-opt label=disable -v "$lab/ca.crt:/lab-ca.crt:ro" \
    --entrypoint curl "$CURL" --noproxy '*' "$protocol" --max-time 8 -sS \
    --cacert /lab-ca.crt --resolve "$domain:$port:127.0.0.1" -D - "https://$domain:$port$path"
}
ready() {
  local host=$1 port=$2
  for _ in {1..15}; do
    if request "$host" "$port" --http3-only / 2>/dev/null | grep -q DUMMY_BACKEND; then return; fi
    sleep 1
  done
  podman logs "$traefik"; podman logs "$quinn"
  echo "HTTP/3 not ready: $host:$port" >&2; return 1
}
ready other.dummy 24443
ready cloud.dummy 24441
echo 'PASS: native Traefik HTTP/3 and Quinn -> verified HTTPS -> Traefik both work'
podman rm -f -t 1 "$traefik" >/dev/null
start_traefik 24442
podman run -d --name "$router" --network host --security-opt label=disable \
  -v "$lab/l4.toml:/etc/rpxy-l4.toml:ro" "$ROUTER" >/dev/null
ready cloud.dummy 24443; ready other.dummy 24443
cloud=$(request cloud.dummy 24443 --http3-only /)
grep -qi '^x-existing: preserved' <<<"$cloud"
grep -qi '^alt-svc: h3=' <<<"$cloud"
other=$(request other.dummy 24443 --http3-only /)
grep -qi '^x-other: traefik' <<<"$other"
protected=$(request cloud.dummy 24443 --http3-only /protected)
grep -q '^HTTP/3 401' <<<"$protected"
tcp=$(request cloud.dummy 24440 --http2 /)
grep -q '^HTTP/2 200' <<<"$tcp"
echo 'PASS: shared UDP keeps both hostnames; middleware, HTTPS verification, auth and TCP fallback survive'
podman rm -f -t 1 "$router" "$traefik" >/dev/null
start_traefik 24443
ready other.dummy 24443
request cloud.dummy 24440 --http2 / | grep -q DUMMY_BACKEND
echo 'PASS: rollback restores the original Traefik UDP mapping and TCP route'
