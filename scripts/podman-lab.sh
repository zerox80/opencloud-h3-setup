#!/usr/bin/env bash
# Experimental, isolated Podman lab. This is not a production installer.
# Usage: ./opencloud-h3-podman-lab.sh /absolute/path/to/rpxy
# The supplied rpxy should be built with the desired H3 and Quinn forks.
set -Eeuo pipefail

if [[ $# -ne 1 ]] || [[ ! -x "$1" ]]; then
  echo "Usage: $0 /absolute/path/to/an/executable-rpxy-binary" >&2
  exit 2
fi
for cmd in podman openssl realpath; do
  command -v "$cmd" >/dev/null || { echo "Missing dependency: $cmd" >&2; exit 2; }
done
binary=$(realpath "$1")
lab=$(mktemp -d "${TMPDIR:-/tmp}/opencloud-h3-lab.XXXXXXXX")
chmod 755 "$lab"
pod="opencloud-h3-lab-$$"
nginx="${pod}-nginx"
router="${pod}-router"
quinn="${pod}-quinn"
cleanup() {
  podman pod rm -f -t 1 "$pod" >/dev/null 2>&1 || true
  rm -rf -- "$lab"
}
trap cleanup EXIT

# A disposable self-signed certificate is generated only for this private lab.
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$lab/key.pem" -out "$lab/cert.pem" \
  -days 1 -subj '/CN=cloud.dummy' \
  -addext 'subjectAltName=DNS:cloud.dummy,DNS:other.dummy' >/dev/null 2>&1
chmod 644 "$lab/key.pem"
cat > "$lab/nginx.conf" <<'CONF'
worker_processes 1;
pid /tmp/nginx-lab.pid;
events { worker_connections 128; }
http {
    access_log off;
    server {
        listen 127.0.0.1:24445;
        server_name cloud.dummy;
        return 200 'CLOUD_DUMMY';
    }
    server {
        listen 127.0.0.1:24443 quic reuseport;
        server_name other.dummy;
        ssl_certificate /lab/cert.pem;
        ssl_certificate_key /lab/key.pem;
        return 200 'OTHER_DUMMY';
    }
}
CONF
cat > "$lab/rpxy.toml" <<'CONF'
listen_port_tls = 24441
listen_address_v4 = '127.0.0.1'
listen_ipv6 = false
[apps.opencloud]
server_name = 'cloud.dummy'
tls = { tls_cert_path = '/lab/cert.pem', tls_cert_key_path = '/lab/key.pem' }
reverse_proxy = [{ upstream = [{ location = '127.0.0.1:24445' }] }]
[experimental.h3]
alt_svc_max_age = 60
CONF
cat > "$lab/l4.toml" <<'CONF'
listen_port = 24443
listen_address_v4 = '127.0.0.1'
listen_ipv6 = false
udp_idle_lifetime = 60
[protocols.opencloud]
protocol = 'quic'
target = ['127.0.0.1:24441']
server_names = ['cloud.dummy']
idle_lifetime = 60
[protocols.fallback]
protocol = 'quic'
target = ['127.0.0.1:24442']
idle_lifetime = 60
CONF

podman pod create --name "$pod" >/dev/null
podman run -d --name "$nginx" --pod "$pod" --security-opt label=disable \
  -v "$lab:/lab:ro" docker.io/library/nginx:1.29-alpine \
  nginx -g 'daemon off;' -c /lab/nginx.conf >/dev/null
podman run -d --name "$quinn" --pod "$pod" --security-opt label=disable \
  -v "$lab:/lab:ro" -v "$lab/rpxy.toml:/etc/rpxy.toml:ro" \
  -v "$binary:/rpxy/bin/rpxy:ro" docker.io/jqtype/rpxy:latest >/dev/null

request() {
  local host=$1 port=$2 expected=$3 value
  value=$(podman run --rm --pod "$pod" --entrypoint curl docker.io/ymuski/curl-http3:latest \
    --noproxy '*' --http3-only --max-time 10 -ksS \
    --resolve "$host:$port:127.0.0.1" "https://$host:$port/")
  [[ "$value" == "$expected" ]] || { echo "Unexpected response for $host: $value" >&2; return 1; }
  echo "PASS: $host:$port returned $expected over HTTP/3"
}
# Wait for both servers to finish starting.
for _ in {1..20}; do
  if request other.dummy 24443 OTHER_DUMMY >/dev/null 2>&1 && \
     request cloud.dummy 24441 CLOUD_DUMMY >/dev/null 2>&1; then break; fi
  sleep 1
done
request other.dummy 24443 OTHER_DUMMY
request cloud.dummy 24441 CLOUD_DUMMY

# Move the old dummy listener internally, then put the SNI router on its old port.
cp "$lab/nginx.conf" "$lab/nginx.conf.backup"
sed -i 's/127.0.0.1:24443 quic/127.0.0.1:24442 quic/' "$lab/nginx.conf"
podman exec "$nginx" nginx -t -c /lab/nginx.conf >/dev/null
podman exec "$nginx" nginx -s reload -c /lab/nginx.conf >/dev/null
podman run -d --name "$router" --pod "$pod" --security-opt label=disable \
  -v "$lab/l4.toml:/etc/rpxy-l4.toml:ro" docker.io/jqtype/rpxy-l4:latest >/dev/null
for _ in {1..20}; do
  if request cloud.dummy 24443 CLOUD_DUMMY >/dev/null 2>&1 && \
     request other.dummy 24443 OTHER_DUMMY >/dev/null 2>&1; then break; fi
  sleep 1
done
request cloud.dummy 24443 CLOUD_DUMMY
request other.dummy 24443 OTHER_DUMMY

# Restore the original dummy listener and verify the old service again.
podman stop --time 1 "$router" >/dev/null 2>&1
cp "$lab/nginx.conf.backup" "$lab/nginx.conf"
podman exec "$nginx" nginx -t -c /lab/nginx.conf >/dev/null
podman exec "$nginx" nginx -s reload -c /lab/nginx.conf >/dev/null
request other.dummy 24443 OTHER_DUMMY
printf '%s\n' 'PASS: the simulated rollback restored the original dummy listener.'
