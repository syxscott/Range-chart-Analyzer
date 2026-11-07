# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Use GitHub's private vulnerability reporting:
`https://github.com/syxscott/Range-chart-Analyzer/security/advisories/new`

If that is unavailable, open a **draft** issue with no technical detail and
ask for a private channel. Include only: affected version, affected component,
and a one-line impact summary. We will follow up privately.

## After a fix ships

A merged fix is not the end of a vulnerability: users on older releases still
need to know they are affected. This is the release-side counterpart to the
private reporting channel above.

### When to open a GitHub Security Advisory

Draft one **as soon as a fix is merged into a release branch**, keep the draft
private while the release is being built, and **publish it at the same time as
the fixing release** — never before. Open one for:

- Any issue reported privately through the channel above. A private report must
  not be converted into a public issue or a bare CHANGELOG line.
- Any data-correctness defect that changes already-exported output (see the
  project-specific note below), even when found internally.
- Any confidentiality or integrity impact on the two sensitive inputs in
  "What this tool handles" (unpublished figures, paid API keys).

Use `https://github.com/syxscott/Range-chart-Analyzer/security/advisories/new`.
For low-severity items needing no user action, a plain release note is enough —
but record that decision in the PR or advisory draft rather than silently
skipping it.

### Affected-version table

Copy this into the advisory and the release notes, one row per affected line:

| 版本号 / Version | 受影响区间 / Affected range | 修复版本 / Fixed in | 是否需重新导出数据 / Re-export required? |
|---|---|---|---|
| 例：v0.1.4 | `>=0.1.0, <0.2.0` | `0.2.0` | **是** — PBDB 数据正确性缺陷，见下 |
| | | | 否 / 是（写明哪些导出、如何核对） |

Answer the last column **explicitly for every row**: `否` is a statement, not a
blank. Whenever it is `是`, link to the exact re-export and re-check steps in
the same document — do not make the user go looking for them.

### When to request a CVE

- Request one (GitHub can assign a CVE ID from a published advisory) when a
  **shipped release** has a security vulnerability with a plausible attack
  path for this single-user desktop tool: credential or key exposure, an SSRF
  guard bypass, a redaction failure, or an exposure of the documented
  unauthenticated local server that is in scope per "Out of scope" below.
- Do **not** request one for pure data-correctness defects, crashes, or
  usability bugs with no confidentiality/integrity impact. Those still get an
  advisory and a prominent release note, but not a CVE — a CVE is not a
  severity badge and must not be used to make a correctness bug look urgent.
- One CVE per distinct root cause, not one per affected version.

### CHANGELOG and release notes

- CHANGELOG: file the entry under **Fixed** (or **Security**) of the fixing
  version, name the affected range, and state plainly what the user must do.
  Do not bury a security fix under "Changed" or "Refactor".
- Release notes: restate the affected-version table above, link the published
  advisory, and repeat any re-export instruction verbatim. Many users read
  release notes and never the CHANGELOG.
- Cross-link both ways: the advisory links the release tag, and the release
  notes link back to the advisory.

### Project-specific: data correctness requires re-export guidance

This tool publishes scientific data (PBDB / range-chart exports), so wrong
output can hurt a user more than a crash. **v0.1.4** was built without
`range_base_label` / `not_drawn` in `rca_core/standards/pbdb.py`: its PBDB
export published the **section's** age span as every taxon's FAD/LAD and
treated `response_kind: "not_drawn"` ("examined, not found") as a real
occurrence. Both were fixed for **v0.2.0**, whose release notes tell anyone
who exported with v0.1.4 or earlier to **re-export and re-check** that data.

Therefore **any security advisory involving data correctness must also carry
an explicit re-export / re-check instruction**: which versions produced bad
output, which artefacts are affected (XLSX, JSON, PBDB export), and how to
verify the corrected output. A code fix does not repair data the user already
exported; never leave that implicit.

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
