# OpenCloud HTTP/3 setup

Interactive, experimental HTTP/3 setup for an **existing OpenCloud installation
behind host Nginx**. It uses rpxy with the zerox80 H3 and Quinn forks.

**Experimental:** the QUIC SNI router's maintainer does not recommend production
use. Local Podman routing tests and simulated installer tests have passed. A
complete successful production cutover has **not** been verified.

## What changes

- OpenCloud's exact hostname gets an HTTP/3 route through Quinn to its local
  OpenCloud upstream.
- Nginx continues to serve TCP 443, including HTTP/1.1 and HTTP/2 fallback for
  OpenCloud, Euro Office and other sites.
- Other domains' existing QUIC traffic is forwarded to Nginx. They are not
  converted to rpxy or forced to use HTTP/3.
- When Nginx already owns UDP 443, its QUIC listeners move to an internal port
  and an SNI router takes the public UDP listener. Public URLs keep port 443.

**Nginx Proxy Manager is not supported.** This installer edits host Nginx files
under `/etc/nginx`; it must not be used to edit NPM-generated configuration.

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

Requirements:

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

- The UDP router does **not** preserve the original client IP at backend
  services. Logs, IP-based rate limits and access controls can be affected.
- QUIC with Encrypted ClientHello is not supported by this routing setup.
- An occupied UDP 443 is migrated only when it belongs to host Nginx. Other
  owners are detected and rejected without automatic migration.
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

## Build and test

The executable is a fixed snapshot, not a moving download:

| Component | Git revision |
| --- | --- |
| rust-rpxy | `ba492b585fe86a279dbaab467d521f2af398027f` |
| zerox80/h3 (`master`) | `e186d95ab35a886fdf4cb4daf27670d17584f348` |
| zerox80/quinn (`main`) | `c752d53293fcc000de5e09c634e1b25e20d54f5b` |

With Rust 1.95+ and the native build prerequisites installed:

```bash
bash scripts/build-rpxy.sh
bash scripts/podman-lab.sh dist/rpxy-opencloud-linux-amd64
```

The weekly workflow in `zerox80/h3` checks its current Quinn dependency without
creating weekly commits. It does not update this executable or deploy software.

The repository's own CI can also be started manually under **Actions → Checks →
Run workflow**. It runs installer simulations with fake system commands, not
against your live host.

See [third-party notices](THIRD_PARTY_NOTICES.md) for component licensing.
