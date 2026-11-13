# Third-party components

The installer is MIT licensed. The bundled `rpxy` executable is a patched build
of [rust-rpxy](https://github.com/junkurihara/rust-rpxy), MIT licensed by Jun Kurihara.
Its license is included in `LICENSES/rpxy-MIT.txt`.

The build uses [zerox80/h3](https://github.com/zerox80/h3) and
[zerox80/quinn](https://github.com/zerox80/quinn). These projects and their
dependencies retain their own license terms. Docker images are pulled from
their respective publishers using fixed digests.

The source patch, build instructions and exact Git revisions used for the
provided executable are included in this repository.

## Traefik adapter dependency

The Traefik adapter vendors the pure-Python `ruamel.yaml` 0.19.1 package under
the MIT license. See `LICENSES/ruamel.yaml-MIT.txt` and `vendor/README.md` for the
original license, download source and verified wheel checksum. It is bundled
so users do not need to install Python packages globally.
