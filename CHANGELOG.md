# Changelog

## 1.10.0

- Enforce source-scoped admin tool access with a dedicated `meshcore_admin` toolset.
- Reject arbitrary remote commands; only documented read-only commands are accepted.
- Replace global IPC files with profile-scoped, atomic `0600` files and request IDs.
- Reject password-bearing cross-process IPC; passwords remain in-memory only.
- Align dashboard configuration with the adapter's scoped environment keys.
- Harden packet parsing, frame bounds, field lengths, and count validation.
- Add focused security/regression tests and CI coverage.

## 1.9.7

- Previous release.
