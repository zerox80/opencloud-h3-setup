#!/usr/bin/env bash
# Isolated pod: actual TCP/UDP 443, private CA, two client source IPs and spoofed headers.
set -Eeuo pipefail
if [[ $# != 1 || ! -x $1 ]]; then echo "Usage: $0 /absolute/path/to/udp-only-capable-rpxy" >&2; exit 2; fi
binary=$(realpath "$1"); repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
lab=$(mktemp -d "${TMPDIR:-/tmp}/traefik-direct-lab.XXXXXXXX"); chmod 755 "$lab"
name="traefik-direct-lab-$$"
TRAEFIK=docker.io/library/traefik@sha256:44b61e76a251fe586d17d881fc900452d0a7b4a4b08c97ce6eac86d45b4c8b9d
RPXY=docker.io/jqtype/rpxy@sha256:d6777c0800dbe036e9efcc6480ae7eada5ae215ed85ea0d19443ddee0b08ce81
CURL=docker.io/ymuski/curl-http3@sha256:9eadfcaa6e541ef61880795c3044f7a603dd62fe5be5a3b45b5e154025c783de
cleanup() { podman pod rm -f -t 1 "$name" >/dev/null 2>&1 || true; rm -rf -- "$lab"; }
trap cleanup EXIT
failure() {
  local result=$?
  podman logs "$name-quinn" 2>&1 || true
  podman logs "$name-edge" 2>&1 || true
  return "$result"
}
trap failure ERR
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj /CN=Direct-Quinn-Lab-CA \
  -keyout "$lab/ca.key" -out "$lab/ca.crt" -addext basicConstraints=critical,CA:TRUE >/dev/null 2>&1
openssl req -newkey rsa:2048 -nodes -subj /CN=cloud.dummy -keyout "$lab/privkey.pem" \
  -out "$lab/server.csr" >/dev/null 2>&1
printf '%s\n' 'subjectAltName=DNS:cloud.dummy,DNS:office.dummy' 'extendedKeyUsage=serverAuth' >"$lab/server.ext"
openssl x509 -req -in "$lab/server.csr" -CA "$lab/ca.crt" -CAkey "$lab/ca.key" -CAcreateserial \
  -days 2 -extfile "$lab/server.ext" -out "$lab/fullchain.pem" >/dev/null 2>&1
chmod 644 "$lab/privkey.pem"
cat >"$lab/echo.py" <<'PY'
from http.server import BaseHTTPRequestHandler,HTTPServer
import json
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
  self.wfile.write(json.dumps({'apps':['files'],'ip':self.headers.get('X-Real-Ip'), 'xff':self.headers.get('X-Forwarded-For'),
                              'forwarded':self.headers.get('Forwarded'),'global':self.headers.get('X-Global')}).encode())
 def log_message(self,*args): pass
HTTPServer(('127.0.0.1',9200),Handler).serve_forever()
PY
python3 - "$repo" "$lab" <<'PY'
import json,pathlib,sys
sys.path.insert(0,sys.argv[1]); import traefik_setup as tf
labels={
 'com.docker.compose.project':'lab','com.docker.compose.service':'opencloud',
 'traefik.http.routers.cloud.rule':'Host(`cloud.dummy`)', 'traefik.http.routers.cloud.tls':'true',
 'traefik.http.routers.cloud.entrypoints':'websecure','traefik.http.routers.cloud.service':'web',
 'traefik.http.routers.cloud.middlewares':'existing',
 'traefik.http.routers.protected.rule':'Host(`cloud.dummy`) && PathPrefix(`/protected`)',
 'traefik.http.routers.protected.tls':'true','traefik.http.routers.protected.entrypoints':'websecure',
 'traefik.http.routers.protected.service':'web','traefik.http.routers.protected.middlewares':'auth',
 'traefik.http.routers.restricted.rule':'Host(`cloud.dummy`) && PathPrefix(`/restricted`)',
 'traefik.http.routers.restricted.tls':'true','traefik.http.routers.restricted.entrypoints':'websecure',
 'traefik.http.routers.restricted.service':'web','traefik.http.routers.restricted.middlewares':'allow',
 'traefik.http.middlewares.existing.headers.customrequestheaders.X-Existing':'preserved',
 'traefik.http.middlewares.global.headers.customrequestheaders.X-Global':'preserved',
 'traefik.http.middlewares.auth.basicauth.users':'lab:unused-hash',
 'traefik.http.middlewares.allow.ipallowlist.sourcerange':'127.0.0.2/32',
 'traefik.http.middlewares.allow.ipallowlist.ipstrategy.depth':'1',
}
edge={'NetworkSettings':{'Ports':{'443/tcp':[{'HostPort':'443','HostIp':'0.0.0.0'}]},'Networks':{}},'Config':{'Labels':{}}}
project=object.__new__(tf.Compose);project.project='lab';project.service='traefik'
project.config={'services':{'traefik':{'command':['--entrypoints.websecure.address=:443','--entrypoints.websecure.http.middlewares=global@file']}}}
app=object.__new__(tf.Compose);app.project='lab';app.service='opencloud';app.config={'services':{'opencloud':{'labels':labels}}}
# The lab uses a file provider instead of exposing the user's Docker socket.
# Use the equivalent Docker reference for policy validation, then translate it
# back to the lab file provider in the generated dynamic configuration.
project.config['services']['traefik']['command'][1]='--entrypoints.websecure.http.middlewares=global@docker'
result,update=tf.direct_settings(edge,project,app,[{'Config':{'Labels':labels}}],'cloud.dummy')
http={'routers':{},'middlewares':{},'services':{'web':{'loadBalancer':{'servers':[{'url':'http://127.0.0.1:9200'}]}}}}
for key,value in result.items():
 if key.startswith('traefik.http.routers.'):
  name,field=key[len('traefik.http.routers.'):].split('.',1)
  router=http['routers'].setdefault(name,{})
  if field=='tls':router['tls']={}
  elif field in ('entrypoints','middlewares'):router['entryPoints' if field=='entrypoints' else field]=value.replace('@docker','@file').split(',')
  elif field=='priority':router[field]=int(value)
  else:router[field]=value
 elif key.startswith('traefik.http.middlewares.'):
  name,rest=key[len('traefik.http.middlewares.'):].split('.',1)
  parts=rest.split('.')
  aliases={'customresponseheaders':'customResponseHeaders','customrequestheaders':'customRequestHeaders',
           'basicauth':'basicAuth','ipallowlist':'ipAllowList','sourcerange':'sourceRange','ipstrategy':'ipStrategy'}
  current=http['middlewares'].setdefault(name,{})
  for part in parts[:-1]:current=current.setdefault(aliases.get(part.lower(),part),{})
  last=aliases.get(parts[-1].lower(),parts[-1])
  current[last]=int(value) if last=='depth' else value.split(',') if last in ('users','sourceRange') else value
http['routers']['office']={'rule':'Host(`office.dummy`)','entryPoints':['websecure'],'service':'web','tls':{}}
folder=pathlib.Path(sys.argv[2])
(folder/'dynamic.yaml').write_text(json.dumps({'http':http,'tls':{'certificates':[{'certFile':'/lab/fullchain.pem','keyFile':'/lab/privkey.pem'}]}}))
flags=update['command']+['--providers.file.filename=/lab/dynamic.yaml','--log.level=WARN']
(folder/'flags.json').write_text(json.dumps([x.replace('@docker','@file') for x in flags]))
(folder/'rpxy.toml').write_text(tf.proxy_config('cloud.dummy',19444,443,direct=True))
PY
mapfile -t flags < <(python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1]))))' "$lab/flags.json")
podman pod create --name "$name" --add-host cloud.dummy:127.0.0.1 >/dev/null
podman run -d --name "$name-backend" --pod "$name" --security-opt label=disable \
  -v "$lab:/lab:ro" docker.io/library/python:3.13-alpine python3 /lab/echo.py >/dev/null
podman run -d --name "$name-edge" --pod "$name" --security-opt label=disable \
  -v "$lab:/lab:ro" "$TRAEFIK" "${flags[@]}" >/dev/null
podman run -d --name "$name-quinn" --pod "$name" --security-opt label=disable \
  --user 0:0 --cap-drop ALL --cap-add NET_BIND_SERVICE --read-only --entrypoint /rpxy/bin/rpxy \
  -e RUST_LOG=warn -e SSL_CERT_FILE=/certs/ca.crt \
  -v "$lab:/certs:ro" -v "$lab/rpxy.toml:/etc/rpxy.toml:ro" -v "$binary:/rpxy/bin/rpxy:ro" \
  "$RPXY" --config /etc/rpxy.toml >/dev/null
request() {
  local protocol=$1 source=$2 host=$3 path=$4
  shift 4
  podman run --rm --pod "$name" --security-opt label=disable -v "$lab/ca.crt:/lab-ca.crt:ro" \
    --entrypoint curl "$CURL" --noproxy '*' "$protocol" --interface "$source" --max-time 8 -sS \
    --cacert /lab-ca.crt --resolve "$host:443:127.0.0.1" "$@" "https://$host$path"
}
for _ in {1..15}; do
  if request --http3-only 127.0.0.2 cloud.dummy /config.json 2>/dev/null | grep -q '"apps"'; then break; fi
  sleep 1
done
response=$(request --http3-only 127.0.0.2 cloud.dummy /config.json \
  -H 'X-Forwarded-For: 203.0.113.99' -H 'X-Real-IP: 203.0.113.99' -H 'Forwarded: for=203.0.113.99')
python3 - "$response" <<'PY'
import json,sys
try:data=json.loads(sys.argv[1])
except ValueError:raise AssertionError('Unexpected response: '+repr(sys.argv[1]))
assert data['ip']=='127.0.0.2',data
assert data['xff'].split(',')[0].strip()=='127.0.0.2',data
assert '203.0.113.99' not in sys.argv[1],data
assert data['global']=='preserved',data
PY
echo 'PASS: actual client source IP reaches backend; forged forwarding headers are discarded; global middleware survives'
request --http3-only 127.0.0.2 cloud.dummy /restricted -o /dev/null -w '%{http_code}' | grep -q '^200$'
request --http3-only 127.0.0.3 cloud.dummy /restricted -H 'X-Forwarded-For: 127.0.0.2' -o /dev/null -w '%{http_code}' | grep -q '^403$'
request --http3-only 127.0.0.2 cloud.dummy /protected -o /dev/null -w '%{http_code}' | grep -q '^401$'
request --http2 127.0.0.2 cloud.dummy /config.json -o /dev/null -w '%{http_code} %{http_version}' | grep -q '^200 2$'
request --http2 127.0.0.2 office.dummy / -o /dev/null -w '%{http_code} %{http_version}' | grep -q '^200 2$'
echo 'PASS: IP allowlist allows real client and rejects spoofed client; path auth and both Traefik TCP routes survive'
podman stop -t 1 "$name-quinn" >/dev/null
request --http2 127.0.0.2 cloud.dummy / -o /dev/null -w '%{http_code}' | grep -q '^200$'
echo 'PASS: stopping the direct UDP component leaves Traefik TCP working'
