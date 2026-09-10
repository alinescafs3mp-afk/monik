# Local candidate 0.2: validation and publication status

The verified starting point is commit `43345b6db4552e60828c0d9380f3eb1e8da14f03`, tree `473436660b9f91470274f6c2edf34e01e31846a6`.

A local candidate integrates the terminal client, improves Russian presentation and live navigation, validates malformed limit data, and adds regression tests. It preserves configuration and database schema version 1.

Validation on Python 3.13.5 / SQLite 3.46.1: 59 unit/integration tests, 58 passed and one native Landlock test skipped; no expected failures. Separate suites passed 11 fixture checks, 23 real loopback HTTP/SSE checks, and 39 offline DOM/UI checks. The terminal client was exercised through a real PTY and HTTP server.

Native browser navigation was blocked by the build environment. Native Landlock, the owner's actual Codex sessions, systemd/HTTPS and a physical Android device were not accepted here. The offline browser transport is a test bridge, not a native browser-network pass.

Publication of the updated normalizer and the CLI/package group was rejected by the publication tool. The candidate tree was therefore NOT assigned to main. This document is a status record, not a declaration that main contains the candidate or that production acceptance is complete.

The source bundle delivered in the conversation is named `monik-0.2.0-local-verified.zip`. Its README, source manifest and `docs/ACCEPTANCE_0.2.md` describe that local candidate. The older reports in this repository must not be represented as acceptance of unpublished changes.
