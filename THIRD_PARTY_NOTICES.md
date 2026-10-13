# Third-party components

This package includes modified copies of two MIT-licensed upstream projects. Their copyright notices and full licenses are retained in the corresponding vendor directories.

| Directory | Source | Pinned upstream commit | License |
| --- | --- | --- | --- |
| `vendor/live-bridge` | https://github.com/Kxiandaoyan/workbuddy-acp-bridge | `3f2f52e7a03b32c455b1718ad43730177472e2ff` | [MIT](vendor/live-bridge/LICENSE), copyright 2026 WorkBuddy ACP Bridge contributors |
| `vendor/desktop-bridge` | https://github.com/gosick233-cloud/Codex-WorkBuddy-Desktop-Bridge | `c00a8e5eab809a97963277e71ec008abb3d372ae` | [MIT](vendor/desktop-bridge/LICENSE), copyright 2026 gosick233-cloud |

Local adaptations include closing temporary ACP discovery connections, support for version-6 UUIDs in endpoint sidecars, bounded connection cleanup, and standalone-client compatibility experiments retained in the vendored client. The production entry point is `local_bridge.py`; it uses the already running native session and does not start the standalone client.

The local adapter adds per-connection `session/load` for WorkBuddy 5.8.2, scoped permission responses, serialized access to a dedicated native chat, cancellation and uncertain-submission checks. Portable configuration and installation are in `configure.py` and `setup.ps1`.

Version 0.1.1 uses TCP-only status discovery, retries only an explicit pre-prompt writer-occupied rejection with the client's `takeWriter` metadata after a fresh idle check, and reports ACP connection cleanup HTTP failures. It never retries an uncertain prompt or bypasses snapshot authentication.

Runtime dependencies are installed from `requirements.txt`: httpx 0.28.1 and mcp 1.30.0. They and their transitive dependencies retain their own licenses; their binaries and virtual environments are not bundled.

WorkBuddy and Codex are products of their respective owners. This project is a community integration and is not represented as an official integration.
