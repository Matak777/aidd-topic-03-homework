# Upstream SDK provenance

This package contains the Python source from the official Tinkoff Invest SDK:

- Repository: https://github.com/Tinkoff/invest-python
- Upstream commit: `1cc84950266d62cd0bc7a43ad764dddfd4252630`
- Upstream version: `0.2.0-beta59`
- License: Apache License 2.0; see [LICENSE](./LICENSE).

The `tinkoff/` source tree is copied unchanged from that commit. The upstream
`pyproject.toml` declares a runtime dependency named `tinkoff>=0.1.1`; that
distribution is unavailable from the configured PyPI index. The local package
metadata omits that unresolvable dependency, provides a local package
description and points to the copied upstream README. The remaining runtime
dependencies are declared with equivalent compatible version ranges and are
locked by the application. These are packaging-only changes; there is no
placeholder package, namespace shim, or edit to installed environment
metadata.
