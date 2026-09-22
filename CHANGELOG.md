# Changelog

## 1.11.0

- Support Hermes singular multiplexed gateways with a per-profile adapter registry.
- Remove the process-wide MeshCore adapter singleton that prevented multiple profiles.
- Resolve the active adapter by profile context for tool handlers.
- Derive profile-scoped IPC state from `HERMES_HOME` when `HERMES_PROFILE_DIR` is not explicitly set.
- Align the dashboard manifest version with the plugin release.

## 1.10.1

- Fix package-relative `meshcore_utils` imports when Hermes loads the plugin as a package.
- Retain a narrow top-level import fallback for the standalone test runner.
- Restore passwordless cross-process admin-query submission while keeping gateway-side authorization.
- Tighten parser boundary validation and add regression coverage for malformed frames.
- Fix dashboard radio-stat key mapping and CI Python executable resolution.

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
