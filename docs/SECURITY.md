# Paddock security model

**Goal:** your media and files stay on hardware you own; nothing about them is exposed to the internet; every
device, server and AI agent gets only the access it needs, can be cut off individually, and leaves an audit trail.

## Network exposure
| Surface | Who can reach it |
|---|---|
| HTTPS :8443 (web, API, MCP) | Home LAN ranges, the WireGuard tunnel subnet, loopback, and linked servers' tunnel IPs (those may only call `/api/peer/*`). Everything else gets 403 — even if someone forwards the port by mistake. |
| UDP :51820 (WireGuard) | The internet. WireGuard never answers unauthenticated packets, so the port looks closed to scanners. |
| Inside the tunnel | The sidecar firewall lets tunnel peers reach **only** Paddock's HTTPS port — not the NAS UI, SSH, SMB, or anything on your LAN; nothing is forwarded. Each peer is a single /32, and phones route only the server's tunnel IP. |

No inbound port is needed for anything else; no traffic goes to Paddock-the-project or any cloud service.
Optional outbound connections only happen if you enable them: Claude API (labelled "cloud"), Docker image pulls.

## Identity & transport
- Private CA generated on first boot (ECDSA P-256, key identifiers per RFC 5280). Phones pin the CA's SPKI from
  the pairing QR code; linked servers pin each other's CA by fingerprint from the invite. No public CA, no ACME.
- HSTS, strict CSP (`script-src 'self'`, no inline scripts), `frame-ancestors 'self'`, nosniff, no referrer,
  COOP/CORP same-origin; static assets revalidate so upgrades never mix code versions.

## Accounts, devices, tokens
- Passwords: scrypt (N=2¹⁶, r=8) with per-user salt; constant-time compare; same error and timing for unknown
  users; exponential back-off per IP and per username.
- Web sessions: random 256-bit tokens in `HttpOnly; Secure; SameSite=Strict` cookies; cookie-authenticated
  writes also require a custom header (blocks cross-site requests). Changing a password ends other sessions.
- Only SHA-256 hashes of every token (web, device, agent, peer, invite, pairing code) are stored.
- **Pairing a phone:** one-time code, 10-minute expiry, accepted only from the home network, rate-limited;
  the phone generates its own WireGuard key — the private key never leaves it; the device token lives in
  Android Keystore-encrypted storage and is excluded from cloud/device-transfer backups.
- **Revoking** a device or linked server kills its token and removes its tunnel peer immediately.

## AI agents (MCP) and the built-in assistant
- Scoped tokens: `read`, `write`, `admin`; admin scope is ignored unless the issuing user is an admin.
  Security/account endpoints refuse agent tokens entirely.
- The assistant acts **as the signed-in user** and is read-only until that user turns on "Let the assistant
  make changes".
- No tool can permanently delete. The worst case is "move to trash", which a person reverses or empties
  (typed confirmation; admins only; never via tokens).
- Every write by a person, device, agent or server is in the audit log with its identity.
- DNS-rebinding guard on `/mcp` (Origin check). Tool inputs are schema-validated; results are size-capped.

## Files
- Every file is addressed by ID and resolved inside its library; symlinks escaping a library, `..` paths and
  folders outside the configured media roots are refused. Media can (and should) be mounted read-only.
- Uploads are chunked, size-capped, SHA-256 verified before being filed; names are sanitised.
- Share links are random, hashed, expiring, optionally download-capped, and only reachable where Paddock is.
- Printers: only private LAN addresses are accepted (no SSRF to the internet).

## Backups to linked servers
- Content is split into 8 MB chunks; each chunk is named with HMAC-SHA256 (keyed) and encrypted with AES-256-GCM
  (name as associated data) on the sending server. Manifests are compressed and encrypted the same way.
- The receiving server stores opaque blobs within a quota you set; it cannot read names, paths or contents.
- Restores verify every chunk (GCM tag + keyed hash). A recovery key (shown only after re-entering your
  password, and audited) lets a new server restore from any linked server.

## Containers
- App container: non-root (uid 568), read-only root filesystem, `cap_drop: ALL`, `no-new-privileges`.
- WireGuard sidecar: only `NET_ADMIN`, read-only root, no listening services; reads configs via group
  permissions (it holds no file-override capability).

## What you should still do
- Snapshot the `apps/paddock` dataset (database, keys) and keep the backup recovery key offline.
- Keep TrueNAS and the app images updated.
- Use a strong admin password; give family members non-admin accounts.
