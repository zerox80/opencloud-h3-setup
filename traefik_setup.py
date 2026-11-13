"""Experimental Docker Compose Traefik adapter; public TCP stays with Traefik."""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

import setup as core

sys.path.insert(0, str(Path(__file__).resolve().parent / 'vendor'))
from ruamel.yaml import YAML

TIMER = Path('/etc/systemd/system/opencloud-h3-cert-sync.timer')
SERVICE = TIMER.with_suffix('.service')
MIDDLEWARE = 'opencloud-h3-experimental'
INTERNAL_PORT = 19444
INTERNAL_ENTRYPOINT = 'ocquinn'


def proxy_config(domain: str, upstream_port: int = 443, listen_port: int = core.H3_PORT, direct: bool = False) -> str:
    core.check_domain(domain)
    return f'''listen_port_tls = {listen_port}
h3_only = {'true' if direct else 'false'}
listen_address_v4 = '{'0.0.0.0' if direct and listen_port == 443 else '127.0.0.1'}'
listen_ipv6 = {'true' if direct and listen_port == 443 and core.socket.has_ipv6 else 'false'}
request_max_body_size = '64m'
redact_query_in_access_log = true
[apps.opencloud]
server_name = '{domain}'
tls = {{ tls_cert_path = '/certs/fullchain.pem', tls_cert_key_path = '/certs/privkey.pem' }}
reverse_proxy = [{{ upstream = [{{ location = '{domain}:{upstream_port}', tls = true }}] }}]
[experimental.h3]
alt_svc_max_age = 60
'''


def containers() -> list[dict]:
    ids = core.command('docker', 'ps', '-q').stdout.split()
    return json.loads(core.command('docker', 'inspect', *ids).stdout) if ids else []


def bindings(container: dict, protocol: str) -> list[tuple[str, dict]]:
    return [(key, value) for key, values in (container['NetworkSettings'].get('Ports') or {}).items()
            if key.endswith('/' + protocol) for value in (values or []) if value['HostPort'] == '443']


def edges(items: list[dict]) -> list[dict]:
    return [c for c in items if re.search(r'(?:^|/)traefik(?::|@|$)', c['Config']['Image'])
            and bindings(c, 'tcp')]


def detected() -> bool:
    if not shutil.which('docker'): return False
    return bool(edges(containers()))


class Compose:
    def __init__(self, container: dict):
        labels = container['Config'].get('Labels') or {}
        self.project = labels.get('com.docker.compose.project', '')
        self.service = labels.get('com.docker.compose.service', '')
        self.directory = labels.get('com.docker.compose.project.working_dir', '')
        self.files = [Path(p).resolve() for p in labels.get('com.docker.compose.project.config_files', '').split(',') if p]
        self.envfiles = labels.get('com.docker.compose.project.environment_file', '').split(',')
        if not self.project or not self.service or not Path(self.directory).is_dir() or not self.files:
            raise RuntimeError('Traefik and OpenCloud must be managed by Docker Compose with accessible project files.')
        if any(not p.is_file() for p in self.files):
            raise RuntimeError('A Compose source file is unavailable. Run setup on the Docker host.')
        self.original = {p: p.read_text() for p in self.files}
        self.config = self.render()
        if self.service not in self.config.get('services', {}): raise RuntimeError('Compose service could not be resolved.')
        config = self.config['services'][self.service]
        if config.get('scale', 1) != 1 or config.get('deploy', {}).get('replicas', 1) != 1:
            raise RuntimeError('Only one container per selected Compose service is supported.')
        image = core.command('docker', 'image', 'inspect', config['image'], '--format', '{{.Id}}').stdout.strip()
        if image != container['Image']:
            raise RuntimeError('A selected image tag changed locally. Reconcile the running Compose service first.')
        self.assert_current(container)

    def args(self, files=None) -> list[str]:
        args = ['docker', 'compose', '--project-directory', self.directory, '-p', self.project, '--profile', '*']
        for path in self.envfiles:
            if path: args += ['--env-file', path]
        for path in files or self.files: args += ['-f', str(path)]
        return args

    def run(self, *args: str, files=None, timeout=60) -> str:
        # Compose output can contain resolved secrets. Never include it in errors or logs.
        result = core.command(*self.args(files), *args, check=False, timeout=timeout)
        if result.returncode: raise RuntimeError(f'Compose {args[0]} failed for project {self.project}; inspect it locally.')
        return result.stdout

    def render(self, files=None) -> dict:
        return json.loads(self.run('config', '--format', 'json', files=files))

    def assert_current(self, container: dict) -> None:
        expected = (container['Config'].get('Labels') or {}).get('com.docker.compose.config-hash')
        lines = self.run('config', '--hash', self.service).splitlines()
        actual = next((line.split()[-1] for line in lines if line.split()[0] == self.service), None)
        if not expected or actual != expected:
            raise RuntimeError(f'Compose project {self.project}, service {self.service} has unapplied changes or a different environment. Reconcile it before setup.')

    def recreate(self) -> None:
        self.run('up', '-d', '--no-deps', '--no-build', '--pull', 'never', self.service, timeout=180)

    def saved(self) -> dict:
        return {'project': self.project, 'service': self.service, 'directory': self.directory,
                'files': list(map(str, self.files)), 'envfiles': self.envfiles}


def compose_from_saved(data: dict) -> Compose:
    obj = object.__new__(Compose)
    obj.__dict__.update(data)
    obj.files = list(map(Path, data['files']))
    return obj


def exact_routers(labels: dict, domain: str) -> list[str]:
    result = []
    for key, value in labels.items():
        match = re.fullmatch(r'traefik\.http\.routers\.([A-Za-z0-9_-]+)\.rule', key)
        if match and re.fullmatch(r'\s*Host\(\s*`' + re.escape(domain) + r'`\s*\)\s*', value, re.I):
            router = match.group(1)
            prefix = f'traefik.http.routers.{router}.tls'
            if labels.get(prefix, '').lower() == 'true' or any(k.startswith(prefix + '.') for k in labels):
                result.append(router)
    return result


def find_app(items: list[dict], domain: str) -> tuple[dict, list[str]]:
    found = [(c, exact_routers(c['Config'].get('Labels') or {}, domain)) for c in items]
    found = [(c, routers) for c, routers in found if routers]
    if len(found) != 1:
        raise RuntimeError('Expected one Compose container with an exact Host(`domain`) TLS router label. File-provider and compound routing rules are not supported automatically.')
    return found[0]


def advertised_labels(labels: dict, routers: list[str]) -> dict:
    result = dict(labels)
    prefix = f'traefik.http.middlewares.{MIDDLEWARE}.'
    if any(k.startswith(prefix) for k in labels): raise RuntimeError('The installer middleware name is already in use.')
    result[prefix + 'headers.customresponseheaders.Alt-Svc'] = 'h3=":443"; ma=60'
    for router in routers:
        key = f'traefik.http.routers.{router}.middlewares'
        current = labels.get(key, '').strip()
        # First in request order is last in response order, so this header survives other middleware.
        result[key] = MIDDLEWARE + '@docker' + (',' + current if current else '')
    return result


def move_port(value):
    if isinstance(value, str):
        match = re.fullmatch(r'(?:(?:0\.0\.0\.0|\[::\]):)?443:([0-9]+)/udp', value)
        if match: return f'127.0.0.1:{core.OLD_PORT}:{match.group(1)}/udp'
    elif isinstance(value, dict) and str(value.get('published')) == '443' and value.get('protocol', 'tcp') == 'udp':
        if value.get('host_ip', '') not in ('', '0.0.0.0', '::'): raise RuntimeError('A custom UDP bind address requires manual setup.')
        result = copy.deepcopy(value); result['host_ip'] = '127.0.0.1'; result['published'] = str(core.OLD_PORT)
        return result
    return None


def check_middleware(references: str, items: list[dict], seen=None) -> None:
    """Refuse middleware whose IP handling cannot be retained automatically."""
    seen = set() if seen is None else seen
    safe = {'headers', 'compress', 'basicauth', 'digestauth', 'stripprefix', 'stripprefixregex',
            'addprefix', 'replacepath', 'replacepathregex', 'redirectregex', 'redirectscheme',
            'buffering', 'retry', 'circuitbreaker', 'contenttype', 'errors', 'forwardauth'}
    for reference in references.split(','):
        if not reference.strip(): continue
        parts = reference.strip().split('@')
        if len(parts) > 2 or len(parts) == 2 and parts[1] != 'docker':
            raise RuntimeError('Direct mode needs Docker-label middleware; file/provider/plugin policies need manual integration.')
        name = parts[0]
        if name in seen: raise RuntimeError('A middleware chain contains a cycle.')
        prefix = f'traefik.http.middlewares.{name}.'.lower()
        definitions = [{k[len(prefix):].lower(): v for k, v in (item['Config'].get('Labels') or {}).items()
                        if k.lower().startswith(prefix)} for item in items]
        definitions = [d for d in definitions if d]
        if len(definitions) != 1: raise RuntimeError(f'Middleware {name} could not be resolved unambiguously.')
        definition = definitions[0]; kinds = {key.split('.')[0] for key in definition}
        if kinds == {'chain'}:
            check_middleware(definition.get('chain.middlewares', ''), items, seen | {name})
        elif kinds == {'ipallowlist'}:
            if definition.get('ipallowlist.ipstrategy.depth') != '1':
                raise RuntimeError(f'Middleware {name} uses the TCP peer IP. Direct mode requires a separately reviewed IP strategy; no policy was changed.')
        elif kinds in ({'ratelimit'}, {'inflightreq'}):
            kind = next(iter(kinds))
            if definition.get(kind + '.sourcecriterion.ipstrategy.depth') != '1':
                raise RuntimeError(f'Rate-limit middleware {name} needs a reviewed forwarded-IP strategy. No policy was changed.')
        elif not kinds or not kinds.issubset(safe):
            raise RuntimeError(f'Middleware {name} needs manual review before placing another proxy in front of it.')
        if any(key.startswith('headers.customrequestheaders.') and key.rsplit('.', 1)[-1] in ('x-forwarded-for', 'x-real-ip', 'forwarded') for key in definition):
            raise RuntimeError(f'Middleware {name} overrides client-IP headers; refusing automatic integration.')


def direct_settings(edge: dict, edge_project: Compose, app_project: Compose, items: list[dict], domain: str) -> tuple[dict, dict]:
    config = edge_project.config['services'][edge_project.service]
    args = config.get('command')
    if not isinstance(args, list) or not args or any(not a.startswith('--') or '=' not in a for a in args):
        raise RuntimeError('Automatic direct mode currently supports Traefik static settings as --option=value Compose command arguments.')
    flags = {arg[2:].split('=', 1)[0].lower(): arg.split('=', 1)[1] for arg in args}
    if 'configfile' in flags or any(k.startswith('TRAEFIK_') for k in config.get('environment', {})):
        raise RuntimeError('File/environment-based Traefik static settings need manual integration for the private trusted entry point.')
    if any(k.startswith(f'entrypoints.{INTERNAL_ENTRYPOINT}.') for k in flags):
        raise RuntimeError('The private Quinn entry point name is already in use.')
    labels = app_project.config['services'][app_project.service].get('labels', {})
    roots = exact_routers(labels, domain)
    entrypoints = {value.strip() for name in roots for value in labels.get(f'traefik.http.routers.{name}.entrypoints', '').split(',') if value.strip()}
    if len(entrypoints) != 1:
        raise RuntimeError('OpenCloud needs an explicit single public Traefik entrypoints router label for automatic direct mode.')
    public = entrypoints.pop().lower()
    container_ports = {key.split('/')[0] for key, _ in bindings(edge, 'tcp')}
    address = flags.get(f'entrypoints.{public}.address', '')
    if not any(address in (f':{port}', f'0.0.0.0:{port}', f'[::]:{port}') for port in container_ports):
        raise RuntimeError('The selected OpenCloud entry point does not match the published TCP 443 listener.')
    globals_middleware = flags.get(f'entrypoints.{public}.http.middlewares', '')
    check_middleware(globals_middleware, items)
    result = advertised_labels(labels, roots)
    for item in items:
        for key, rule in (item['Config'].get('Labels') or {}).items():
            if not key.startswith('traefik.http.routers.') or not key.endswith('.rule'): continue
            if 'HostRegexp(' in rule:
                raise RuntimeError('Wildcard Traefik routers need manual policy review before direct mode.')
            if domain in rule and (item['Config'].get('Labels') or {}).get('com.docker.compose.project') != app_project.project:
                raise RuntimeError('A matching route is defined in another project; integrate its policy manually.')
            if domain in rule and (item['Config'].get('Labels') or {}).get('com.docker.compose.service') != app_project.service:
                raise RuntimeError('A matching route is defined in another service; integrate its policy manually.')
    # Clone every matching route, including path-specific authentication routes.
    # Cloning only the exact root Host router could bypass a protected path.
    for key, rule in labels.items():
        match = re.fullmatch(r'traefik\.http\.routers\.([A-Za-z0-9_-]+)\.rule', key)
        if not match or domain not in rule: continue
        name = match.group(1); prefix = f'traefik.http.routers.{name}.'
        bound = labels.get(prefix + 'entrypoints', '')
        if bound and public not in [x.strip().lower() for x in bound.split(',')]: continue
        hosts = re.findall(r'Host\(\s*`([^`]+)`\s*\)', rule)
        if not hosts or any(host != domain for host in hosts) or '!' in rule or '||' in rule:
            raise RuntimeError('The selected domain has a complex route needing manual integration.')
        if labels.get(prefix + 'tls', '').lower() != 'true' and not any(k.startswith(prefix + 'tls.') for k in labels):
            continue  # Plain-HTTP redirect routers are not part of the HTTPS path.
        check_middleware(labels.get(prefix + 'middlewares', ''), items)
        clone = f'traefik.http.routers.och3-{name}.'
        if any(k.startswith(clone) for k in labels): raise RuntimeError('The private Quinn router name is already in use.')
        for old, value in labels.items():
            if old.startswith(prefix): result[clone + old[len(prefix):]] = value
        result[clone + 'entrypoints'] = INTERNAL_ENTRYPOINT
        priority = int(labels.get(prefix + 'priority', '0')) or len(rule.encode())
        result[clone + 'priority'] = str(priority + 1)
        # Middleware only adds a response header; existing request controls remain.
        value = labels.get(prefix + 'middlewares', '')
        result[clone + 'middlewares'] = MIDDLEWARE + '@docker' + (',' + value if value else '')
    gateways = {n.get('Gateway', '') for n in edge['NetworkSettings'].get('Networks', {}).values()}
    trusted = ['127.0.0.1/32', '::1/128']
    import ipaddress
    for gateway in gateways - {''}:
        ip = ipaddress.ip_address(gateway); trusted.append(f'{ip}/{ip.max_prefixlen}')
    extra = [f'--entrypoints.{INTERNAL_ENTRYPOINT}.address=:{INTERNAL_PORT}',
             f'--entrypoints.{INTERNAL_ENTRYPOINT}.forwardedheaders.trustedips=' + ','.join(trusted)]
    # Preserve entry-point HTTP middleware/TLS policies on the private route.
    for key, value in flags.items():
        prefix = f'entrypoints.{public}.http.'
        if key.startswith(prefix): extra.append(f'--entrypoints.{INTERNAL_ENTRYPOINT}.http.' + key[len(prefix):] + '=' + value)
    return result, {'command': args + extra}


def prepare_changes(projects: list[Compose], app: Compose, edge: Compose, labels: dict, occupied: bool,
                    edge_update: dict | None = None) -> dict[Path, str]:
    originals = {p: text for c in projects for p, text in c.original.items()}
    yaml = YAML(); yaml.preserve_quotes = True
    docs = {p: yaml.load(text) for p, text in originals.items()}
    before = copy.deepcopy(docs)
    owners = [p for p in app.files if isinstance(docs[p], dict) and app.service in docs[p].get('services', {})]
    if not owners: raise RuntimeError('OpenCloud service is not defined in the discovered Compose files.')
    service = docs[owners[-1]]['services'][app.service]
    current = service.get('labels') or {}
    if isinstance(current, list): current = dict(x.split('=', 1) if '=' in x else (x, '') for x in current)
    if not isinstance(current, dict): raise RuntimeError('Unsupported Compose labels format.')
    current = copy.deepcopy(current)
    resolved = app.config['services'][app.service].get('labels', {})
    for key, value in labels.items():
        if resolved.get(key) != value: current[key] = value.replace('$', '$$')
    service['labels'] = current
    if edge_update:
        target = next((p for p in reversed(edge.files) if edge.service in docs[p].get('services', {})), None)
        if target is None: raise RuntimeError('Traefik Compose service was not found.')
        # The effective command replaces earlier commands. The new port is unique
        # and therefore merges without altering any existing published port.
        docs[target]['services'][edge.service]['command'] = edge_update['command']
        docs[target]['services'][edge.service].setdefault('ports', []).append(f'127.0.0.1:{INTERNAL_PORT}:{INTERNAL_PORT}/tcp')
    changed_ports = 0
    if occupied:
        for path in edge.files:
            source = docs[path].get('services', {}).get(edge.service, {})
            for i, value in enumerate(source.get('ports', [])):
                moved = move_port(value)
                if moved is not None:
                    source['ports'][i] = moved; changed_ports += 1
        if changed_ports != 1:
            raise RuntimeError('Expected one literal UDP 443 Compose port binding. Variable port mappings and multiple definitions require manual setup.')
    changes = {}
    for path, document in docs.items():
        if document == before[path]: continue
        stream = io.StringIO(); yaml.dump(document, stream)
        text = stream.getvalue()
        if text != originals[path]: changes[path] = text
    # Validate the effective Compose model. Anchors, interpolation or overrides must
    # not change unrelated services, ports, images, middleware or environment.
    paths = {}
    try:
        for path, text in changes.items():
            fd, name = tempfile.mkstemp(prefix='.opencloud-h3-check-', suffix='.yaml', dir=path.parent)
            with os.fdopen(fd, 'w') as f: f.write(text)
            paths[path] = Path(name)
        for project in projects:
            expected = copy.deepcopy(project.config)
            services = expected['services']
            if project.project == app.project:
                services[app.service]['labels'] = labels
            if occupied and project.project == edge.project:
                count = 0
                for port in services[edge.service].get('ports', []):
                    if str(port.get('published')) == '443' and port.get('protocol', 'tcp') == 'udp':
                        port['published'] = str(core.OLD_PORT); port['host_ip'] = '127.0.0.1'; count += 1
                if count != 1: raise RuntimeError('Unsupported resolved UDP mapping.')
            if edge_update and project.project == edge.project:
                services[edge.service]['command'] = edge_update['command']
                services[edge.service].setdefault('ports', []).append({'target': INTERNAL_PORT, 'published': str(INTERNAL_PORT),
                                                                      'host_ip': '127.0.0.1', 'protocol': 'tcp', 'mode': 'ingress'})
            actual = project.render([paths.get(p, p) for p in project.files])
            if actual != expected:
                raise RuntimeError('The Compose edit affects more than the reviewed routing, command or port changes. No configuration was changed.')
    finally:
        for path in paths.values(): path.unlink(missing_ok=True)
    return changes


def acme_candidates(edge: dict) -> list[Path]:
    candidates = []
    for mount in edge.get('Mounts', []):
        source = Path(mount.get('Source', ''))
        if source.is_file() and source.suffix == '.json': candidates.append(source)
        elif source.is_dir() and str(source) != '/':
            candidates.extend(source.glob('*.json'))
            candidates.extend(source.glob('*/*.json'))
    return sorted(set(p.resolve() for p in candidates))


def certificate(source: dict, domain: str) -> tuple[bytes, bytes]:
    if source['kind'] == 'pem':
        cert, key = Path(source['cert']).read_bytes(), Path(source['key']).read_bytes()
    else:
        store = json.loads(Path(source['path']).read_text())
        if not isinstance(store, dict): raise ValueError('Not a Traefik ACME certificate store.')
        choices = []
        for resolver in store.values():
            if not isinstance(resolver, dict): continue
            for entry in resolver.get('Certificates') or []:
                if not isinstance(entry, dict) or not isinstance(entry.get('domain'), dict): continue
                names = [entry.get('domain', {}).get('main', '')] + (entry.get('domain', {}).get('sans') or [])
                if any(name == domain or (name.startswith('*.') and domain.partition('.')[2] == name[2:]) for name in names):
                    choices.append(entry)
        if len(choices) != 1: raise RuntimeError('Expected one matching certificate in the selected Traefik ACME store.')
        cert = base64.b64decode(choices[0]['certificate'], validate=True)
        key = base64.b64decode(choices[0]['key'], validate=True)
    # Check the actual X.509 SAN, expiry and key pair; ACME metadata alone is insufficient.
    with tempfile.TemporaryDirectory(prefix='opencloud-h3-cert-') as directory:
        folder = Path(directory); c = folder / 'cert'; k = folder / 'key'
        c.write_bytes(cert); k.write_bytes(key); k.chmod(0o600)
        for option, value in (('-checkhost', domain), ('-checkend', '86400')):
            core.command('openssl', 'x509', '-in', str(c), '-noout', option, value)
        pub = core.command('openssl', 'x509', '-in', str(c), '-pubkey', '-noout').stdout
        result = subprocess.run(['openssl', 'pkey', '-pubin', '-outform', 'DER'], input=pub.encode(), capture_output=True, check=True)
        keypub = subprocess.run(['openssl', 'pkey', '-in', str(k), '-pubout', '-outform', 'DER'], capture_output=True, check=True)
        if result.stdout != keypub.stdout: raise RuntimeError('Certificate and private key do not match.')
    return cert, key


def find_certificate(edge: dict, domain: str) -> dict:
    candidates = [{'kind': 'acme', 'path': str(p)} for p in acme_candidates(edge)]
    candidates += [{'kind': 'pem', 'cert': str(p), 'key': str(p.parent / 'privkey.pem')}
                   for p in Path('/etc/letsencrypt/live').glob('*/fullchain.pem')]
    for source in candidates:
        try: certificate(source, domain)
        except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError): continue
        print(f'Found and verified an existing HTTPS certificate for {domain}.')
        return source
    path = Path(input('Certificate not found automatically. Absolute path to Traefik acme.json (Enter to cancel): ').strip())
    if not path.is_absolute(): raise RuntimeError('Setup cancelled; a valid certificate source is required.')
    source = {'kind': 'acme', 'path': str(path.resolve())}; certificate(source, domain)
    return source


def sync_certificate(state: Path = core.STATE, restart=True) -> None:
    manifest = json.loads((state / 'manifest.json').read_text())
    if manifest.get('backend') != 'traefik' or (state / 'ROLLED_BACK').exists():
        raise RuntimeError('No active Traefik certificate sync configuration.')
    cert, key = certificate(manifest['certificate_source'], manifest['domain'])
    folder = state / 'certs'
    if (folder / 'fullchain.pem').exists() and (folder / 'privkey.pem').exists():
        unchanged = (folder / 'fullchain.pem').read_bytes() == cert and (folder / 'privkey.pem').read_bytes() == key
        applied = state / 'certificate-applied.sha256'
        fingerprint = hashlib.sha256(cert + key).hexdigest()
        if unchanged and applied.is_file() and applied.read_text() == fingerprint: return
    folder.mkdir(mode=0o750, exist_ok=True); os.chown(folder, 0, 900)
    for name, data, mode in (('fullchain.pem', cert, 0o644), ('privkey.pem', key, 0o600)):
        path = folder / (name + '.new'); path.write_bytes(data); path.chmod(mode); os.chown(path, 900, 900)
    os.replace(folder / 'fullchain.pem.new', folder / 'fullchain.pem')
    os.replace(folder / 'privkey.pem.new', folder / 'privkey.pem')
    if restart: core.command('docker', 'restart', core.RPXY, timeout=60)
    (state / 'certificate-applied.sha256').write_text(hashlib.sha256(cert + key).hexdigest())


def rollback(state: Path) -> None:
    manifest = json.loads((state / 'manifest.json').read_text())
    for item in manifest['files']:
        path = Path(item['path'])
        if not path.is_file() or core.digest(path) not in {item['original_sha256'], item['installed_sha256']}:
            raise RuntimeError(f'{path} changed since setup. Restore the saved backup manually.')
    if manifest.get('sync_created'):
        for name, expected in manifest.get('unit_hashes', {}).items():
            path = TIMER.parent / name
            if path.exists() and core.digest(path) != expected:
                raise RuntimeError(f'{path} changed since setup; refusing to remove it.')
        core.command('systemctl', 'disable', '--now', TIMER.name, check=False)
        core.command('systemctl', 'stop', SERVICE.name, check=False)
        for path in (TIMER, SERVICE): path.unlink(missing_ok=True)
        core.command('systemctl', 'daemon-reload')
    for name in (core.STAGE, core.ROUTER, core.RPXY): core.docker_remove(name)
    for item in manifest['files']:
        shutil.copy2(state / 'backup' / item['backup'], item['path'])
    # A recreate can fail halfway. Restore both selected services even if the
    # configuration files have already been restored by an earlier rollback.
    if manifest.get('compose_started'):
        for project in manifest['projects']: compose_from_saved(project).recreate()
    for path in (state / 'certs').glob('privkey.pem*'): path.unlink()
    (state / 'ROLLED_BACK').write_text('The original Compose configuration was restored.\n')
    print(f'Rollback complete. Backups retained in {state / "backup"}.')


def install(shared_router: bool = False) -> None:
    if os.geteuid() != 0: raise RuntimeError('Run setup as root with sudo on the Docker host.')
    if core.platform.machine().lower() not in ('x86_64', 'amd64'): raise RuntimeError('The bundled binary requires Linux x86-64.')
    for name in ('docker', 'openssl', 'curl', 'ss', 'systemctl'):
        if not shutil.which(name): raise RuntimeError(f'Missing required program: {name}')
    endpoint = os.environ.get('DOCKER_HOST') or core.command('docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}').stdout.strip()
    if not endpoint.startswith('unix://'): raise RuntimeError('Run this installer on the local Docker host using its Unix socket.')
    if shutil.which('getenforce') and core.command('getenforce').stdout.strip() == 'Enforcing':
        raise RuntimeError('SELinux enforcing mode is not supported by this experimental installer.')
    info = json.loads(core.command('docker', 'info', '--format', '{{json .}}').stdout)
    if any('rootless' in s for s in info.get('SecurityOptions', [])):
        raise RuntimeError('This mode requires rootful Docker on the local host.')
    if info.get('Swarm', {}).get('LocalNodeState') == 'active': raise RuntimeError('Docker Swarm is not supported.')
    if not shared_router and int(info.get('ServerVersion', '0').split('.')[0]) < 28:
        raise RuntimeError('Direct mode requires Docker 28+ for loopback-only publication of the trusted internal HTTPS port.')
    core.command('systemctl', 'is-system-running', check=False)
    if not Path('/run/systemd/system').is_dir(): raise RuntimeError('Certificate renewal sync requires systemd.')
    previous = core.check_previous_attempt()
    if TIMER.exists() or SERVICE.exists(): raise RuntimeError('Certificate sync units already exist.')
    for name in (core.RPXY, core.ROUTER, core.STAGE):
        if core.command('docker', 'inspect', name, check=False).returncode == 0: raise RuntimeError(f'Container {name} already exists.')
    items = containers(); candidates = edges(items)
    if len(candidates) != 1: raise RuntimeError('Expected one Traefik container publishing public TCP 443.')
    edge = candidates[0]
    if edge['HostConfig'].get('NetworkMode') == 'host': raise RuntimeError('Host-network Traefik is not supported automatically.')
    if any(b['HostIp'] not in ('', '0.0.0.0', '::') for _, b in bindings(edge, 'tcp')):
        raise RuntimeError('Traefik must publish TCP 443 on all host interfaces.')
    suggestion = ''
    for item in items:
        if 'opencloud' not in item['Config']['Image'].lower(): continue
        for key, value in (item['Config'].get('Labels') or {}).items():
            match = re.fullmatch(r'Host\(`([^`]+)`\)', value)
            if key.endswith('.rule') and match: suggestion = match.group(1)
    print('EXPERIMENTAL: Traefik Docker Compose + your bundled Quinn HTTP/3 component.')
    print('Public TCP 443 stays with Traefik. Only the selected OpenCloud hostname uses Quinn.')
    if shared_router: print('EXPLICIT SHARED-ROUTER MODE: original UDP client IPs are lost; QUIC ECH is unsupported.')
    else: print('Direct UDP mode preserves the peer IP at Quinn; the private Traefik entry point accepts its sanitized forwarding headers.')
    domain = core.prompt_domain('OpenCloud domain', suggestion)
    app_container, routers = find_app(items, domain)
    for selected_container in (edge, app_container):
        selected_labels = selected_container['Config'].get('Labels') or {}
        if sum(all((c['Config'].get('Labels') or {}).get(k) == selected_labels.get(k)
                   for k in ('com.docker.compose.project', 'com.docker.compose.service')) for c in items) != 1:
            raise RuntimeError('A selected Compose service has multiple running containers.')
    edge_project, app_project = Compose(edge), Compose(app_container)
    if edge_project.project == app_project.project and edge_project.files != app_project.files:
        raise RuntimeError('Services in the same Compose project were started with different source files.')
    projects = [edge_project] if edge_project.project == app_project.project else [edge_project, app_project]
    # Keep both service objects for recreation even when their source files are shared.
    selected = [edge_project, app_project] if edge_project.service != app_project.service or edge_project.project != app_project.project else [edge_project]
    occupied = bool(bindings(edge, 'udp'))
    for item in items:
        if item['Id'] != edge['Id'] and bindings(item, 'udp'): raise RuntimeError('Another container publishes UDP 443; refusing to change it.')
    listeners = core.udp443_listeners()
    if not shared_router and (occupied or listeners):
        raise RuntimeError('UDP 443 is already occupied. Direct mode stops without changing the existing service. Free UDP 443 or use a separate public IP; --traefik-shared-router explicitly selects the older experimental mode that loses client IPs.')
    if listeners and (not occupied or any('docker-proxy' not in line for line in listeners.splitlines())):
        raise RuntimeError('UDP 443 has an unsupported owner. No listener will be killed or changed.')
    fallback = ''
    if occupied:
        suggestion = next((name for item in items for key, value in (item['Config'].get('Labels') or {}).items()
                           if key.endswith('.rule') for name in re.findall(r'Host\(`([^`]+)`\)', value)
                           if name != domain and core.NAME.fullmatch(name)), '')
        value = input(f'Another existing Traefik HTTP/3 domain to test [{suggestion or "Enter: test the existing OpenCloud Traefik endpoint"}]: ').strip()
        fallback = core.check_domain(value or suggestion) if value or suggestion else domain
    binary = core.find_binary(); source = find_certificate(edge, domain)
    if shared_router: core.ports_free()
    else:
        for kind, port in ((core.socket.SOCK_STREAM, INTERNAL_PORT), (core.socket.SOCK_DGRAM, core.H3_PORT)):
            sock = core.socket.socket(core.socket.AF_INET, kind)
            try: sock.bind(('127.0.0.1', port))
            finally: sock.close()
    # Verify that localhost TCP 443 really reaches this OpenCloud installation.
    check = core.command('curl', '--noproxy', '*', '--max-time', '15', '-fsS', '--resolve',
                         f'{domain}:443:127.0.0.1', f'https://{domain}/config.json').stdout
    if not isinstance(json.loads(check).get('apps'), list): raise RuntimeError('The selected HTTPS route does not return an OpenCloud config.json.')
    if shared_router:
        labels = advertised_labels(app_project.config['services'][app_project.service].get('labels', {}), routers)
        edge_update = None
    else: labels, edge_update = direct_settings(edge, edge_project, app_project, items, domain)
    changes = prepare_changes(projects, app_project, edge_project, labels, occupied, edge_update)
    print('\nFiles to back up and update:')
    for path in changes: print('  ' + str(path))
    if occupied: print('Traefik UDP 443 moves to localhost:18443; other existing HTTP/3 domains keep their Traefik fallback.')
    if not shared_router: print('Quinn binds UDP 443 directly. A new trusted Traefik HTTPS port is published only on localhost:19444.')
    print('Only OpenCloud gets the new Alt-Svc middleware; its existing middleware chain is preserved.')
    print('Recreating the selected Compose services briefly interrupts their connections, including all Traefik sites.')
    print('A systemd timer checks for renewed certificates every 5 minutes.')
    if input('Type APPLY to start the experimental setup: ').strip() != 'APPLY': print('No changes made.'); return
    # Recheck before the first mutation, including the original Compose runtime hashes.
    fresh = {c['Id']: c for c in containers()}
    for project, container in ((edge_project, edge), (app_project, app_container)):
        if container['Id'] not in fresh: raise RuntimeError('A selected container changed while setup was being prepared.')
        project.assert_current(fresh[container['Id']])
    for p, text in {p: t for c in projects for p, t in c.original.items()}.items():
        if p.read_text() != text: raise RuntimeError(f'{p} changed while setup was being prepared.')
    if previous: core.STATE.rename(core.STATE.with_name(core.STATE.name + '.rolled-back-' + str(time.time_ns())))
    state = core.STATE; state.mkdir(mode=0o700); backup = state / 'backup'; backup.mkdir(mode=0o700)
    manifest = {'backend': 'traefik', 'direct': not shared_router, 'domain': domain, 'certificate_source': source, 'files': [],
                'projects': [c.saved() for c in selected], 'compose_started': False, 'sync_created': False}
    def save(): (state / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    for i, (p, text) in enumerate(changes.items()):
        name = f'{i}.yaml'; shutil.copy2(p, backup / name)
        manifest['files'].append({'path': str(p), 'backup': name, 'original_sha256': core.digest(p),
                                  'installed_sha256': hashlib.sha256(text.encode()).hexdigest()})
    save()
    try:
        sync_certificate(state, restart=False)
        shutil.copyfile(binary, state / 'rpxy-bin'); (state / 'rpxy-bin').chmod(0o755)
        # DNS for this one hostname resolves to the unchanged local Traefik TCP
        # listener. TLS SNI/verification and the original router/middleware remain in use.
        (state / 'rpxy.toml').write_text(proxy_config(domain, 443 if shared_router else INTERNAL_PORT,
                                                    core.H3_PORT if shared_router else 443, direct=not shared_router))
        (state / 'direct-stage.toml').write_text(proxy_config(domain, INTERNAL_PORT, core.H3_PORT, direct=True))
        def router_config(port, target):
            text = f"listen_port = {port}\nlisten_address_v4 = '{'127.0.0.1' if port != 443 else '0.0.0.0'}'\nlisten_ipv6 = {'true' if port == 443 and core.socket.has_ipv6 else 'false'}\n[protocols.opencloud]\nprotocol = 'quic'\ntarget = ['127.0.0.1:{core.H3_PORT}']\nserver_names = ['{domain}']\n"
            return text + (f"[protocols.fallback]\nprotocol = 'quic'\ntarget = ['127.0.0.1:{target}']\n" if occupied else '')
        (state / 'stage.toml').write_text(router_config(core.STAGE_PORT, 443))
        (state / 'router.toml').write_text(router_config(443, core.OLD_PORT))
        print('Preparing pinned images and testing both routes before switching...', flush=True)
        for image in ((core.RPXY_IMAGE, core.ROUTER_IMAGE, core.CURL_IMAGE) if shared_router else (core.RPXY_IMAGE, core.CURL_IMAGE)):
            core.command('docker', 'pull', image, timeout=600)
        if shared_router:
            core.command('docker', 'run', '-d', '--name', core.RPXY, '--label', f'{core.LABEL}=true', '--network', 'host',
                     '--restart', 'unless-stopped', '--add-host', f'{domain}:127.0.0.1', '-e', 'LOG_LEVEL=warn',
                     '-v', f'{state}/rpxy.toml:/etc/rpxy.toml:ro', '-v', f'{state}/rpxy-bin:/rpxy/bin/rpxy:ro',
                     '-v', f'{state}/certs:/certs:ro', core.RPXY_IMAGE)
            core.wait_h3(domain, core.H3_PORT, True)
            core.command('docker', 'run', '-d', '--name', core.STAGE, '--label', f'{core.LABEL}=true', '--network', 'host',
                     '-v', f'{state}/stage.toml:/etc/rpxy-l4.toml:ro', core.ROUTER_IMAGE)
            core.wait_h3(domain, core.STAGE_PORT, True)
            if occupied: core.wait_h3(fallback, 443 if fallback == domain else core.STAGE_PORT, False)
            core.docker_remove(core.STAGE)
        for item in manifest['files']:
            p = Path(item['path'])
            if core.digest(p) != item['original_sha256']: raise RuntimeError(f'{p} changed during setup.')
            core.replace_file(p, changes[p])
        manifest['compose_started'] = True; save()
        for project in selected: project.recreate()
        if not core.wait_for_udp443(): raise RuntimeError('UDP 443 was not released. Rolling back without sharing the port.')
        if not shared_router:
            def start_direct(name, config):
                core.command('docker', 'run', '-d', '--name', name, '--label', f'{core.LABEL}=true', '--network', 'host',
                             '--restart', 'unless-stopped', '--user', '0:0', '--cap-drop', 'ALL', '--cap-add', 'NET_BIND_SERVICE',
                             '--security-opt', 'no-new-privileges', '--read-only', '--entrypoint', '/rpxy/bin/rpxy',
                             '--add-host', f'{domain}:127.0.0.1', '-e', 'RUST_LOG=warn',
                             '-v', f'{state}/{config}:/etc/rpxy.toml:ro', '-v', f'{state}/rpxy-bin:/rpxy/bin/rpxy:ro',
                             '-v', f'{state}/certs:/certs:ro', core.RPXY_IMAGE, '--config', '/etc/rpxy.toml')
            start_direct(core.STAGE, 'direct-stage.toml')
            core.wait_h3(domain, core.H3_PORT, True)
            core.docker_remove(core.STAGE)
            start_direct(core.RPXY, 'rpxy.toml')
        else:
            core.command('docker', 'run', '-d', '--name', core.ROUTER, '--label', f'{core.LABEL}=true', '--network', 'host',
                     '--restart', 'unless-stopped', '--user', '0:0', '--cap-drop', 'ALL', '--cap-add', 'NET_BIND_SERVICE',
                     '--security-opt', 'no-new-privileges', '--read-only', '--entrypoint', '/rpxy-l4/bin/rpxy-l4',
                     '-e', 'RUST_LOG=warn', '-v', f'{state}/router.toml:/etc/rpxy-l4.toml:ro', core.ROUTER_IMAGE,
                     '--config', '/etc/rpxy-l4.toml')
        core.wait_h3(domain, 443, True)
        if occupied: core.wait_h3(fallback, core.OLD_PORT if fallback == domain else 443, False)
        for path in ('/', '/config.json'):
            headers = core.command('curl', '--noproxy', '*', '--max-time', '15', '-fsSI', '--resolve',
                                   f'{domain}:443:127.0.0.1', f'https://{domain}{path}').stdout
            if not re.search(r'(?im)^alt-svc:\s*.*h3=', headers): raise RuntimeError('OpenCloud Alt-Svc middleware did not become active.')
        # Copy only the self-contained runtime files; later sync does not depend on the download directory.
        for name in ('setup.py', 'traefik_setup.py'): shutil.copyfile(Path(__file__).parent / name, state / name)
        shutil.copytree(Path(__file__).parent / 'vendor', state / 'vendor', ignore=shutil.ignore_patterns('__pycache__'))
        units = {
            SERVICE: f'[Unit]\nDescription=Sync OpenCloud Quinn certificate from existing Traefik/Certbot certificate\n[Service]\nType=oneshot\nUMask=0077\nExecStart={sys.executable} {state}/setup.py --sync-cert\n',
            TIMER: '[Unit]\nDescription=Check OpenCloud HTTP/3 certificate renewal\n[Timer]\nOnBootSec=2min\nOnUnitActiveSec=5min\n[Install]\nWantedBy=timers.target\n',
        }
        manifest['unit_hashes'] = {p.name: hashlib.sha256(text.encode()).hexdigest() for p, text in units.items()}
        manifest['sync_created'] = True; save()
        for p, text in units.items(): p.write_text(text)
        core.command('systemctl', 'daemon-reload'); core.command('systemctl', 'enable', '--now', TIMER.name)
        print('\nLocal OpenCloud HTTP/3 and Traefik fallback tests passed. Verify external UDP and a real authenticated upload.')
        print(f'Rollback: sudo python3 {Path(__file__).parent / "setup.py"} --rollback')
    except BaseException:
        try: core.save_diagnostics(state)
        except Exception: pass
        print('Setup failed. Restoring the selected Compose services...', file=sys.stderr)
        try: rollback(state)
        except Exception as exc: print(f'ROLLBACK FAILED: {exc}. Backups: {backup}', file=sys.stderr)
        raise
