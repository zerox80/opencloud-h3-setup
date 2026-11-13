#!/usr/bin/env python3
"""Experimental interactive OpenCloud HTTP/3 setup for host Nginx and Docker.

No certificates or credentials are embedded. Requires the companion rpxy binary,
existing Let's Encrypt certificate files, host Nginx, Docker and Python 3.9+.
Only a host Nginx listener on UDP 443 is migrated; other owners are rejected.
The real router has been tested in an isolated lab; production cutover has not.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

STATE = Path('/var/lib/opencloud-h3-experimental')
HOOK = Path('/etc/letsencrypt/renewal-hooks/deploy/opencloud-h3-experimental')
RPXY_IMAGE = 'jqtype/rpxy@sha256:d6777c0800dbe036e9efcc6480ae7eada5ae215ed85ea0d19443ddee0b08ce81'
ROUTER_IMAGE = 'jqtype/rpxy-l4@sha256:2fd0cb93958174095bb43c8cf0d0e2eb5a71ccd91e0b3cb026eab8b126adafb0'
CURL_IMAGE = 'ymuski/curl-http3@sha256:9eadfcaa6e541ef61880795c3044f7a603dd62fe5be5a3b45b5e154025c783de'
RPXY = 'opencloud-h3-experimental-rpxy'
ROUTER = 'opencloud-h3-experimental-router'
STAGE = 'opencloud-h3-experimental-stage'
LABEL = 'org.opencloud.h3.experimental'
H3_PORT, OLD_PORT, STAGE_PORT = 19443, 18443, 24443
PORT_RELEASE_TIMEOUT = 15
NAME = re.compile(r'(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$')
QUIC_LISTEN = re.compile(r'^(\s*listen\s+)(443|0\.0\.0\.0:443|\[::\]:443)(\s+[^;]*\bquic\b[^;]*;[^\n]*)$', re.M)
ANY_QUIC_443 = re.compile(r'^\s*listen\s+[^;]*\b443\b[^;]*\bquic\b[^;]*;', re.M)


def command(*args: str, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f'Command failed: {args[0]}: {exc}') from exc
    if check and result.returncode:
        raise RuntimeError(f'{args[0]} failed: {result.stderr.strip() or result.stdout.strip()}')
    return result


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace_file(path: Path, content: str) -> None:
    stat = path.stat()
    fd, name = tempfile.mkstemp(prefix='.opencloud-h3-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as tmp:
            os.fchmod(tmp.fileno(), stat.st_mode & 0o7777)
            os.fchown(tmp.fileno(), stat.st_uid, stat.st_gid)
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def check_domain(value: str) -> str:
    if not NAME.fullmatch(value) or '..' in value:
        raise ValueError('Enter a valid fully qualified domain name.')
    return value.lower()


def prompt_domain(label: str, default: str = '') -> str:
    while True:
        value = input(f'{label}{" [" + default + "]" if default else ""}: ').strip() or default
        try:
            return check_domain(value)
        except ValueError as exc:
            print(exc)


def server_blocks(files: dict[Path, str]):
    for path, text in files.items():
        masked = mask_comments(text)
        for match in re.finditer(r'(?m)^\s*server\s*\{', masked):
            opening = masked.find('{', match.start(), match.end())
            yield path, masked[opening + 1:braces(text, opening)]


def server_names(body: str) -> list[str]:
    return [name.lower() for group in re.findall(r'(?m)^\s*server_name\s+([^;]+);', body)
            for name in group.split() if NAME.fullmatch(name)]


def suggested_domain(files: dict[Path, str]) -> str:
    for _, body in server_blocks(files):
        if re.search(r'proxy_pass\s+http://(?:127\.0\.0\.1:9200|opencloud[^;\s]*)', body):
            names = server_names(body)
            if names: return names[0]
    return ''


def suggested_upstream(files: dict[Path, str], domain: str) -> str:
    combined = '\n'.join(mask_comments(text) for text in files.values())
    for _, body in server_blocks(files):
        if domain not in server_names(body): continue
        match = re.search(r'proxy_pass\s+http://([^/;\s]+)', body)
        if not match: continue
        value = match.group(1)
        if re.fullmatch(r'127\.0\.0\.1:[0-9]{1,5}', value): return value
        if not re.fullmatch(r'[A-Za-z0-9_-]+', value): continue
        upstream = re.search(r'\bupstream\s+' + re.escape(value) + r'\s*\{([^}]+)\}', combined)
        if upstream:
            addresses = re.findall(r'\bserver\s+(127\.0\.0\.1:[0-9]{1,5})\s*;', upstream.group(1))
            if len(addresses) == 1: return addresses[0]
    return '127.0.0.1:9200'


def find_binary() -> Path:
    directory = Path(__file__).resolve().parent
    candidates = [directory / 'rpxy-opencloud-linux-amd64', directory / 'rpxy-bin',
                  Path('/root/opencloud/rpxy-h3/rpxy-bin')]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            print('Found the required HTTP/3 support component automatically.')
            return candidate.resolve()
    raise RuntimeError('This setup download is incomplete. Extract the complete opencloud-h3-setup.tar.gz bundle and run its setup script. No component paths need to be entered.')


def find_certificates(files: dict[Path, str], domain: str) -> tuple[Path, Path]:
    candidates = []
    for _, body in server_blocks(files):
        if domain not in server_names(body): continue
        certs = re.findall(r'(?m)^\s*ssl_certificate\s+([^;]+);', body)
        keys = re.findall(r'(?m)^\s*ssl_certificate_key\s+([^;]+);', body)
        if len(certs) == len(keys) == 1:
            candidates.append((Path(certs[0].strip().strip('\"\'')), Path(keys[0].strip().strip('\"\''))))
    for cert in sorted(Path('/etc/letsencrypt/live').glob('*/fullchain.pem')):
        candidates.append((cert, cert.parent / 'privkey.pem'))
    seen = set()
    for cert, key in candidates:
        if (cert, key) in seen: continue
        seen.add((cert, key))
        if not cert.is_file() or not key.is_file() or cert.parent != key.parent: continue
        if not re.fullmatch(r'/etc/letsencrypt/live/[A-Za-z0-9._-]+/fullchain\.pem', str(cert)): continue
        if key.name != 'privkey.pem': continue
        if command('openssl', 'x509', '-in', str(cert), '-noout', '-checkhost', domain, check=False).returncode: continue
        if command('openssl', 'x509', '-in', str(cert), '-noout', '-checkend', '86400', check=False).returncode: continue
        print(f'Found and verified the existing HTTPS certificate for {domain}.')
        return cert, key
    print(f'No usable Let\'s Encrypt certificate was found for {domain}.')
    value = input('Let\'s Encrypt certificate folder, e.g. /etc/letsencrypt/live/example.com (or q to cancel): ').strip()
    if value.lower() in ('', 'q', 'quit'): raise RuntimeError('Setup cancelled; a valid HTTPS certificate is required.')
    directory = Path(value).expanduser()
    return directory / 'fullchain.pem', directory / 'privkey.pem'


def suggested_fallback(files: dict[Path, str], domain: str) -> str:
    for _, body in server_blocks(files):
        if not ANY_QUIC_443.search(body): continue
        for name in server_names(body):
            if name != domain: return name
    return ''


def loaded_nginx_files() -> dict[Path, str]:
    output = command('nginx', '-T', timeout=30).stdout
    matches = list(re.finditer(r'^# configuration file (.+):\s*$', output, re.M))
    files: dict[Path, str] = {}
    for match in matches:
        path = Path(match.group(1)).resolve()
        if not path.is_absolute() or not path.is_file():
            raise RuntimeError(f'Unsupported Nginx configuration path: {path}')
        if path in files:
            raise RuntimeError(f'Nginx includes {path} more than once; refusing automatic edits.')
        files[path] = path.read_text()
    if not files:
        raise RuntimeError('Could not enumerate active Nginx configuration files.')
    return files


def braces(text: str, start: int) -> int:
    # Find the matching closing brace while respecting Nginx quotes and comments.
    depth, quote, escape, comment = 0, '', False, False
    for i in range(start, len(text)):
        c = text[i]
        if comment:
            if c == '\n': comment = False
            continue
        if escape:
            escape = False
            continue
        if quote:
            if c == '\\': escape = True
            elif c == quote: quote = ''
            continue
        if c == '#': comment = True
        elif c in ('\"', "'"): quote = c
        elif c == '{': depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0: return i
    raise RuntimeError('Unbalanced braces in Nginx configuration.')


def mask_comments(text: str) -> str:
    # Preserve offsets and quoted characters; only comments are blanked.
    out, quote, escape, comment = [], '', False, False
    for c in text:
        if comment:
            out.append('\n' if c == '\n' else ' ')
            if c == '\n': comment = False
        elif escape:
            out.append(c); escape = False
        elif quote:
            out.append(c)
            if c == '\\': escape = True
            elif c == quote: quote = ''
        elif c == '#': out.append(' '); comment = True
        else:
            out.append(c)
            if c in ('\"', "'"): quote = c
    return ''.join(out)


def add_advertisement(files: dict[Path, str], domain: str) -> tuple[Path, str]:
    candidates = []
    for path, text in files.items():
        masked = mask_comments(text)
        for match in re.finditer(r'(?m)^\s*server\s*\{', masked):
            opening = masked.find('{', match.start(), match.end())
            closing = braces(text, opening)
            body = masked[opening + 1:closing]
            names = re.findall(r'(?m)^\s*server_name\s+([^;]+);', body)
            if not any(domain in namestr.split() for namestr in names): continue
            if not re.search(r'(?m)^\s*listen\s+(?:443|0\.0\.0\.0:443|\[::\]:443)\s+[^;]*\bssl\b', body): continue
            if any(namestr.split() != [domain] for namestr in names):
                raise RuntimeError('The target TLS server block contains additional server names; configure it manually.')
            if re.search(r'(?im)^\s*add_header\s+alt-svc\b', body):
                raise RuntimeError('The target server block already sets Alt-Svc; refusing to duplicate it.')
            candidates.append((path, opening))
    if len(candidates) != 1:
        raise RuntimeError('Expected exactly one explicit TLS server block for the OpenCloud domain.')
    path, opening = candidates[0]
    text = files[path]
    header = '\n    add_header Alt-Svc \'h3=":443"; ma=60\' always; # opencloud-h3-experimental\n'
    return path, text[:opening + 1] + header + text[opening + 1:]


def migrate_nginx_quic(files: dict[Path, str]) -> tuple[dict[Path, str], bool]:
    modified, ipv4 = {}, False
    for path, text in files.items():
        masked = mask_comments(text)
        matches = list(QUIC_LISTEN.finditer(masked))
        unsupported = list(ANY_QUIC_443.finditer(masked))
        if len(matches) != len(unsupported):
            raise RuntimeError(f'Unsupported or multiline QUIC listen directive in {path}.')
        for match in reversed(matches):
            old = match.group(2)
            replacement = '[::1]:18443' if old.startswith('[') else '127.0.0.1:18443'
            if not old.startswith('['): ipv4 = True
            start, end = match.span(2)
            text = text[:start] + replacement + text[end:]
        if matches: modified[path] = text
    return modified, ipv4


def ports_free() -> None:
    for kind, port in ((socket.SOCK_DGRAM, OLD_PORT), (socket.SOCK_DGRAM, STAGE_PORT),
                       (socket.SOCK_DGRAM, H3_PORT), (socket.SOCK_STREAM, H3_PORT)):
        sock = socket.socket(socket.AF_INET, kind)
        try: sock.bind(('127.0.0.1', port))
        except OSError as exc: raise RuntimeError(f'Internal port {port} is occupied: {exc}') from exc
        finally: sock.close()


def curl_h3(domain: str, port: int, require_200: bool) -> None:
    args = ['docker', 'run', '--rm', '--network', 'host', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--entrypoint', 'curl', CURL_IMAGE,
            '--noproxy', '*', '--http3-only', '--max-time', '5', '-sS', '-o', '/dev/null',
            '-w', '%{http_code}', '--resolve', f'{domain}:{port}:127.0.0.1',
            f'https://{domain}:{port}/config.json' if require_200 else f'https://{domain}:{port}/']
    result = command(*args, timeout=60)
    code = result.stdout.strip()
    if not re.fullmatch(r'[1-5][0-9][0-9]', code) or (require_200 and code != '200'):
        raise RuntimeError(f'HTTP/3 check failed for {domain}:{port}: {code}')


def wait_h3(domain: str, port: int, require_200: bool) -> None:
    last = None
    for _ in range(3):
        try:
            curl_h3(domain, port, require_200)
            return
        except RuntimeError as exc:
            last = exc
            time.sleep(2)
    raise RuntimeError(f'HTTP/3 did not become ready: {last}')


def docker_remove(name: str) -> None:
    result = command('docker', 'inspect', '--format', '{{index .Config.Labels "org.opencloud.h3.experimental"}}',
                     name, check=False)
    if result.returncode: return
    if result.stdout.strip() != 'true':
        raise RuntimeError(f'Refusing to remove container {name}: it is not owned by this installer.')
    command('docker', 'rm', '-f', name, timeout=30)


def udp443_listeners() -> str:
    return command('ss', '-Hlunp', 'sport = :443').stdout.strip()


def wait_for_udp443() -> bool:
    deadline = time.monotonic() + PORT_RELEASE_TIMEOUT
    while True:
        if not udp443_listeners(): return True
        if time.monotonic() >= deadline: return False
        time.sleep(1)


def release_public_port() -> None:
    print('Waiting for the previous UDP 443 listeners to close...', flush=True)
    if wait_for_udp443(): return
    listeners = udp443_listeners()
    if any('nginx' not in line.lower() for line in listeners.splitlines()):
        raise RuntimeError('Another process still owns UDP 443; refusing to share its packets.')
    print('Old Nginx workers still hold UDP 443 after reload.')
    print('A brief Nginx restart can release them, but it interrupts current connections on ALL host Nginx sites.')
    answer = input('Type RESTART to restart Nginx, or press Enter to restore the original setup: ').strip()
    if answer != 'RESTART':
        raise RuntimeError('UDP 443 is still occupied. Nginx restart was not approved; rolling back.')
    if not shutil.which('systemctl') or command('systemctl', 'is-active', 'nginx', check=False).returncode:
        raise RuntimeError('Nginx is not managed by an active systemd nginx service; restart it manually.')
    command('nginx', '-t')
    command('systemctl', 'restart', 'nginx', timeout=60)
    if not wait_for_udp443(): raise RuntimeError('UDP 443 remains occupied after restarting Nginx.')


def save_diagnostics(state: Path) -> None:
    data = {'udp443': udp443_listeners(), 'containers': {}}
    for name in (RPXY, STAGE, ROUTER):
        status = command('docker', 'inspect', '--format', '{{json .State}}', name, check=False)
        logs = command('docker', 'logs', '--tail', '100', name, check=False)
        data['containers'][name] = {'state': status.stdout.strip(), 'logs': logs.stdout + logs.stderr}
    path = state / 'diagnostics.json'
    path.write_text(json.dumps(data, indent=2))
    path.chmod(0o600)
    print(f'Diagnostic logs saved to {path}', file=sys.stderr)


def check_previous_attempt() -> bool:
    if not STATE.exists(): return False
    if not (STATE / 'ROLLED_BACK').is_file():
        raise RuntimeError(f'{STATE} contains an existing setup. Run this script with --rollback first.')
    manifest = json.loads((STATE / 'manifest.json').read_text())
    for item in manifest['files']:
        path = Path(item['path'])
        if not path.is_file() or digest(path) != item['original_sha256']:
            raise RuntimeError(f'The previous rollback could not be verified for {path}. Check the saved backups first.')
    return True


def rollback(state: Path = STATE) -> None:
    manifest = json.loads((state / 'manifest.json').read_text())
    changed = []
    for item in manifest['files']:
        path = Path(item['path'])
        allowed = {item['installed_sha256'], item['original_sha256']}
        if not path.is_file() or digest(path) not in allowed:
            raise RuntimeError(f'{path} changed since setup. Restore it manually from {state / "backup"}.')
        if digest(path) != item['original_sha256']:
            changed.append(item)
    docker_remove(STAGE)
    docker_remove(ROUTER)
    docker_remove(RPXY)
    for item in changed:
        shutil.copy2(state / 'backup' / item['backup'], Path(item['path']))
    if changed:
        command('nginx', '-t')
        command('nginx', '-s', 'reload')
    if manifest.get('hook_created') and HOOK.exists(): HOOK.unlink()
    copied_key = state / 'certs' / 'privkey.pem'
    if copied_key.exists(): copied_key.unlink()
    (state / 'ROLLED_BACK').write_text('The original Nginx files were restored.\n')
    print(f'Rollback complete. Backups are retained in {state / "backup"}.')


def install() -> None:
    if os.geteuid() != 0: raise RuntimeError('Run this script as root with sudo.')
    if platform.machine().lower() not in ('x86_64', 'amd64'):
        raise RuntimeError('The tested companion binary and container images require Linux x86-64.')
    for name in ('nginx', 'docker', 'openssl', 'ss', 'curl'):
        if not shutil.which(name): raise RuntimeError(f'Missing required program: {name}')
    if shutil.which('getenforce') and command('getenforce').stdout.strip() == 'Enforcing':
        raise RuntimeError('SELinux enforcing mode is not supported by this experimental script.')
    previous_attempt = check_previous_attempt()
    if HOOK.exists(): raise RuntimeError(f'{HOOK} already exists; refusing to overwrite it.')
    for name in (RPXY, ROUTER, STAGE):
        if command('docker', 'inspect', name, check=False).returncode == 0:
            raise RuntimeError(f'Container {name} already exists.')
    print('EXPERIMENTAL: host Nginx + Docker only; HTTP/3 via rpxy and a QUIC SNI router.')
    print('The router maintainer does not recommend it for production; this script has only been tested in a lab.')
    print('UDP 443 stays public. Existing Nginx QUIC listeners move to localhost:18443.')
    print('The UDP router does not preserve the original client IP at backend services.')
    print('Encrypted ClientHello over QUIC and arbitrary non-Nginx UDP services are not supported.')
    print('\nLooking for your existing OpenCloud, Nginx and HTTPS settings...', flush=True)
    command('nginx', '-t')
    command('docker', 'info', timeout=30)
    files = loaded_nginx_files()
    domain = prompt_domain('OpenCloud domain', suggested_domain(files))
    binary = find_binary()
    upstream = suggested_upstream(files, domain)
    body_limit = '64m'
    if input('Customize advanced connection settings? [y/N]: ').strip().lower() in ('y', 'yes'):
        upstream = input(f'OpenCloud internal address [{upstream}]: ').strip() or upstream
        body_limit = input('Maximum request body size [64m; 0 means unlimited]: ').strip() or body_limit
    if not re.fullmatch(r'127\.0\.0\.1:[0-9]{1,5}', upstream) or not 1 <= int(upstream.rsplit(':', 1)[1]) <= 65535:
        raise RuntimeError('The OpenCloud upstream must be 127.0.0.1:<port>.')
    if not re.fullmatch(r'(?:0|[1-9][0-9]*[kKmMgG]?)', body_limit):
        raise RuntimeError('Enter a non-negative integer with an optional k, m or g suffix.')
    cert, key = find_certificates(files, domain)
    if not binary.is_file() or not os.access(binary, os.X_OK): raise RuntimeError('The bundled HTTP/3 support component cannot be used. Extract the complete setup bundle again.')
    if not cert.is_absolute() or not key.is_absolute() or cert.parent != key.parent:
        raise RuntimeError('Certificate and key must be absolute paths in the same Let\'s Encrypt lineage.')
    if not str(cert).startswith('/etc/letsencrypt/live/') or cert.name != 'fullchain.pem' or key.name != 'privkey.pem':
        raise RuntimeError('Only /etc/letsencrypt/live/<lineage>/{fullchain,privkey}.pem is supported.')
    if not HOOK.parent.is_dir(): raise RuntimeError('The Certbot deploy-hook directory is missing.')
    command('openssl', 'x509', '-in', str(cert), '-noout', '-checkhost', domain)
    command('openssl', 'x509', '-in', str(cert), '-noout', '-checkend', '86400')
    certpub = subprocess.run(['openssl', 'x509', '-in', str(cert), '-pubkey', '-noout'], capture_output=True, check=True).stdout
    certder = subprocess.run(['openssl', 'pkey', '-pubin', '-outform', 'DER'], input=certpub, capture_output=True, check=True).stdout
    keyder = subprocess.run(['openssl', 'pkey', '-in', str(key), '-pubout', '-outform', 'DER'], capture_output=True, check=True).stdout
    if certder != keyder: raise RuntimeError('Certificate and private key do not match.')
    command('nginx', '-t')
    command('docker', 'info', timeout=30)
    ports_free()
    listeners = command('ss', '-Hlunp', 'sport = :443').stdout.strip()
    occupied = bool(listeners)
    if occupied and any('nginx' not in line.lower() for line in listeners.splitlines()):
        raise RuntimeError('UDP 443 is owned by something other than host Nginx. Automatic migration is not supported.')
    existing_domain = prompt_domain('Another existing HTTP/3 domain to test', suggested_fallback(files, domain)) if occupied else ''
    if occupied and existing_domain == domain:
        raise RuntimeError('Use a different existing HTTP/3 hostname to validate the fallback.')
    moved, has_ipv4 = migrate_nginx_quic(files)
    if occupied and not has_ipv4:
        raise RuntimeError('Could not find a supported IPv4 Nginx QUIC listener on 443.')
    if not occupied and moved:
        raise RuntimeError('Nginx config contains QUIC listeners on 443 but ss shows none; resolve this first.')
    base = dict(files); base.update(moved)
    site_path, site_content = add_advertisement(base, domain)
    changes = dict(moved); changes[site_path] = site_content
    for path in changes:
        if not path.is_relative_to(Path('/etc/nginx')):
            raise RuntimeError(f'A required Nginx edit is outside /etc/nginx: {path}. Configure this layout manually.')
    print('\nFiles to back up and update:')
    for path in changes: print(f'  {path}')
    print(f'The public HTTPS TCP listener stays with Nginx. rpxy uses localhost:{H3_PORT}.')
    if input('Type APPLY to start the experimental setup: ').strip() != 'APPLY':
        print('No changes made.'); return
    if previous_attempt:
        archived = STATE.with_name(STATE.name + '.rolled-back-' + str(time.time_ns()))
        STATE.rename(archived)
        print(f'The previous attempt and backups were retained in {archived}.')
    STATE.mkdir(mode=0o700)
    backup = STATE / 'backup'; backup.mkdir(mode=0o700)
    manifest = {'files': [], 'hook_created': False}
    for i, (path, content) in enumerate(changes.items()):
        name = f'{i}.conf'; shutil.copy2(path, backup / name)
        manifest['files'].append({'path': str(path), 'backup': name,
                                  'original_sha256': digest(path),
                                  'installed_sha256': hashlib.sha256(content.encode()).hexdigest()})
    (STATE / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    os.chmod(STATE / 'manifest.json', 0o600)
    try:
        certdir = STATE / 'certs'; certdir.mkdir(mode=0o750); os.chown(certdir, 0, 900)
        shutil.copyfile(cert, certdir / 'fullchain.pem'); os.chmod(certdir / 'fullchain.pem', 0o644)
        shutil.copyfile(key, certdir / 'privkey.pem'); os.chown(certdir / 'privkey.pem', 900, 900); os.chmod(certdir / 'privkey.pem', 0o600)
        shutil.copyfile(binary, STATE / 'rpxy-bin'); os.chmod(STATE / 'rpxy-bin', 0o755)
        (STATE / 'rpxy.toml').write_text(f'''listen_port_tls = {H3_PORT}\nlisten_address_v4 = '127.0.0.1'\nlisten_ipv6 = false\nrequest_max_body_size = '{body_limit}'\nredact_query_in_access_log = true\n[apps.opencloud]\nserver_name = '{domain}'\ntls = {{ tls_cert_path = '/certs/fullchain.pem', tls_cert_key_path = '/certs/privkey.pem' }}\nreverse_proxy = [{{ upstream = [{{ location = '{upstream}' }}] }}]\n[experimental.h3]\nalt_svc_max_age = 60\nmax_idle_timeout = 60\n''')
        def router_config(port: int, target_port: int, address: str) -> str:
            fallback = f"\n[protocols.fallback]\nprotocol = 'quic'\ntarget = ['127.0.0.1:{target_port}']\nidle_lifetime = 60\n" if occupied else ''
            return f"listen_port = {port}\nlisten_address_v4 = '{address}'\nlisten_ipv6 = {'true' if address == '0.0.0.0' and socket.has_ipv6 else 'false'}\nudp_idle_lifetime = 60\n[protocols.opencloud]\nprotocol = 'quic'\ntarget = ['127.0.0.1:{H3_PORT}']\nserver_names = ['{domain}']\nidle_lifetime = 60\n" + fallback
        (STATE / 'stage.toml').write_text(router_config(STAGE_PORT, 443, '127.0.0.1'))
        (STATE / 'router.toml').write_text(router_config(443, OLD_PORT, '0.0.0.0'))
        print('Preparing the pinned container images; this may take several minutes...', flush=True)
        for image in (RPXY_IMAGE, ROUTER_IMAGE, CURL_IMAGE): command('docker', 'pull', image, timeout=600)
        command('docker', 'run', '-d', '--name', RPXY, '--label', f'{LABEL}=true', '--network', 'host', '--restart', 'unless-stopped',
                '-e', 'LOG_LEVEL=warn', '-v', f'{STATE}/rpxy.toml:/etc/rpxy.toml:ro',
                '-v', f'{STATE}/rpxy-bin:/rpxy/bin/rpxy:ro', '-v', f'{certdir}:/certs:ro', RPXY_IMAGE)
        print('Testing rpxy on a local port...', flush=True)
        wait_h3(domain, H3_PORT, True)
        command('docker', 'run', '-d', '--name', STAGE, '--label', f'{LABEL}=true', '--network', 'host',
                '-v', f'{STATE}/stage.toml:/etc/rpxy-l4.toml:ro', ROUTER_IMAGE)
        print('Testing the router and existing-service fallback before switching...', flush=True)
        wait_h3(domain, STAGE_PORT, True)
        if occupied: wait_h3(existing_domain, STAGE_PORT, False)
        docker_remove(STAGE)
        for item in manifest['files']:
            path = Path(item['path'])
            if digest(path) != item['original_sha256']:
                raise RuntimeError(f'{path} changed while setup was running.')
            replace_file(path, changes[path])
        command('nginx', '-t'); command('nginx', '-s', 'reload')
        release_public_port()
        command('docker', 'run', '-d', '--name', ROUTER, '--label', f'{LABEL}=true', '--network', 'host', '--restart', 'unless-stopped',
                '--user', '0:0', '--cap-drop', 'ALL', '--cap-add', 'NET_BIND_SERVICE',
                '--security-opt', 'no-new-privileges', '--read-only',
                '--entrypoint', '/rpxy-l4/bin/rpxy-l4', '-e', 'RUST_LOG=warn',
                '-v', f'{STATE}/router.toml:/etc/rpxy-l4.toml:ro', ROUTER_IMAGE, '--config', '/etc/rpxy-l4.toml')
        print('Testing both routes on UDP 443...', flush=True)
        wait_h3(domain, 443, True)
        if occupied: wait_h3(existing_domain, 443, False)
        for path in ('/', '/config.json'):
            headers = command('curl', '--noproxy', '*', '--max-time', '12', '-sSI', '--resolve',
                              f'{domain}:443:127.0.0.1', f'https://{domain}{path}').stdout.lower()
            if not re.search(r'(?m)^alt-svc:\s*.*h3=', headers):
                raise RuntimeError(f'Alt-Svc is missing from the OpenCloud response for {path}.')
        lineage = str(cert.parent)
        # Domain/lineage paths are restricted to a conservative character set before embedding.
        if not re.fullmatch(r'/etc/letsencrypt/live/[A-Za-z0-9._-]+', lineage): raise RuntimeError('Unsupported certificate lineage path.')
        hook = f'''#!/bin/sh\nset -eu\n[ "${{RENEWED_LINEAGE:-}}" = "{lineage}" ] || exit 0\ninstall -m 644 "$RENEWED_LINEAGE/fullchain.pem" "{certdir}/fullchain.pem.new"\ninstall -m 600 -o 900 -g 900 "$RENEWED_LINEAGE/privkey.pem" "{certdir}/privkey.pem.new"\nmv -f "{certdir}/fullchain.pem.new" "{certdir}/fullchain.pem"\nmv -f "{certdir}/privkey.pem.new" "{certdir}/privkey.pem"\ndocker restart {RPXY} >/dev/null\n'''
        manifest['hook_created'] = True
        (STATE / 'manifest.json').write_text(json.dumps(manifest, indent=2))
        HOOK.write_text(hook); os.chmod(HOOK, 0o755)
        print('\nLocal HTTP/3 tests passed for the OpenCloud route and, when applicable, the fallback route.')
        print('Verify UDP 443 from an external network and test a real authenticated upload.')
        print(f'To roll back: sudo python3 {Path(__file__).resolve()} --rollback')
    except BaseException:
        try: save_diagnostics(STATE)
        except Exception as exc: print(f'Could not save diagnostic logs: {exc}', file=sys.stderr)
        print('\nSetup failed. Restoring backed-up Nginx files and stopping test containers...', file=sys.stderr)
        try: rollback(STATE)
        except Exception as exc: print(f'ROLLBACK FAILED: {exc}. Backups: {backup}', file=sys.stderr)
        raise


def main() -> int:
    def interrupted(signum: int, _frame: object) -> None:
        raise RuntimeError(f'Interrupted by signal {signum}')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    try:
        if sys.argv[1:] == ['--rollback']:
            if os.geteuid() != 0: raise RuntimeError('Run rollback as root with sudo.')
            rollback()
        elif not sys.argv[1:]: install()
        else: raise RuntimeError(f'Usage: {sys.argv[0]} [--rollback]')
        return 0
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError, KeyboardInterrupt, EOFError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
