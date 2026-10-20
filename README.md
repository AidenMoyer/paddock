<p align="center"><img src="server/paddock/web/static/icon.svg" width="96" alt="Paddock"></p>

<h1 align="center">Paddock</h1>

A private, self-hosted vault for **movies, TV, music, photos, home videos, RED/cinema footage, CAD, 3D-print
and electronics files**, with a web app for watching and managing everything, a native Android app, local AI,
live transcription, encrypted server-to-server backup, and an MCP server so agents like OpenClaw can help
manage it.

Run it on its own minimal operating system (**Paddock OS**), as an app on **TrueNAS SCALE**, or on any Docker
host. Nothing goes through anyone else's servers.

## Download

Everything is on the [latest release](https://github.com/AidenMoyer/paddock/releases/latest):

| | |
|---|---|
| **Paddock OS** (`paddock-os-*.iso`) | A dedicated server in one USB stick: a stripped-down Debian 13 that runs Paddock and nothing else. Software RAID, drive health, network and power are managed from the web. Intel and AMD GPU transcoding. |
| **Paddock OS, NVIDIA edition** (`paddock-os-nvidia-*.iso`) | The same, plus the NVIDIA driver for NVENC/NVDEC (GTX 900-series and newer). |
| **TrueNAS app** (`paddock-truenas-*.yaml`) | Paste into *Apps → Discover Apps → ⋮ → Install via YAML*. |
| **Android app** (`paddock-*.apk`) | Android 8 or newer. |
| **Paddock Node** (`paddock-node-*.yaml`) | The partner server for off-site backups (any Docker host). |

Container images: `ghcr.io/aidenmoyer/paddock` and `ghcr.io/aidenmoyer/paddock-wg`. Every release lists
SHA-256 checksums, and every ISO is installed and boot-tested in a virtual machine (BIOS and UEFI, including
building a RAID 1 pool) before it's published.

```
 Phone (native app)                     Browser (web app + full admin)
   │  at home: HTTPS on the LAN            │
   │  away: app-only WireGuard tunnel ─┐   │
   ▼                                   ▼   ▼
 ┌──────────────── Paddock OS or TrueNAS ─────────────────────────┐
 │  paddock-wg  ── one UDP port; firewalls tunnel → Paddock only   │
 │  paddock     ── catalog · streaming/transcoding (GPU) · uploads │
 │               · projects · AI tools · MCP · backups · sharing   │
 │  Ollama (Qwen3.8 27B)                        ← optional, local  │
 │  drives / RAID pools / ZFS datasets: media · photos · footage   │
 └──────────────┬──────────────────────────────────────────────────┘
                │ WireGuard + pinned TLS, end-to-end encrypted chunks
                ▼
        Paddock Node (offsite backup / a friend's server)
```

## What it does

| | |
|---|---|
| **Watch** | Movies, shows (seasons/episodes), music (albums, mini-player), home videos. Player with **Auto** (adaptive), **Original** (untouched bitstream) and 4K→360p quality you can switch any time; GPU transcoding (NVENC / Quick Sync / VAAPI) with automatic CPU fallback; segment cache, encode-ahead with throttling, optional pre-transcode; scrub previews; resume; PiP; keyboard shortcuts. Originals are never modified. |
| **Live captions** | Local Whisper transcribes just ahead of the playhead on web and Android; finished transcripts make *spoken words searchable*. |
| **Photos** | Phone backup (resumable, deduplicated, keeps GPS), day timeline, “On this day”, places (no map service), full-screen viewer. |
| **RED / cinema** | R3D clips cataloged as one item (all spans), camera/reel/clip from names, RMD look data, REDline metadata & proxies when installed, BRAW, shoot-day clip logs, verified **card offload** with checksums. |
| **Engineering** | STL/3MF/OBJ/STEP/IGES/SolidWorks/Fusion/FreeCAD/DXF/G-code/KiCad. Server-rendered thumbnails, WebGL 3D viewer, G-code toolpath preview, print stats, STEP header data, **part families** (all revisions + exports), drop-folder **auto-filing**, send G-code to Klipper/OctoPrint, expiring share links. |
| **Projects** | Gather footage, photos, audio, CAD and notes per build/trip; day-by-day timeline; **AI video plan** (hook, beats, shot list mapped to your clips, missing shots, voice-over, music). |
| **AI** | Built-in assistant with tool calling over your library (read-only unless you allow changes, never deletes). Local by default: **Qwen3.8 27B** via Ollama or vLLM; Claude optional. Auto-tagging/captions from thumbnails. |
| **MCP** | `https://<paddock>:8443/mcp` — scoped tokens, audit log. Works with OpenClaw, Claude Desktop/Code, Cursor… |
| **Linked servers** | Pair other Paddock servers or **Paddock Node** (sister app) by invite + safety phrase; **encrypted, deduplicated, incremental backups** with retention, verify and restore; share libraries between servers. |
| **Admin** | Everything from the web UI: setup, libraries (folder picker), users, devices (QR pairing), remote access (auto router mapping), AI models (download/load/unload, VRAM), transcoding, transcription, printers, linked servers & backups, jobs, sessions, audit log, trash. |

## Install Paddock OS

1. Write the ISO to a USB stick with [balenaEtcher](https://etcher.balena.io/), Rufus (choose *DD image*
   mode) or `dd`. Turn **Secure Boot off** in the computer's firmware settings (for now).
2. Boot from the stick and choose **Install Paddock OS**. Pick the disk for the system (16 GB or more, an SSD
   is ideal), give the server a name, and optionally a console password. **Try Paddock OS** runs it from the
   stick without installing anything.
3. Remove the stick and restart. The server's screen shows the **Paddock OS console**: a live dashboard with
   the address (e.g. `https://192.168.1.50` or `https://paddock.local`), health, pools and drive status.
   Press **S** there to create your admin account at the server, or open the address in a browser on your
   network, accept the certificate once, and create it there.
4. **Manage server → System & drives**: drives you plug in show up there. Combine them into a **pool**
   (mirror / RAID 1, parity / RAID 5, double parity / RAID 6, RAID 10 or striped / RAID 0), or format or mount
   single drives. Each pool or drive appears under `/srv/media` with an **Add as library** button. The page
   also shows drive health (SMART), rebuild progress and failed drives, and lets you replace or add drives,
   run integrity checks, set a fixed IP address (with automatic undo if you lose access), change the name and
   time zone, read logs, and restart or shut down.

**Fully offline, at the machine:** press Enter on the console and sign in with a Paddock admin account for the
same management without any browser or network: first-time setup, drives & RAID pools, libraries, network
(DHCP or fixed address), name & time zone, Paddock service & logs, accounts (including resetting a forgotten
password at the console), restart / shut down and a root shell. It uses the same checked operations as the
web page, and if Paddock itself isn't running you can still repair the machine from there.

Paddock OS is Debian 13 with no desktop, no SSH and no extra services: about 1 GB installed. Paddock runs as an
unprivileged user; the few root actions go through a small agent that only accepts a fixed list of checked
operations. Your data lives on its own partition, so **re-running the installer offers an upgrade that keeps
your accounts, settings and libraries**.

## Install on TrueNAS SCALE

1. Create datasets: `apps/paddock` (Paddock's data — **snapshot it**), plus your media datasets. Give the
   `apps` user (568) read access to media, write access to the photo-backup and engineering datasets.
2. **Apps → Discover Apps → ⋮ → Install via YAML**, paste `paddock-truenas-<version>.yaml` from the
   [latest release](https://github.com/AidenMoyer/paddock/releases/latest), and edit the marked lines (dataset
   paths, hostnames, timezone, GPU groups).
3. Open `https://<nas-ip>:8443`, accept the certificate once (or install `paddock-ca.crt` from
   *Manage server → Remote access*), create the admin account, add libraries.

On TrueNAS (or any Docker host), drives, pools and the network are managed by the host; Paddock's *System*
page shows status only. To build the images yourself: `docker build -f deploy/Dockerfile -t paddock .` and
`docker build -t paddock-wg deploy/wireguard`.

### Transcoding & FFmpeg
Paddock ships its **own FFmpeg build** ([`ffmpeg/build.sh`](ffmpeg/build.sh)), compiled from the official,
signature-verified FFmpeg source with only what Paddock uses: Intel Quick Sync and VAAPI, AMD VAAPI, NVIDIA
NVENC/NVDEC with CUDA scaling, x264, dav1d, zimg tone-mapping and the whisper filter. Network protocols,
capture devices and players are left out.

### Remote access (one UDP port, or none to configure)
*Manage server → Remote access* → turn on **Open the port automatically**. Paddock asks your router (NAT-PMP,
then UPnP) to forward the WireGuard UDP port and learns your public IP from the router — no DDNS or
“what's my IP” service. If your router doesn't allow that, add a single manual rule (UDP 51820 → the NAS).
Never forward 8443; Paddock refuses web requests from the internet anyway. Behind CGNAT, link a Paddock Node
that has a public address, or ask your ISP for a public IPv4.

### Phone
Install `paddock-<version>.apk` from the [latest release](https://github.com/AidenMoyer/paddock/releases/latest)
(allow installing apps from your browser when Android asks), then on your home Wi-Fi: *Your account → Connect a
phone* → scan. If you had a self-built test version installed, uninstall it first — release builds are signed
with a different key. The app pins the server's certificate from the QR code,
generates its own tunnel key, and from then on picks the LAN at home and its **app-only tunnel** elsewhere
(Android asks once to allow the VPN; only Paddock's traffic uses it, and it can only reach Paddock).

### Local AI (Qwen3.8 27B)
Install the TrueNAS **Ollama** app with your GPU and these env vars:
`OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 OLLAMA_NUM_PARALLEL=1 OLLAMA_KEEP_ALIVE=30m`.
In *Manage server → Assistant* pick **Qwen3.8 27B · 24 GB GPU**, set the URL, **Download model**, **Load now**.
Q4_K_M is an 18 GB download and fits a 24 GB card at ~32K context; Paddock shows how much is actually on the
GPU and warns if it spills to CPU. For more speed use vLLM with an AWQ/INT4/NVFP4 quant (OpenAI-compatible
preset). One model does chat, planning and image tagging, requests are serialized with interactive chat first,
and thinking is on only where it helps. Claude is available as an explicit, labelled opt-in.

### Live transcription
Built in: *Manage server → Transcription → Built-in (ffmpeg whisper)* and **Download** a model (each is checked
against its published SHA-256, then everything runs offline). *Large v3 Turbo (compressed)* is a good default.
For GPU speed on TrueNAS, uncomment the `speaches` service in the compose file instead and point *Speech
server* at `http://127.0.0.1:8000/v1`.

### MCP / OpenClaw
*Manage server → AI agents → Connect an agent* gives you a token (read / write / admin scopes). OpenClaw:
```json5
{ mcp: { servers: { paddock: {
    url: "https://192.168.1.50:8443/mcp", transport: "streamable-http",
    headers: { Authorization: "Bearer pdk_ai_…" } } } } }
```
Start OpenClaw with `NODE_EXTRA_CA_CERTS=/path/to/paddock-ca.crt` so it trusts Paddock's private CA, or use
the dependency-free stdio bridge [`server/scripts/paddock_mcp_stdio.py`](server/scripts/paddock_mcp_stdio.py).
Agents can search, inspect, tag, organise, build projects and plans, rescan and move things to trash —
never permanently delete. Everything they change is in the audit log.

### Linked servers & encrypted backup
*Manage server → Linked servers* → **Invite a server** (choose backup space / shared libraries) → on the
other machine **Join with an invite**; both admins confirm the same 6-word safety phrase. Then pick what to
back up, where, when and how many snapshots to keep. Save the **recovery key** somewhere offline — with it,
a brand-new Paddock can restore from any linked server. Run [`deploy/compose.node.yaml`](deploy/compose.node.yaml)
(or [`deploy/install-node.ps1`](deploy/install-node.ps1) on Windows) for the sister **Paddock Node**.

## Develop
```bash
cd server
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt pytest   # bin/ on Linux/macOS
.venv/Scripts/python scripts/dev.py      # http://localhost:8600 with generated demo media
.venv/Scripts/python -m pytest           # incl. two real HTTPS servers linking & backing up
python ../design/gen_tokens.py           # after editing design/tokens.json (web + Android themes)
cd ../android && ./gradlew assembleDebug
```

Paddock OS and FFmpeg build on Debian 13 (or in a `debian:trixie` container), as root:
```bash
bash ffmpeg/build.sh                             # → ffmpeg/out/paddock-ffmpeg-*.tar.zst
bash os/build.sh --variant standard              # or nvidia → os/out/paddock-os-*.iso
bash os/test/smoke.sh os/out/paddock-os-*.iso    # boots it in QEMU: live, install, BIOS, UEFI, RAID
```
Pushing a `v*` tag runs [the release workflow](.github/workflows/release.yml), which builds, tests and
publishes all of the above.

## Layout
```
design/            tokens.json → web CSS variables + Android Compose theme (one look everywhere)
server/paddock/    FastAPI app: scanner, probe (ffprobe/EXIF/tags), red, engineering, stlrender, thumbs,
                   transcode (ladder/HLS/GPU/cache), transcribe, ai, tools (agent tool surface), mcp,
                   federation, vault, backup, portmap, wireguard, tls, security, api/*, web/ (SPA)
server/tests/      end-to-end tests with generated media (ffmpeg/Pillow)
android/           native Kotlin/Compose app (ExoPlayer, WireGuard, WorkManager backup)
deploy/            Dockerfile, TrueNAS compose, WireGuard sidecar, Paddock Node (Docker/Windows)
ffmpeg/            Paddock's own FFmpeg build (official sources, only what Paddock uses)
os/                Paddock OS: package list, system files, installer, root agent, ISO build, VM tests
docs/SECURITY.md   threat model and the measures behind it
```

## License
Paddock is free software under the [GNU Affero General Public License v3.0](LICENSE). If you run a modified
version for other people, you must offer them its source. Bundled third-party components keep their own
licenses (hls.js: Apache-2.0; FFmpeg: GPL-3.0; whisper.cpp: MIT); Paddock OS images contain Debian packages
under their respective licenses, listed in `/usr/share/doc/*/copyright` on the installed system.

## Status and known limits
- The server, web app and federation are covered by automated tests; GPU encoders, WireGuard, router port
  mapping, REDline and the Android app need verification on your actual hardware (the dev box has no
  NVIDIA/Intel GPU passthrough, no WireGuard kernel, no Android device).
- R3D decoding needs RED's free REDline inside the container for metadata/proxies; without it clips are
  cataloged from names, sidecars and proxies.
- No online metadata scraping (by design); local `poster.jpg`/NFO-style artwork is used.
- Windows Paddock Node links over the LAN; for an offsite node use Docker (Linux/NAS/Raspberry Pi).
- Per-user library permissions (e.g. hiding footage from kids' accounts) and iOS are not built yet.
- Paddock OS: x86-64 only, wired networking only (no Wi-Fi drivers, to keep it lean), Secure Boot must be
  off, and updates are done by re-running the installer's *Upgrade* (keeps your data). Built-in
  transcription runs on the CPU; use a speech server for GPU speed. Container images are amd64 for now.
