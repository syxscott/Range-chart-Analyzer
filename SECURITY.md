# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Use GitHub's private vulnerability reporting:
`https://github.com/syxscott/Range-chart-Analyzer/security/advisories/new`

If that is unavailable, open a **draft** issue with no technical detail and
ask for a private channel. Include only: affected version, affected component,
and a one-line impact summary. We will follow up privately.

## What this tool handles

Users give it two sensitive things:

1. **Unpublished figures** from papers under review. Images are sent to an
   LLM API endpoint the user configured — the tool is local, but it is
   **not offline**. See the "where your data goes" note below.
2. **Paid API keys**, stored locally with Fernet encryption
   (`rca_core/secrets_store.py`), preferably in the OS credential store via
   `keyring`. When keyring is unavailable the key file is
   `~/.range_chart_analyzer/fernet_key.fek` (0600).

## Security properties that are intentional

| Property | Where |
|---|---|
| Endpoint validation / SSRF guard (incl. DNS rebinding, NAT64, IPv4-compatible IPv6) | `rca_core/ssrf.py` |
| Credential redaction of error bodies leaving the process | `rca_core/redact.py` |
| Encrypted provider config; a decrypt failure preserves the ciphertext instead of blanking the key | `rca_core/secrets_store.py` |
| CSRF token + Origin checks; startup warning that the server has **no authentication** | `server.py` |
| Constant-time comparison for the provenance shared secret | `server.py` |
| CodeQL `security-extended` on Python and JS | `.github/workflows/codeql.yml` |

## ⚠️ Two things users must know

**`RCA_ALLOW_PRIVATE=1` disables the ENTIRE private-IP check for every
endpoint in the process**, including the cloud metadata address
`169.254.169.254`. It is read **once at import time** (so setting it at
runtime does nothing) and it is **not** per-endpoint. It exists for people
who want to reach a local Ollama instance. On a cloud VM with an IAM role,
do not set it. Consider a firewall rule instead.

**Provider presets include third-party API relays.** The preset list in
`rca_core/llm.py` contains many hosts that are not the model vendor. If you
select one, both your **image** and your **API key** go to that host. Check
the endpoint before use; the tool does not currently classify presets by
trust level. This is tracked as a known gap in
`docs/GAP-ANALYSIS-2026-11-02.md`.

## Out of scope

The local HTTP server binds to loopback by default and has **no
authentication**. Binding it to `0.0.0.0` exposes the audit trail to anyone
who can reach the port; use the reverse proxy in `proxy/` and set
`RCA_PROVENANCE_TOKEN` as documented in the README. Reports that require an
attacker to already have local access to the user's machine are generally
out of scope for this single-user desktop tool.

## Supported versions

Only the latest release receives fixes. See
`https://github.com/syxscott/Range-chart-Analyzer/releases`.
