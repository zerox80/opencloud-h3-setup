"""Real Compose rendering and X.509 checks; lifecycle commands are mocked."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import setup as core
import traefik_setup as tf


class TraefikTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.real_command = core.command
        self.commands = []
        def command(*args, **kwargs):
            self.commands.append(args)
            if args[:2] == ('docker', 'compose') and os.environ.get('COMPOSE_TEST_BINARY'):
                return self.real_command(os.environ['COMPOSE_TEST_BINARY'], *args[2:], **kwargs)
            return self.real_command(*args, **kwargs)
        self.command_patch = patch.object(core, 'command', command)
        self.command_patch.start(); self.addCleanup(self.command_patch.stop)

    def projects(self, ports='["443:443/tcp", "443:443/udp"]', extra=''):
        text = '''# Preserve operator comments
services:
  traefik:
    image: traefik:v3.7.19
    command: ["--entrypoints.websecure.address=:443", "--providers.docker=true"]
    ports: PORTS
  opencloud:
    image: opencloudeu/opencloud:latest
    labels:
      traefik.http.routers.cloud.rule: Host(`cloud.example.com`)
      traefik.http.routers.cloud.tls: "true"
      traefik.http.routers.cloud.entrypoints: websecure
      traefik.http.routers.cloud.middlewares: existing@docker
  office:
    image: nginx:alpine
    labels:
      traefik.http.routers.office.rule: Host(`office.example.com`)
      traefik.http.routers.office.tls: "true"
''' .replace('PORTS', ports) + extra
        file = self.path / 'compose.yaml'; file.write_text(text)
        edge = object.__new__(tf.Compose)
        edge.project = 'test'; edge.service = 'traefik'; edge.directory = str(self.path)
        edge.files = [file]; edge.envfiles = []; edge.original = {file: text}; edge.config = edge.render()
        app = copy.copy(edge); app.service = 'opencloud'
        return edge, app

    def changes(self, edge, app, occupied=True):
        old = app.config['services'][app.service]['labels']
        labels = tf.advertised_labels(old, ['cloud'])
        return tf.prepare_changes([edge], app, edge, labels, occupied)

    def test_short_port_preserves_office_and_middleware(self):
        edge, app = self.projects()
        changes = self.changes(edge, app)
        text = changes[edge.files[0]]
        self.assertIn('# Preserve operator comments', text)
        self.assertIn('127.0.0.1:18443:443/udp', text)
        self.assertIn('opencloud-h3-experimental@docker,existing@docker', text)
        self.assertNotIn('middlewares', text.split('  office:')[1])
        self.assertEqual(edge.files[0].read_text(), edge.original[edge.files[0]])
        self.assertFalse(list(self.path.glob('.opencloud-h3-check-*')))

    def test_long_port(self):
        edge, app = self.projects('[{target: 443, published: "443", protocol: tcp}, {target: 443, published: "443", protocol: udp}]')
        text = next(iter(self.changes(edge, app).values()))
        self.assertIn('127.0.0.1', text); self.assertIn('18443', text)

    def test_free_udp(self):
        edge, app = self.projects('["443:443/tcp"]')
        self.assertNotIn('18443', next(iter(self.changes(edge, app, False).values())))

    def test_split_compose_files(self):
        edge, app = self.projects('["443:443/tcp"]')
        override = self.path / 'compose.override.yaml'
        override.write_text('services:\n  traefik:\n    ports: ["443:443/udp"]\n  opencloud:\n    environment:\n      EXAMPLE: "keep me"\n')
        edge.files.append(override); edge.original[override] = override.read_text(); edge.config = edge.render()
        app.config = edge.config
        changes = self.changes(edge, app)
        self.assertIn(override, changes)
        self.assertNotIn(edge.files[0], changes)
        self.assertIn('EXAMPLE: "keep me"', changes[override])

    def test_variable_port_rejected(self):
        (self.path / '.env').write_text('UDP_PORT=443\n')
        edge, app = self.projects('["443:443/tcp", "${UDP_PORT}:443/udp"]')
        with self.assertRaisesRegex(RuntimeError, 'literal UDP'): self.changes(edge, app)

    def test_shared_label_alias_rejected(self):
        edge, app = self.projects()
        file = edge.files[0]
        original = file.read_text().replace('    labels:\n      traefik.http.routers.cloud', '    labels: &shared\n      traefik.http.routers.cloud', 1)
        original += '  alias:\n    image: nginx:alpine\n    labels: *shared\n'
        file.write_text(original); edge.original[file] = original; edge.config = edge.render(); app.config = edge.config
        # Deep-copying labels isolates the service and leaves the alias untouched.
        self.changes(edge, app)

    def test_compound_host_is_not_guessed(self):
        self.assertEqual(tf.exact_routers({'traefik.http.routers.c.rule': 'Host(`cloud.example.com`) && PathPrefix(`/`)', 'traefik.http.routers.c.tls': 'true'}, 'cloud.example.com'), [])

    def test_middleware_name_collision(self):
        with self.assertRaisesRegex(RuntimeError, 'already in use'):
            tf.advertised_labels({f'traefik.http.middlewares.{tf.MIDDLEWARE}.headers.foo': 'x'}, ['cloud'])

    def make_cert(self, domain='cloud.example.com', suffix=''):
        cert, key = self.path / ('cert' + suffix), self.path / ('key' + suffix)
        self.real_command('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key),
                          '-out', str(cert), '-days', '2', '-subj', '/CN=' + domain, '-addext', 'subjectAltName=DNS:' + domain)
        return cert, key

    def store(self, cert, key):
        path = self.path / 'acme.json'
        path.write_text(json.dumps({'resolver': {'Certificates': [{'domain': {'main': 'cloud.example.com'},
                         'certificate': base64.b64encode(cert.read_bytes()).decode(), 'key': base64.b64encode(key.read_bytes()).decode()}]}}))
        return {'kind': 'acme', 'path': str(path)}

    def test_acme_certificate_verified(self):
        cert, key = self.make_cert(); source = self.store(cert, key)
        self.assertEqual(tf.certificate(source, 'cloud.example.com'), (cert.read_bytes(), key.read_bytes()))

    def test_acme_metadata_cannot_hide_wrong_san(self):
        cert, key = self.make_cert('other.example.com')
        with self.assertRaises(RuntimeError): tf.certificate(self.store(cert, key), 'cloud.example.com')

    def test_mismatched_key_rejected(self):
        cert, key = self.make_cert(); _, otherkey = self.make_cert(suffix='2')
        with self.assertRaisesRegex(RuntimeError, 'do not match'): tf.certificate(self.store(cert, otherkey), 'cloud.example.com')

    def test_renewal_retries_failed_restart(self):
        cert, key = self.make_cert(); source = self.store(cert, key)
        state = self.path / 'state'; state.mkdir()
        (state / 'manifest.json').write_text(json.dumps({'backend': 'traefik', 'domain': 'cloud.example.com', 'certificate_source': source}))
        with patch.object(tf.os, 'chown'), patch.object(core, 'command', side_effect=lambda *a, **kw: self.real_command(*a, **kw)):
            tf.sync_certificate(state, restart=False)
        cert2, key2 = self.make_cert(suffix='2'); self.store(cert2, key2)
        restarts = []
        def command(*args, **kwargs):
            if args[:2] == ('docker', 'restart'):
                restarts.append(args)
                if len(restarts) == 1: raise RuntimeError('simulated failed restart')
                return subprocess.CompletedProcess(args, 0, '', '')
            return self.real_command(*args, **kwargs)
        with patch.object(tf.os, 'chown'), patch.object(core, 'command', command):
            with self.assertRaisesRegex(RuntimeError, 'failed restart'): tf.sync_certificate(state)
            tf.sync_certificate(state)
            tf.sync_certificate(state)
        self.assertEqual(len(restarts), 2)
        self.assertEqual((state / 'certs/privkey.pem').read_bytes(), key2.read_bytes())
        self.assertEqual((state / 'certs/privkey.pem').stat().st_mode & 0o777, 0o600)

    def test_rollback_refuses_changed_files(self):
        file = self.path / 'compose.yaml'; file.write_text('operator edit')
        manifest = {'files': [{'path': str(file), 'original_sha256': 'old', 'installed_sha256': 'installed'}]}
        (self.path / 'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(RuntimeError, 'changed since setup'): tf.rollback(self.path)
        self.assertEqual(file.read_text(), 'operator edit')

    def test_rollback_restores_both_services_after_partial_recreate(self):
        file = self.path / 'compose.yaml'; file.write_text('installed')
        (self.path / 'backup').mkdir(); (self.path / 'backup/0').write_text('original')
        manifest = {'files': [{'path': str(file), 'backup': '0', 'original_sha256': hashlib.sha256(b'original').hexdigest(),
                              'installed_sha256': hashlib.sha256(b'installed').hexdigest()}], 'compose_started': True,
                    'projects': [{'service': 'traefik', 'files': []}, {'service': 'opencloud', 'files': []}]}
        (self.path / 'manifest.json').write_text(json.dumps(manifest))
        restored = []
        with patch.object(core, 'docker_remove'), patch.object(tf.Compose, 'recreate', lambda c: restored.append(c.service)):
            tf.rollback(self.path)
        self.assertEqual(restored, ['traefik', 'opencloud']); self.assertEqual(file.read_text(), 'original')
        self.assertTrue((self.path / 'ROLLED_BACK').exists())

    def lifecycle(self, fail=False, free=False, direct=False):
        edge, app = self.projects('["443:443/tcp"]' if free else '["443:443/tcp", "443:443/udp"]')
        source = edge.files[0]; original = source.read_text()
        cert, key = self.make_cert(); self.store(cert, key)
        binary = self.path / 'rpxy'; binary.write_text('fixture'); binary.chmod(0o755)
        state = self.path / 'state'; units = self.path / 'units'; units.mkdir()
        selected = []
        for service, image in [('traefik', 'traefik:v3.7.19'), ('opencloud', 'opencloudeu/opencloud:latest')]:
            config = edge.config['services'][service]
            hash_lines = edge.run('config', '--hash', service).splitlines()
            hash_value = next(line.split()[-1] for line in hash_lines if line.split()[0] == service)
            labels = dict(config.get('labels', {}))
            labels.update({'com.docker.compose.project': 'test', 'com.docker.compose.service': service,
                           'com.docker.compose.project.working_dir': str(self.path),
                           'com.docker.compose.project.config_files': str(source), 'com.docker.compose.config-hash': hash_value})
            selected.append({'Id': service, 'Image': service + '-image', 'Config': {'Image': image, 'Labels': labels},
                             'HostConfig': {'NetworkMode': 'test_default'},
                             'NetworkSettings': {'Ports': {'443/tcp': [{'HostPort': '443', 'HostIp': '0.0.0.0'}],
                                                          **({} if free else {'443/udp': [{'HostPort': '443', 'HostIp': '0.0.0.0'}]})} if service == 'traefik' else {}},
                             'Mounts': [{'Source': str(self.path / 'acme.json')}] if service == 'traefik' else []})
        owned = set(); recreated = []; restarted = []; h3 = []
        def command(*args, **kwargs):
            if args[:2] == ('docker', 'compose'):
                if 'up' in args:
                    recreated.append(args[-1]); return subprocess.CompletedProcess(args, 0, '', '')
                binary_args = [os.environ['COMPOSE_TEST_BINARY']] if os.environ.get('COMPOSE_TEST_BINARY') else ['docker', 'compose']
                return self.real_command(*binary_args, *args[2:], **kwargs)
            if args[0] == 'openssl': return self.real_command(*args, **kwargs)
            out = ''; code = 0
            if args[:2] == ('docker', 'context'): out = 'unix:///var/run/docker.sock'
            elif args[:2] == ('docker', 'info'): out = '{"ServerVersion": "28.0.0"}'
            elif args[:2] == ('docker', 'ps'): out = 'traefik opencloud'
            elif args[:2] == ('docker', 'image'): out = 'traefik-image' if args[3].startswith('traefik') else 'opencloud-image'
            elif args[:2] == ('docker', 'inspect'):
                if args[-1] in owned: out = 'true'
                elif args[2:] == ('traefik', 'opencloud'): out = json.dumps(selected)
                else: code = 1
            elif args[:2] == ('docker', 'run'): owned.add(args[args.index('--name') + 1]); out = 'id'
            elif args[:2] == ('docker', 'rm'): owned.discard(args[-1])
            elif args[:2] == ('docker', 'restart'): restarted.append(args[-1])
            elif args[0] == 'curl':
                out = 'HTTP/2 200\nAlt-Svc: h3=":443"; ma=60\n\n' if '-fsSI' in args else '{"apps": ["files"]}'
            return subprocess.CompletedProcess(args, code, out, '')
        def wait_h3(domain, port, require200):
            h3.append((domain, port))
            if fail and port == 443: raise RuntimeError('simulated public HTTP/3 failure')
        original_is_dir = Path.is_dir
        def is_dir(path): return True if str(path) == '/run/systemd/system' else original_is_dir(path)
        def which(name): return None if name == 'getenforce' else '/usr/bin/' + name
        with patch.object(core, 'STATE', state), patch.object(tf, 'TIMER', units / tf.TIMER.name), \
             patch.object(tf, 'SERVICE', units / tf.SERVICE.name), patch.object(core, 'command', command), \
             patch.object(tf.os, 'geteuid', return_value=0), patch.object(tf.os, 'chown'), \
             patch.object(core, 'replace_file', lambda p, text: p.write_text(text)), \
             patch.object(Path, 'is_dir', is_dir), patch.object(tf.shutil, 'which', which), \
             patch.object(core, 'find_binary', return_value=binary), patch.object(core, 'ports_free'), \
             patch.object(core, 'udp443_listeners', return_value=''), patch.object(core, 'wait_for_udp443', return_value=True), \
             patch.object(core, 'wait_h3', wait_h3), patch('builtins.input', side_effect=['', 'APPLY'] if free else ['', 'other.example.com', 'APPLY']):
            if direct and not free:
                with self.assertRaisesRegex(RuntimeError, 'UDP 443 is already occupied'): tf.install()
                self.assertEqual(source.read_text(), original); self.assertFalse(state.exists()); self.assertEqual(recreated, [])
                return
            if fail:
                with self.assertRaisesRegex(RuntimeError, 'public HTTP/3 failure'): tf.install(shared_router=not direct)
                self.assertTrue((state / 'diagnostics.json').is_file())
            else:
                tf.install(shared_router=not direct)
                if not free: self.assertIn('18443', source.read_text())
                self.assertTrue((units / tf.TIMER.name).is_file())
                self.assertIn(('cloud.example.com', 443), h3)
                if not free: self.assertIn(('other.example.com', 443), h3)
                core.rollback(state)
        self.assertEqual(source.read_text(), original)
        self.assertEqual(owned, set()); self.assertEqual(recreated, ['traefik', 'opencloud', 'traefik', 'opencloud'])
        self.assertFalse((state / 'certs/privkey.pem').exists()); self.assertTrue((state / 'ROLLED_BACK').exists())
        self.assertFalse((units / tf.TIMER.name).exists())

    def test_interactive_success_and_manual_rollback(self): self.lifecycle()

    def test_interactive_public_failure_restores_compose(self): self.lifecycle(fail=True)

    def test_interactive_free_udp(self): self.lifecycle(free=True)

    def test_interactive_direct_udp(self):
        # A middleware with unknown source-IP behavior is intentionally refused.
        original = self.projects
        def projects(*args, **kwargs):
            edge, app = original(*args, **kwargs)
            p = edge.files[0]
            text = p.read_text().replace('      traefik.http.routers.cloud.middlewares: existing@docker\n', '')
            p.write_text(text); edge.original[p] = text; edge.config = edge.render(); app.config = edge.config
            return edge, app
        with patch.object(self, 'projects', projects): self.lifecycle(free=True, direct=True)

    def test_direct_mode_stops_at_occupied_udp_without_changes(self): self.lifecycle(direct=True)

    def test_direct_clones_path_auth_and_preserves_global_middleware(self):
        edge, app = self.projects('["443:443/tcp"]')
        labels = app.config['services']['opencloud']['labels']
        labels.update({'traefik.http.middlewares.existing.headers.customresponseheaders.X-Test': 'keep',
                       'traefik.http.routers.protected.rule': 'Host(`cloud.example.com`) && PathPrefix(`/protected`)',
                       'traefik.http.routers.protected.tls': 'true', 'traefik.http.routers.protected.entrypoints': 'websecure',
                       'traefik.http.routers.protected.middlewares': 'auth', 'traefik.http.middlewares.auth.basicauth.users': 'test:hash',
                       'traefik.http.routers.private.rule': 'Host(`cloud.example.com`) && PathPrefix(`/private`)',
                       'traefik.http.routers.private.tls': 'true', 'traefik.http.routers.private.entrypoints': 'admin'})
        item = {'Config': {'Labels': {**labels, 'com.docker.compose.project': 'test', 'com.docker.compose.service': 'opencloud'}}}
        container = {'NetworkSettings': {'Ports': {'443/tcp': [{'HostPort': '443', 'HostIp': '0.0.0.0'}]}, 'Networks': {'net': {'Gateway': '172.18.0.1'}}}}
        result, update = tf.direct_settings(container, edge, app, [item], 'cloud.example.com')
        self.assertEqual(result['traefik.http.routers.och3-protected.middlewares'], 'opencloud-h3-experimental@docker,auth')
        self.assertEqual(result['traefik.http.routers.protected.entrypoints'], 'websecure')
        self.assertFalse(any(k.startswith('traefik.http.routers.och3-private.') for k in result))
        self.assertIn('172.18.0.1/32', '\n'.join(update['command']))
        self.assertNotIn('forwardedheaders.insecure', '\n'.join(update['command']))

    def test_direct_refuses_peer_ip_based_controls(self):
        item = {'Config': {'Labels': {'traefik.http.middlewares.allow.ipallowlist.sourcerange': '192.0.2.0/24'}}}
        with self.assertRaisesRegex(RuntimeError, 'TCP peer IP'): tf.check_middleware('allow', [item])
        item['Config']['Labels']['traefik.http.middlewares.allow.ipallowlist.ipstrategy.depth'] = '1'
        tf.check_middleware('allow', [item])

    def test_direct_refuses_unreviewed_provider_and_header_override(self):
        with self.assertRaisesRegex(RuntimeError, 'manual integration'): tf.check_middleware('security@file', [])
        item = {'Config': {'Labels': {'traefik.http.middlewares.spoof.headers.customrequestheaders.X-Forwarded-For': '127.0.0.1'}}}
        with self.assertRaisesRegex(RuntimeError, 'overrides client-IP'): tf.check_middleware('spoof', [item])


if __name__ == '__main__': unittest.main()
