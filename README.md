# OpenCloud HTTP/3 setup

Interactive, experimental HTTP/3 setup for an **existing OpenCloud installation
behind host Nginx or Docker Compose Traefik**. It uses rpxy with the zerox80 H3
and Quinn forks.

**Experimental:** local Podman routing tests and simulated installer tests have
passed. A complete successful production cutover has **not** been verified.
The Nginx mode and optional shared-router Traefik mode use a QUIC SNI router
whose maintainer does not recommend production use. Default Traefik mode
does not use that router.

## What changes with host Nginx

- OpenCloud's exact hostname gets an HTTP/3 route through Quinn to its local
  OpenCloud upstream.
- Nginx continues to serve TCP 443, including HTTP/1.1 and HTTP/2 fallback for
  OpenCloud, Euro Office and other sites.
- Other domains' existing QUIC traffic is forwarded to Nginx. They are not
  converted to rpxy or forced to use HTTP/3.
- When Nginx already owns UDP 443, its QUIC listeners move to an internal port
  and an SNI router takes the public UDP listener. Public URLs keep port 443.

**Nginx Proxy Manager is not supported.** The Nginx mode edits host files under
`/etc/nginx`; it must not be used to edit NPM-generated configuration.

## Traefik mode

The complete bundle detects a Traefik container publishing public TCP 443.
You can also select it explicitly:

```bash
sudo python3 setup.py --traefik
```

OpenCloud's HTTP/3 traffic uses the bundled **Quinn fork**, then verified HTTPS
to a private Traefik HTTPS entry point published only at `127.0.0.1:19444`.
Quinn binds **UDP 443 directly**, using the host network and a small UDP-only
extension included as a source patch. No UDP relay or Docker port-forwarding
layer sits in front of Quinn. Traefik continues serving TCP 443 for OpenCloud,
Euro Office and every other site.

**If UDP 443 is occupied, default mode stops without migrating the listener.**
Free that UDP port or use a separate public IP. TCP 443 may remain occupied by
Traefik.

Quinn derives forwarding headers from the actual peer address, discarding
untrusted client-supplied forwarding headers. The private Traefik entry point
trusts only loopback and the selected Traefik bridge gateways; it does not enable
`forwardedHeaders.insecure` or change public entry-point trust. Matching TLS
routes, including path-specific authentication routes and entry-point HTTP
middleware, are copied to that private entry point. Only OpenCloud's selected
routers get the new Alt-Svc middleware.

The original client IP is available in validated forwarding headers at the
backend. As with other HTTP reverse proxies, Traefik's TCP peer is the proxy;
middleware that uses the raw peer address needs an explicit forwarded-IP
strategy. The installer **refuses** automatically integrating default peer-IP
allowlists/rate limits, unknown plugins and provider middleware it cannot inspect.
It does not silently weaken these policies. Traefik access logs may need
configuration to record the validated forwarding header instead of the TCP peer.

Traefik requirements and automatic-setup boundaries:

- Local, rootful Docker **28+**, Compose, bridge networking, one container per selected
  service, Linux x86-64, Python 3.9+, OpenSSL, curl, `ss` and systemd.
- Traefik publishes TCP 443 on all interfaces. OpenCloud returns its normal
  `/config.json` through this route and has an exact ``Host(`domain`)`` TLS router
  in Docker labels, with an explicit single public `entrypoints` label.
  Simple matching path-specific TLS routes on the same service are preserved.
  Wildcard/cross-service/complex rules and file-provider-only routers need manual
  integration. Static Traefik settings must use `--option=value` Compose command
  arguments; file/environment static configuration needs manual integration.
  Entry-point-default TLS without explicit router TLS labels also needs manual
  integration.
- The original Compose files and environment must match the running services.
  The script validates config hashes and refuses pending changes or changed
  local image tags. Standard multiple `-f` files and `.env` files are supported;
  inaccessible custom launch environments are rejected.
- UDP 443 must be free. Host networking for Traefik, Swarm and unknown UDP owners
  are rejected without changing them. The Quinn component itself uses host
  networking, read-only files and only the `NET_BIND_SERVICE` capability.
- A valid certificate is discovered in mounted Traefik ACME JSON storage or
  existing Certbot files. An encrypted/private external certificate store needs
  manual integration. No new certificates are issued.

The script backs up the source YAML and validates the resolved Compose changes
before applying them. It recreates only the selected Traefik/OpenCloud services
with `--no-deps --no-build --pull never`. **This briefly interrupts those
services and connections through Traefik.** Review this before typing `APPLY`.
Other Compose services are not recreated.

A systemd timer checks the original certificate store every five minutes,
verifies renewed certificates and key pairs, and restarts the Quinn component
when the certificate changes. It does not restart Traefik for renewal. Inspect
it with `systemctl status opencloud-h3-cert-sync.timer` and
`journalctl -u opencloud-h3-cert-sync.service`. Rollback removes these units,
restores the original YAML and recreates the two selected services.

### Optional shared-router mode

`sudo python3 setup.py --traefik-shared-router` explicitly selects the older
experimental routing mode. It **loses original UDP client IPs**. When Traefik
publishes UDP 443, its literal short/long Compose mapping moves to
`127.0.0.1:18443:<existing-container-port>/udp`. An SNI router takes public UDP
443 and forwards other domains to Traefik. The existing Traefik HTTP/3 endpoint
is tested before and after switching. This is separate from default direct mode.

## Download and run

For Linux x86-64, download the complete bundle from
[Releases](https://github.com/zerox80/opencloud-h3-setup/releases).
The bundle includes the HTTP/3 component. No component file paths need to be
entered, and no real certificates or credentials are included.

```bash
tar -xzf opencloud-h3-setup.tar.gz
cd opencloud-h3-setup
sha256sum -c SHA256SUMS
sudo python3 setup.py
```

The script looks up your OpenCloud domain, matching Let's Encrypt certificate
and local HTTP upstream from the existing configuration. Press Enter to accept
suggestions. Advanced connection settings are optional. Review the proposed
file changes and type `APPLY` to start.

If old Nginx workers still hold UDP 443 after reload, the installer waits and
then asks for `RESTART`. **Restarting Nginx briefly interrupts existing
connections on all host Nginx sites.** Press Enter instead to roll back.

Host Nginx mode requirements:

- Linux x86-64, root, Python 3.9+, Docker, Nginx with QUIC support, OpenSSL,
  curl and `ss`.
- Existing OpenCloud reachable over HTTP on `127.0.0.1:<port>`.
- Existing valid Let's Encrypt certificate and Certbot deploy-hook directory.
- Incoming UDP 443 allowed by host and provider firewalls. The script does not
  change firewall rules.

## Rollback and retries

```bash
sudo python3 setup.py --rollback
```

Backups, configuration and diagnostics are kept under
`/var/lib/opencloud-h3-experimental`. Failed attempts can be retried after a
verified rollback; the old directory is archived rather than overwritten.
Rollback refuses to overwrite configuration changed by another process or to
remove containers that do not have the installer's ownership label.

A Certbot deploy hook updates the proxy's copy of the certificate after renewal.
The copied private key is restricted to the proxy user and removed on rollback.

## Limits that matter

- In Nginx/shared-router mode, the UDP router does **not** preserve the client IP at backend
  services. Logs, IP-based rate limits and access controls can be affected.
- QUIC with Encrypted ClientHello is not supported by the shared SNI router.
- An occupied UDP 443 is migrated only for the selected supported proxy: host
  Nginx or the detected Compose Traefik service. Other owners are rejected.
- The default proxy request-body limit is `64m`. It applies per request,
  including unchunked WebDAV uploads. It can be changed in advanced settings.
- External UDP connectivity, authentication, real uploads, Office editing and
  every affected site's behavior still require verification after installation.
- This adds an HTTP/3 route; it does not establish that HTTP/3 is faster or that
  it fixes an existing upload issue.

## Tested behavior

Real, isolated Podman tests exercised HTTP/3 routing for two dummy domains using
the server-built executable, a shared UDP entry point and restoration of the
previous listener. A privileged-port binding problem found by the test was fixed.

Separate containers simulate installer execution and cover automatic discovery,
Certbot includes, free/occupied ports, failed post-switch checks, old listeners,
restart refusal/approval, certificate renewal, retries and rollback safeguards.
Those simulations do not constitute a complete real-VPS installation test.

The optional shared-router Traefik lab uses real Traefik 3.7.19, Quinn, a private lab CA and loopback
Docker-style port mappings. It verifies both HTTP/3 hostnames, verified HTTPS
between Quinn and Traefik, preservation of an existing response middleware,
an unauthenticated protected route returning 401, TCP HTTP/2 and restoration of
the original UDP mapping. Separate tests use the real Compose renderer and
OpenSSL to verify YAML edits, split files, certificate validation/renewal,
restart retries and rollback safeguards. The full interactive Traefik installer
has not been run through a production Docker/systemd cutover.

The direct-mode lab uses a separate pod with real TCP and UDP 443, two client
source IPs and deliberately forged forwarding headers. It checks IP propagation,
allow/deny behavior of a forwarded-IP allowlist, a path-specific authentication
route, global middleware and both OpenCloud/Office TCP routes.

## Build and test

The executable is a fixed snapshot, not a moving download:

| Component | Git revision |
| --- | --- |
| rust-rpxy | `ba492b585fe86a279dbaab467d521f2af398027f` |
| zerox80/h3 (`master`) | `e186d95ab35a886fdf4cb4daf27670d17584f348` |
| zerox80/quinn (`main`) | `c752d53293fcc000de5e09c634e1b25e20d54f5b` |

`patches/rpxy-h3-only.patch` adds a validated `h3_only` option and exclusive UDP
binding for direct mode. It leaves normal combined TCP/UDP operation unchanged
when this option is false. It is applied after the fork compatibility patch.

With Rust 1.95+ and the native build prerequisites installed:

```bash
bash scripts/build-rpxy.sh
bash scripts/podman-lab.sh dist/rpxy-opencloud-linux-amd64
bash scripts/traefik-podman-lab.sh dist/rpxy-opencloud-linux-amd64
bash scripts/traefik-direct-lab.sh dist/rpxy-opencloud-linux-amd64
python3 -m unittest discover -s tests -v
```

The weekly workflow in `zerox80/h3` checks its current Quinn dependency without
creating weekly commits. It does not update this executable or deploy software.

The repository's own CI can also be started manually under **Actions → Checks →
Run workflow**. It runs installer simulations, actual Compose rendering and
certificate tests. It does not run against your live host.

See [third-party notices](THIRD_PARTY_NOTICES.md) for component licensing.
