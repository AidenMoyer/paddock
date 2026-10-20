#!/opt/paddock/venv/bin/python
"""Paddock OS console — the admin panel on the server's own screen and keyboard (tty1).

Everything here works with no network and no browser:
  * a live dashboard: addresses, Paddock status, health, network, pools and drive health, alerts
  * first-time setup (creates the admin account), then an admin menu behind a Paddock admin sign-in:
    status · drives & RAID pools · libraries · network · name & time zone · Paddock service & logs ·
    accounts (incl. resetting a forgotten password) · restart / shut down · root shell · lock

System changes go through the same checked operations as Manage server → System (paddock-agent);
Paddock's own data (accounts, libraries) goes through Paddock's API on 127.0.0.1.
"""

from __future__ import annotations

import curses
import http.cookiejar
import importlib.machinery
import importlib.util
import json
import os
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

TITLE = "Paddock OS"
ENV_FILE = os.environ.get("PADDOCK_CONSOLE_ENV", "/etc/paddock/paddock.env")
AGENT = os.environ.get("PADDOCK_CONSOLE_AGENT", "/usr/local/sbin/paddock-agent")
PY = "/opt/paddock/venv/bin/python"
PADDOCK_UID = 568
UNLOCK_SECONDS = 600
LEVEL_NAMES = {"raid1": "Mirror (RAID 1)", "raid5": "Parity (RAID 5)", "raid6": "Double parity (RAID 6)",
               "raid10": "Mirrored stripes (RAID 10)", "raid0": "Striped (RAID 0)"}
LEVEL_MIN = {"raid0": 2, "raid1": 2, "raid5": 3, "raid6": 4, "raid10": 4}


def read_env() -> dict:
    env = {}
    try:
        for line in open(ENV_FILE, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k] = v
    except OSError:
        pass
    return env


ENV = read_env()
PORT = ENV.get("PADDOCK_PORT", "443")
DATA = ENV.get("PADDOCK_DATA_DIR", "/srv/data/paddock")


def load_agent():
    loader = importlib.machinery.SourceFileLoader("paddock_agent", AGENT)
    spec = importlib.util.spec_from_loader("paddock_agent", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


A = load_agent()


# ---- Paddock API over loopback ----------------------------------------------------------------

class ApiError(Exception):
    pass


class Api:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self._opener = None

    def opener(self):
        if self._opener is None:
            ca = os.path.join(DATA, "tls", "ca.crt")
            ctx = ssl.create_default_context(cafile=ca) if os.path.exists(ca) else ssl._create_unverified_context()
            ctx.check_hostname = False   # 127.0.0.1; the chain is still checked against Paddock's own CA
            self._opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                                       urllib.request.HTTPCookieProcessor(self.jar))
        return self._opener

    def call(self, method: str, path: str, body=None, timeout: float = 30):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"https://127.0.0.1:{PORT}{path}", data=data, method=method,
                                     headers={"content-type": "application/json", "x-paddock": "1"})
        try:
            with self.opener().open(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("detail")
            except Exception:  # noqa: BLE001
                detail = None
            raise ApiError(detail if isinstance(detail, str) else f"Paddock answered {e.code}") from e
        except (urllib.error.URLError, OSError, ssl.SSLError) as e:
            self._opener = None   # e.g. the CA was created after we started
            raise ApiError("Paddock isn't answering") from e

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body if body is not None else {}, **kw)


api = Api()


# ---- whiptail dialogs ---------------------------------------------------------------------------

def backtitle() -> str:
    return f"Paddock OS console · {os.uname().nodename}"


def wt(*args) -> tuple[bool, str]:
    r = subprocess.run(["whiptail", "--backtitle", backtitle(), "--title", TITLE, *args], stderr=subprocess.PIPE, text=True)
    return r.returncode == 0, r.stderr.strip()


def msg(text: str, h: int = 14) -> None:
    wt("--msgbox", text, str(h), "78")


def info(text: str) -> None:
    subprocess.run(["whiptail", "--backtitle", backtitle(), "--title", TITLE, "--infobox", text, "8", "78"])


def yesno(text: str, yes: str = "Yes", no: str = "No", h: int = 14) -> bool:
    return wt("--yes-button", yes, "--no-button", no, "--yesno", text, str(h), "78")[0]


def ask(text: str, init: str = "", password: bool = False, h: int = 11) -> str | None:
    ok, v = wt("--passwordbox", text, str(h), "78") if password else wt("--inputbox", text, str(h), "78", init)
    return v if ok else None


def menu(text: str, items: list[tuple[str, str]], h: int = 22) -> str | None:
    if not items:
        return None
    flat = [x for pair in items for x in pair]
    ok, v = wt("--notags", "--menu", text, str(h), "78", str(min(len(items), h - 8)), *flat)
    return v if ok else None


def checklist(text: str, items: list[tuple[str, str]], h: int = 20) -> list[str] | None:
    flat = [x for k, label in items for x in (k, label, "OFF")]
    ok, v = wt("--separate-output", "--checklist", text, str(h), "78", str(min(len(items), h - 8)), *flat)
    return [x for x in v.splitlines() if x] if ok else None


def textbox(content: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(content)
        path = f.name
    try:
        rows, cols = shutil_size()
        wt("--scrolltext", "--textbox", path, str(max(rows - 4, 15)), str(max(min(cols - 4, 140), 78)))
    finally:
        os.unlink(path)


def shutil_size() -> tuple[int, int]:
    try:
        sz = os.get_terminal_size(0)
        return sz.lines, sz.columns
    except OSError:
        return 25, 80


def new_password(label: str) -> str | None:
    while True:
        pw = ask(f"{label}\n\nAt least 10 characters.", password=True)
        if pw is None:
            return None
        again = ask("Type it again:", password=True)
        if again is None:
            return None
        if pw == again:
            return pw
        msg("The two passwords didn't match.", 8)


def gb(n) -> str:
    if not n:
        return "0 GB"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB", "MB") else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def uptime(s: float) -> str:
    d, h, m = int(s // 86400), int(s % 86400 // 3600), int(s % 3600 // 60)
    return f"{d} d {h} h" if d else f"{h} h {m} min" if h else f"{m} min"


def agent(fn, args: dict | None = None):
    """Run an agent operation; show its refusal (wrong input, protected disk…) instead of crashing."""
    try:
        return fn(args or {})
    except A.Refused as e:
        msg(str(e), 10)
    except Exception as e:  # noqa: BLE001
        msg(f"That didn't work: {type(e).__name__}: {e}", 12)
    return None


# ---- the dashboard's data ------------------------------------------------------------------------

class Snapshot:
    def __init__(self):
        self.lock = threading.Lock()
        self.data: dict = {}
        self.disks: list = []
        self.disks_at = 0.0

    def refresh(self) -> None:
        d: dict = {}
        for key, fn in (("info", A.op_info), ("net", A.op_network)):
            try:
                d[key] = fn({})
            except Exception as e:  # noqa: BLE001
                d[key + "_error"] = str(e)
        try:
            d["pools"] = A.pools_status()
        except Exception:  # noqa: BLE001
            d["pools"] = []
        try:
            d["auth"] = api.get("/api/auth/state", timeout=4)
        except ApiError as e:
            d["auth_error"] = str(e)
        if time.time() - self.disks_at > 60:   # SMART is slow; drives are refreshed once a minute
            try:
                disks = A.op_disks({})["disks"]
                with self.lock:
                    self.disks, self.disks_at = disks, time.time()
            except Exception:  # noqa: BLE001
                self.disks_at = time.time()
        with self.lock:
            self.data = d

    def get(self) -> tuple[dict, list]:
        with self.lock:
            return dict(self.data), list(self.disks)


snap = Snapshot()


def collector() -> None:
    while True:
        try:
            snap.refresh()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(4)


def addresses(net: dict) -> list[str]:
    return [a.split("/")[0] for i in net.get("interfaces", []) for a in i.get("addresses", [])]


def draw(scr, unlocked: bool) -> None:
    d, disks = snap.get()
    scr.erase()
    rows, cols = scr.getmaxyx()
    info_ = d.get("info", {})
    net = d.get("net", {})
    auth = d.get("auth") or {}
    os_rel = info_.get("paddock_os", {})
    host = info_.get("hostname") or os.uname().nodename
    y = 0

    def put(text: str, attr: int = 0, x: int = 2) -> None:
        nonlocal y
        if y < rows - 1:
            try:
                scr.addnstr(y, x, text, max(cols - x - 1, 0), attr)
            except curses.error:
                pass
        y += 1

    edition = " (NVIDIA)" if os_rel.get("PADDOCK_OS_VARIANT") == "nvidia" else ""
    header = f" Paddock OS {os_rel.get('PADDOCK_OS_VERSION', '')}{edition}  ·  {host}"
    clock = time.strftime("%H:%M ")
    scr.addnstr(0, 0, header.ljust(cols), cols - 1, curses.A_REVERSE | curses.A_BOLD)
    scr.addnstr(0, max(cols - len(clock) - 1, 0), clock, len(clock), curses.A_REVERSE | curses.A_BOLD)
    y = 2
    ips = addresses(net)
    if ips:
        put("Open Paddock in a browser on your network:")
        put("   " + "     ".join([f"https://{ip}" for ip in ips] + [f"https://{host}.local"]), curses.A_BOLD)
    else:
        put("No network yet: plug in a cable, or set an address in the admin menu (Enter).", C_WARN)
    y += 1

    alerts = []
    if info_.get("live"):
        alerts.append(("Running from the USB stick: nothing is saved. Restart and choose Install to install.", C_WARN))
    if auth.get("setup_required"):
        alerts.append(("First-time setup: press S to create the admin account here (or use the browser).", C_WARN))
    for p in d.get("pools", []):
        if p.get("degraded"):
            alerts.append((f"Pool {p['name']} is DEGRADED: a drive failed or is missing. Admin menu > Drives & pools.", C_BAD))
    for disk in disks:
        s = disk.get("smart") or {}
        if s.get("passed") is False:
            alerts.append((f"Drive {disk['path']} ({disk.get('model') or 'drive'}) is failing: replace it soon.", C_BAD))
    for text, attr in alerts:
        put("! " + text, attr | curses.A_BOLD)
    if alerts:
        y += 1

    svc = (info_.get("services") or {}).get("paddock", "unknown")
    if svc == "active" and not d.get("auth_error"):
        server = auth.get("server") or {}
        state = "first-time setup needed" if auth.get("setup_required") else "ready"
        put(f"Paddock     * running  ·  {server.get('name', 'Paddock')} {server.get('version', '')}  ·  {state}", C_OK)
    elif svc == "active":
        put("Paddock     * starting...", C_WARN)
    else:
        put(f"Paddock     * {svc}  (admin menu > Paddock service shows why)", C_BAD)

    if net:
        mode = "fixed address" if net.get("mode") == "static" else "DHCP"
        for i in net.get("interfaces", []):
            addr = ", ".join(i.get("addresses", [])) or "no address"
            cable = "" if i.get("up") else "  ·  cable unplugged"
            put(f"Network     {i['name']}  {addr}  ·  {mode}  ·  router {net.get('default_gateway') or '-'}{cable}")
    if info_:
        mem = info_.get("memory") or {}
        used = (mem.get("total") or 0) - (mem.get("available") or 0)
        temps = [t["celsius"] for t in info_.get("temperatures", [])]
        hot = f"  ·  {max(temps):.0f}°C" if temps else ""
        load = info_.get("load") or [0, 0, 0]
        put(f"Health      up {uptime(info_.get('uptime', 0))}  ·  load {load[0]:.2f}  ·  memory {gb(used)} of {gb(mem.get('total'))}{hot}")
        put(f"Clock       {info_.get('timezone', 'UTC')}  ·  {'synchronised' if info_.get('time_synced') else 'not synchronised yet'}")
    pools = d.get("pools", [])
    for n, p in enumerate(pools):
        st = {"healthy": "healthy", "degraded": "DEGRADED", "rebuilding": f"{p.get('action')} {p.get('progress') or 0:.0f}%"}.get(p["state"], p["state"])
        size = f"{gb(p.get('used'))} of {gb(p.get('size'))}" if p.get("size") else "not mounted"
        attr = C_BAD if p.get("degraded") else C_WARN if p.get("action") else 0
        put(f"{'Pools' if n == 0 else '':<12}{p['name']}  {LEVEL_NAMES.get(p['level'], p['level'])}  {st}  ·  {size}", attr)
    for n, disk in enumerate(disks):
        s = disk.get("smart") or {}
        health = "FAILING" if s.get("passed") is False else "good" if s.get("passed") else "-"
        temp = f"  {s['temperature']}°C" if s.get("temperature") else ""
        role = {"system": "Paddock OS", "pool": "pool " + ",".join(disk.get("pools", [])), "media": "media",
                "free": "empty", "unmounted": "not mounted"}.get(disk["role"], disk["role"])
        put(f"{'Drives' if n == 0 else '':<12}{disk['path']:<10} {gb(disk['size']):>9}  {(disk.get('model') or '')[:24]:<24} {role:<14} {health}{temp}",
            C_BAD if s.get("passed") is False else 0)

    foot = "Enter  admin menu" + ("    S  first-time setup" if auth.get("setup_required") else "") + \
           ("    L  lock" if unlocked else "") + "    Everything here also works in the browser."
    try:
        scr.addnstr(rows - 1, 0, (" " + foot).ljust(cols), cols - 1, curses.A_REVERSE)
    except curses.error:
        pass
    scr.refresh()


# ---- admin session ---------------------------------------------------------------------------------

class Session:
    until = 0.0

    def unlocked(self) -> bool:
        return time.time() < self.until

    def touch(self) -> None:
        self.until = time.time() + UNLOCK_SECONDS

    def lock(self) -> None:
        self.until = 0
        try:
            api.post("/api/auth/logout")
        except ApiError:
            pass
        api.jar.clear()


session = Session()


def sign_in() -> bool:
    if session.unlocked():
        session.touch()
        return True
    try:
        state = api.get("/api/auth/state", timeout=5)
    except ApiError:
        if yesno("Paddock isn't answering, so your account can't be checked.\n\n"
                 "Because you're at the server itself, you can still manage it (network, drives, restart...). Continue?",
                 yes="Continue", no="Back"):
            session.touch()
            return True
        return False
    if state.get("setup_required"):
        return first_time_setup()
    while True:
        user = ask("Sign in with a Paddock admin account.\n\nUsername:")
        if user is None:
            return False
        pw = ask(f"Password for {user}:", password=True)
        if pw is None:
            continue
        try:
            api.post("/api/auth/login", {"username": user.strip(), "password": pw})
            me = (api.get("/api/auth/state") or {}).get("user") or {}
        except ApiError as e:
            if yesno(f"{e}\n\nForgot the password? You can set a new one here, at the server.", yes="Try again", no="Reset it"):
                continue
            reset_password()
            continue
        if not me.get("is_admin"):
            msg("That account isn't an admin. Ask an admin, or reset an admin's password here.", 9)
            session.lock()
            continue
        session.touch()
        return True


def first_time_setup() -> bool:
    msg("Welcome! Let's create the first admin account.\n\n"
        "This account manages everything, here at the console and in the browser. "
        "You can add family members' accounts later.", 12)
    name = ask("A name for this server (shown in the apps):", "Paddock")
    if name is None:
        return False
    user = ask("Admin username:", "admin")
    if not user:
        return False
    pw = new_password(f"Password for {user}:")
    if pw is None:
        return False
    try:
        api.post("/api/auth/setup", {"username": user.strip(), "password": pw, "server_name": name.strip() or "Paddock"})
    except ApiError as e:
        msg(f"Setup didn't work: {e}", 9)
        return False
    session.touch()
    snap.refresh()
    msg(f"Done: {user} is the admin.\n\nNext steps (here or in the browser):\n"
        "  - Drives & pools: combine drives, or format a new one\n"
        "  - Libraries: point Paddock at your movies, photos and footage\n"
        "  - Phones: in the browser, Your account > Connect a phone", 14)
    return True


# ---- admin menu sections ---------------------------------------------------------------------------

def status_screen() -> None:
    d, disks = snap.get()
    i = d.get("info") or {}
    lines = [f"Paddock OS {i.get('paddock_os', {}).get('PADDOCK_OS_VERSION', '?')} "
             f"({i.get('paddock_os', {}).get('PADDOCK_OS_VARIANT', '?')}) on {i.get('debian', '?')}",
             f"Kernel {i.get('kernel')}  ·  started with {i.get('boot')}  ·  up {uptime(i.get('uptime', 0))}",
             f"Processor: {i.get('cpu')} ({i.get('cores')} threads)  ·  load {' '.join(f'{x:.2f}' for x in i.get('load') or [])}",
             f"Memory: {gb((i.get('memory') or {}).get('total', 0) - (i.get('memory') or {}).get('available', 0))} used of "
             f"{gb((i.get('memory') or {}).get('total'))}", "", "Services:"]
    lines += [f"  {k:<20} {v}" for k, v in (i.get("services") or {}).items()]
    temps = [f"  {t['sensor']:<14} {t['label']:<16} {t['celsius']:.0f} C" for t in i.get("temperatures", [])]
    lines += ["", "Temperatures:"] + (temps or ["  (no sensors)"])
    lines += ["", "Pools:"] + ([f"  {p['name']:<12} {LEVEL_NAMES.get(p['level'], p['level']):<26} {p['state']:<10} "
                                f"{gb(p.get('used'))} / {gb(p.get('size'))}  members: {', '.join(m['device'] for m in p['members'])}"
                                for p in d.get("pools", [])] or ["  (none)"])
    lines += ["", "Drives:"]
    for disk in disks:
        s = disk.get("smart") or {}
        lines.append(f"  {disk['path']:<12} {gb(disk['size']):>9}  {disk.get('model') or '':<28} {disk['role']:<10} "
                     f"SMART {'FAILING' if s.get('passed') is False else 'ok' if s.get('passed') else '-'}  "
                     f"{s.get('temperature') or '-'} C  {s.get('power_on_hours') or '-'} h  reallocated {s.get('reallocated') or 0}")
    try:
        ov = api.get("/api/admin/overview", timeout=10)
        st = ov.get("stats", {})
        lines += ["", "Paddock:", f"  {st.get('items', 0)} items, {gb(st.get('bytes', 0))}  ·  {ov.get('users')} users  ·  "
                  f"{ov.get('devices')} devices  ·  transcoding: {ov.get('pipeline') or ov.get('hwaccel_active')}"]
    except ApiError:
        lines += ["", "Paddock: (sign in to see library details)"]
    textbox("\n".join(lines) + "\n")


def free_drives(disks: list) -> list:
    return [x for x in disks if x["role"] in ("free", "unmounted", "media")]


def drive_label(x: dict) -> str:
    kind = "USB" if x.get("transport") == "usb" else "NVMe" if x.get("transport") == "nvme" else "HDD" if x.get("rotational") else "SSD"
    return f"{x['path']:<10} {gb(x['size']):>9}  {kind:<4} {(x.get('model') or 'drive')[:30]}  ({x['role']})"


def storage_menu() -> None:
    while True:
        info("Reading drives (health checks can take a few seconds)...")
        data = agent(A.op_disks)
        if not data:
            return
        disks, pools = data["disks"], data["pools"]
        items = [("create", "Create a pool from several drives (RAID)")]
        items += [(f"pool:{p['name']}", f"Pool {p['name']}: {LEVEL_NAMES.get(p['level'], p['level'])}, {p['state']}") for p in pools]
        items += [("format", "Erase & format a single drive for media"), ("mount", "Mount or unmount a drive"),
                  ("list", "Show all drives and their health")]
        c = menu("Drives & pools. Pools and drives appear under /srv/media for your libraries.", items)
        if c is None:
            return
        session.touch()
        if c == "create":
            create_pool(disks)
        elif c.startswith("pool:"):
            pool_actions(next(p for p in pools if p["name"] == c[5:]), disks)
        elif c == "format":
            format_drive(disks)
        elif c == "mount":
            mount_menu(disks)
        elif c == "list":
            snap.disks_at = 0
            status_screen()


def create_pool(disks: list) -> None:
    free = free_drives(disks)
    if len(free) < 2:
        msg("A pool needs at least two drives that aren't in use. Plug in more drives, or remove one from a pool first.", 9)
        return
    picked = checklist("Select the drives to combine (Space to select). EVERYTHING on them will be erased.",
                       [(x["path"], drive_label(x)) for x in free])
    if not picked:
        return
    n = len(picked)
    smallest = min(x["size"] for x in free if x["path"] in picked)
    caps = {"raid0": n, "raid1": 1, "raid5": n - 1, "raid6": n - 2, "raid10": n // 2}
    levels = [(k, f"{LEVEL_NAMES[k]}: about {gb(caps[k] * smallest)} usable") for k in ("raid1", "raid5", "raid6", "raid10", "raid0")
              if n >= LEVEL_MIN[k]]
    level = menu(f"{n} drives selected. How should they work together?\n"
                 "Mirror and parity keep your files when a drive fails; striped does not.", levels)
    if not level:
        return
    name = ask("Pool name (lowercase letters, numbers, dashes). It appears as /srv/media/<name>:", "media")
    if not name:
        return
    name = name.strip().lower()
    typed = ask(f"This ERASES {', '.join(picked)}.\n\nType the pool name ({name}) to confirm:")
    if typed != name:
        return
    info(f"Creating {name}...")
    r = agent(A.op_pool_create, {"name": name, "level": level, "disks": picked, "confirm": typed})
    if r:
        msg(f"Pool {name} is ready at /srv/media/{name}.\n\n{r.get('note', '')}\n\nAdd it as a library: Libraries > Add.", 12)


def pool_actions(p: dict, disks: list) -> None:
    free = free_drives(disks)
    items = []
    if free:
        items.append(("add", "Replace a failed drive" if p["degraded"] else "Add a drive (spare, or grow the pool)"))
    if p["level"] != "raid0" and not p.get("action"):
        items.append(("check", "Check integrity now"))
    if not p.get("action"):
        items.append(("expand", "Use new space (after growing)"))
    items += [("remove", "Remove a drive from the pool"), ("delete", "Delete the pool (erases it)")]
    detail = (f"{LEVEL_NAMES.get(p['level'], p['level'])}, {p['devices']} drives, {p['state']}"
              + (f", {p['action']} {p.get('progress') or 0:.0f}%" if p.get("action") else "")
              + f"\nMembers: {', '.join(m['device'] + ' (' + m['role'] + ')' for m in p['members'])}")
    c = menu(f"Pool {p['name']}\n{detail}", items)
    if c == "add":
        disk = menu("Which drive? It will be erased.", [(x["path"], drive_label(x)) for x in free])
        if not disk:
            return
        grow = False
        if not p["degraded"] and p["level"] in ("raid1", "raid5", "raid6"):
            grow = yesno("Grow the pool with this drive (more space)?\n\nNo = keep it as a hot spare that takes over "
                         "automatically when another drive fails.", yes="Grow", no="Spare")
        typed = ask(f"Type {disk.split('/')[-1]} to erase it and add it to {p['name']}:")
        if typed:
            r = agent(A.op_pool_add, {"name": p["name"], "disk": disk, "grow": grow, "confirm": typed})
            if r:
                msg(r.get("note") or ("Added as a hot spare." if r.get("spare") else "Added. The pool is rebuilding."), 9)
    elif c == "check":
        if agent(A.op_pool_check, {"name": p["name"]}):
            msg("Integrity check started. Progress shows on the dashboard.", 8)
    elif c == "expand":
        if agent(A.op_pool_expand, {"name": p["name"]}):
            msg("The pool now uses all its space.", 8)
    elif c == "remove":
        dev = menu("Remove which drive?", [(m["device"], f"{m['device']} ({m['role']})") for m in p["members"]])
        if dev:
            typed = ask(f"The pool keeps working without {dev} if it has redundancy.\n\nType {dev.split('/')[-1]} to confirm:")
            if typed and agent(A.op_pool_remove_disk, {"name": p["name"], "device": dev, "confirm": typed}):
                msg("Removed.", 7)
    elif c == "delete":
        typed = ask(f"Every file on {p['name']} will be erased.\n\nType: delete {p['name']}")
        if typed and agent(A.op_pool_destroy, {"name": p["name"], "confirm": typed}):
            msg("Pool deleted.", 7)


def format_drive(disks: list) -> None:
    free = free_drives(disks)
    disk = menu("Erase which drive?", [(x["path"], drive_label(x)) for x in free])
    if not disk:
        return
    label = ask("A name for the drive (shows up as /srv/media/<name>):", "Media")
    if not label:
        return
    typed = ask(f"EVERYTHING on {disk} will be erased.\n\nType {disk.split('/')[-1]} to confirm:")
    if typed:
        info("Formatting...")
        if agent(A.op_format, {"disk": disk, "label": label, "confirm": typed}):
            msg(f"{disk} is ready as /srv/media/{label}.", 8)


def mount_menu(disks: list) -> None:
    items = []
    for x in disks:
        if x["role"] in ("system", "pool"):
            continue
        for p in x["partitions"]:
            media = [m for m in p["mountpoints"] if m.startswith("/srv/media/")]
            if media:
                items.append((f"u:{p['path']}", f"Unmount {p['path']}  {media[0]}  (safe to unplug afterwards)"))
            elif p.get("fstype"):
                items.append((f"m:{p['path']}", f"Mount {p['path']}  {p.get('label') or ''} ({p['fstype']})"))
    c = menu("Drives are mounted automatically when plugged in.", items) if items else msg("No drives to mount or unmount.", 7)
    if c:
        fn = A.op_unmount if c.startswith("u:") else A.op_mount
        if agent(fn, {"device": c[2:]}):
            msg("Done.", 7)


def libraries_menu() -> None:
    try:
        libs = api.get("/api/libraries")
        kinds = api.get("/api/library-kinds")
    except ApiError as e:
        msg(f"{e}. Libraries are managed by Paddock, so it needs to be running.", 9)
        return
    items = [("add", "Add a library")] + [(f"scan:{l['id']}", f"Rescan  {l['name']}  ({l.get('kind_label', l['kind'])}, {l.get('item_count', 0)} items)  {l['path']}") for l in libs]
    c = menu("Libraries", items)
    if c == "add":
        try:
            browse = api.get("/api/fs/browse?path=/srv/media")
            folders = [(d["path"], d["path"]) for d in browse.get("dirs", [])]
        except ApiError:
            folders = []
        folders.append(("other", "Another folder..."))
        path = menu("Which folder? Drives and pools are listed under /srv/media.", folders)
        if path == "other":
            path = ask("Full path of the folder:", "/srv/media/")
        if not path:
            return
        kind = menu("What's in it?", [(k["kind"], k["label"]) for k in kinds])
        if not kind:
            return
        name = ask("Library name:", path.rstrip("/").split("/")[-1].title())
        if not name:
            return
        try:
            api.post("/api/libraries", {"name": name, "kind": kind, "path": path})
            msg(f"{name} added. Paddock is scanning it now.", 8)
        except ApiError as e:
            msg(str(e), 8)
    elif c and c.startswith("scan:"):
        try:
            api.post(f"/api/libraries/{c[5:]}/scan")
            msg("Rescan started.", 7)
        except ApiError as e:
            msg(str(e), 8)


def network_menu() -> None:
    n = agent(A.op_network)
    if not n:
        return
    current = "\n".join(f"  {i['name']}: {', '.join(i['addresses']) or 'no address'}{'' if i['up'] else ' (cable unplugged)'}"
                        for i in n["interfaces"])
    mode = menu(f"Network\n{current}\n  Router {n.get('default_gateway') or '-'}  ·  DNS {', '.join(n.get('dns') or []) or '-'}",
                [("dhcp", "Automatic address from the router (DHCP)"), ("static", "Fixed address")])
    if not mode:
        return
    args = {"mode": mode}
    if mode == "static":
        first = next((a for i in n["interfaces"] for a in i["addresses"]), "192.168.1.50/24")
        args["address"] = ask("Address and prefix, e.g. 192.168.1.50/24:", n.get("address") or first)
        if not args["address"]:
            return
        args["gateway"] = ask("Router (gateway) address:", n.get("gateway") or n.get("default_gateway") or "")
        if not args["gateway"]:
            return
        dns = ask("DNS servers (up to 3, separated by spaces):", " ".join(n.get("dns_configured") or [args["gateway"]]))
        args["dns"] = (dns or "").split()
    if not agent(A.op_network_set, args):
        return
    info("Applying the new network settings...")
    time.sleep(8)
    n2 = A.op_network({})
    now = "\n".join(f"  {i['name']}: {', '.join(i['addresses']) or 'no address'}" for i in n2["interfaces"])
    if yesno(f"The network now looks like this:\n{now}\n\nKeep these settings?", yes="Keep", no="Undo"):
        A.op_network_confirm({})
    else:
        A.revert_network()
        msg("The previous settings are back.", 7)


def name_time_menu() -> None:
    c = menu("Name & time zone", [("name", f"Server name: {os.uname().nodename}"), ("tz", "Time zone")])
    if c == "name":
        name = ask("New name. The server will answer to <name>.local:", os.uname().nodename)
        if name:
            r = agent(A.op_hostname, {"hostname": name.strip().lower()})
            if r:
                msg(f"Renamed. Paddock restarts and answers at {r['url']}.", 8)
    elif c == "tz":
        import zoneinfo

        zones = sorted(z for z in zoneinfo.available_timezones() if "/" in z and not z.startswith(("Etc/", "SystemV/", "posix/", "right/")))
        regions = sorted({z.split("/")[0] for z in zones})
        region = menu("Region", [(r, r) for r in regions])
        if not region:
            return
        tz = menu("Time zone", [(z, z.split("/", 1)[1].replace("_", " ")) for z in zones if z.startswith(region + "/")])
        if tz and agent(A.op_timezone, {"timezone": tz}):
            msg(f"Time zone set to {tz}. Paddock restarts.", 8)


def paddock_menu() -> None:
    c = menu("Paddock service", [("status", "Status"), ("restart", "Restart Paddock"), ("logs:paddock", "Paddock's log"),
                                 ("logs:paddock-wg", "Tunnel (WireGuard) log"), ("logs:systemd-networkd", "Network log"),
                                 ("logs:kernel", "Kernel log (drives, hardware)")])
    if c == "status":
        r = subprocess.run(["systemctl", "status", "--no-pager", "-l", "paddock", "paddock-wg"], capture_output=True, text=True)
        textbox(r.stdout or r.stderr)
    elif c == "restart":
        subprocess.run(["systemctl", "restart", "paddock"])
        msg("Paddock restarted.", 7)
    elif c and c.startswith("logs:"):
        r = agent(A.op_logs, {"unit": c[5:], "lines": 500})
        if r:
            textbox(r["text"] or "(empty)")


def as_paddock(op: dict) -> dict:
    """Talk to Paddock's database as the paddock user (so file ownership never changes)."""
    code = (
        "import json,sys\n"
        "from paddock import db\n"
        "from paddock.security import hash_password, password_problems\n"
        "d=json.load(sys.stdin)\n"
        "if d['op']=='list':\n"
        "    print(json.dumps([{'id':r['id'],'username':r['username'],'is_admin':bool(r['is_admin'])} for r in db.q('SELECT id,username,is_admin FROM users ORDER BY username')]))\n"
        "else:\n"
        "    p=password_problems(d['password'])\n"
        "    if p: print(json.dumps({'error':p})); sys.exit(0)\n"
        "    db.x('UPDATE users SET pw_hash=? WHERE id=?',(hash_password(d['password']),d['id']))\n"
        "    db.x('DELETE FROM sessions WHERE user_id=?',(d['id'],))\n"
        "    db.audit('console','user.password_reset',user_id=d['id'])\n"
        "    print(json.dumps({'ok':True}))\n"
    )
    env = dict(ENV, PATH="/usr/bin:/bin", PYTHONPATH="/opt/paddock/app", HOME="/tmp")
    r = subprocess.run([PY, "-c", code], input=json.dumps(op), capture_output=True, text=True, env=env,
                       cwd="/opt/paddock/app", user=PADDOCK_UID, group=PADDOCK_UID, extra_groups=[], timeout=60)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "failed").strip().splitlines()[-1])
    return json.loads(r.stdout)


def reset_password() -> None:
    try:
        users = as_paddock({"op": "list"})
    except Exception as e:  # noqa: BLE001
        msg(f"Couldn't read the accounts: {e}", 9)
        return
    if not users:
        msg("There are no accounts yet. Use first-time setup.", 8)
        return
    uid = menu("Reset the password of which account? (You're at the server, so no old password is needed.)",
               [(str(u["id"]), f"{u['username']}{'  (admin)' if u['is_admin'] else ''}") for u in users])
    if not uid:
        return
    user = next(u for u in users if str(u["id"]) == uid)
    pw = new_password(f"New password for {user['username']}:")
    if pw is None:
        return
    r = as_paddock({"op": "reset", "id": int(uid), "password": pw})
    if r.get("error"):
        msg(r["error"], 8)
    else:
        msg(f"Password for {user['username']} changed. Their other sessions were signed out.", 8)


def accounts_menu() -> None:
    c = menu("Accounts", [("list", "Show accounts"), ("reset", "Reset a password")])
    if c == "list":
        try:
            users = api.get("/api/users")
            textbox("\n".join(f"{u['username']:<24} {'admin' if u['is_admin'] else 'member'}" for u in users) + "\n")
        except ApiError:
            textbox("\n".join(f"{u['username']:<24} {'admin' if u['is_admin'] else 'member'}" for u in as_paddock({"op": "list"})) + "\n")
    elif c == "reset":
        reset_password()


def updates_menu() -> None:
    info("Checking GitHub for the latest stable release...")
    u = agent(A.op_update_check)
    if not u:
        return
    s = u["settings"]
    head = (f"Installed: Paddock OS {u.get('current')}\n"
            + (f"Available: {u['latest']}\n" if u.get("available") else "Up to date.\n" if u.get("latest") else "")
            + (f"Last problem: {u['error']}\n" if u.get("error") else "")
            + f"Automatic updates: {'on, nightly at ' + s['time'] if s['auto'] else 'off'}")
    items = ([("install", f"Install {u['latest']} now (restarts the server)")] if u.get("available") and not u.get("running") else [])
    items += [("auto", "Turn automatic updates " + ("off" if s["auto"] else "on")), ("time", f"Change the time ({s['time']})")]
    c = menu(head, items)
    if c == "install" and yesno(f"Download, verify and install Paddock OS {u['latest']}, then restart?\n\n"
                                "Your data, drives, accounts and settings are kept.", yes="Install", no="Cancel", h=11):
        if agent(A.op_update_apply):
            while True:
                time.sleep(3)
                st = A.op_update_status({})
                if st.get("state") == "error":
                    msg(f"The update stopped: {st.get('error')}", 9)
                    return
                info("Downloading... " + f"{st.get('progress') or 0}%" if st.get("state") == "downloading" else
                     "Installing... the server restarts when it's done.")
    elif c == "auto":
        agent(A.op_update_settings, {"auto": not s["auto"], "time": s["time"]})
    elif c == "time":
        t = ask("Install updates at (24-hour time, HH:MM):", s["time"])
        if t:
            agent(A.op_update_settings, {"auto": s["auto"], "time": t.strip()})


def power_menu() -> None:
    c = menu("Power", [("reboot", "Restart the computer"), ("poweroff", "Shut down")])
    if c and yesno("Restart the computer now?" if c == "reboot" else
                   "Shut down now? Press the power button to start it again.", yes="Yes", no="Cancel", h=9):
        A.op_power({"action": c})
        info("Restarting..." if c == "reboot" else "Shutting down...")
        time.sleep(30)


def root_shell() -> None:
    if not yesno("Open a root shell? Changes made by hand aren't checked like the menus are.\n\nType exit to come back.",
                 yes="Open", no="Back", h=10):
        return
    subprocess.run(["clear"])
    subprocess.run(["/bin/bash", "-l"], env=dict(os.environ, PS1="paddock-os# "))


def admin_menu() -> None:
    if not sign_in():
        return
    while True:
        if not session.unlocked():
            msg("The console locked itself after 10 minutes. Sign in again to continue.", 8)
            if not sign_in():
                return
        c = menu("Manage this server", [
            ("status", "Server status & health"), ("storage", "Drives & RAID pools"), ("libraries", "Libraries"),
            ("network", "Network"), ("name", "Name & time zone"), ("paddock", "Paddock service & logs"),
            ("accounts", "Accounts & passwords"), ("updates", "Updates"), ("power", "Restart or shut down"),
            ("shell", "Root shell (advanced)"),
            ("lock", "Lock the console"),
        ])
        if c is None:
            return
        session.touch()
        if c == "lock":
            session.lock()
            return
        {"status": status_screen, "storage": storage_menu, "libraries": libraries_menu, "network": network_menu,
         "name": name_time_menu, "paddock": paddock_menu, "accounts": accounts_menu, "updates": updates_menu, "power": power_menu,
         "shell": root_shell}[c]()
        snap.disks_at = 0


# ---- main loop -----------------------------------------------------------------------------------------

C_OK = C_WARN = C_BAD = 0


def run(scr) -> None:
    global C_OK, C_WARN, C_BAD
    curses.curs_set(0)
    if curses.has_colors():
        curses.start_color()
        curses.init_pair(1, curses.COLOR_GREEN, curses.COLOR_BLACK)
        curses.init_pair(2, curses.COLOR_YELLOW, curses.COLOR_BLACK)
        curses.init_pair(3, curses.COLOR_RED, curses.COLOR_BLACK)
        C_OK, C_WARN, C_BAD = curses.color_pair(1), curses.color_pair(2), curses.color_pair(3)
    scr.timeout(1000)
    while True:
        draw(scr, session.unlocked())
        ch = scr.getch()
        if ch == -1:
            continue
        action = None
        if ch in (10, 13, curses.KEY_ENTER, ord("m"), ord("M")):
            action = admin_menu
        elif ch in (ord("s"), ord("S")) and (snap.get()[0].get("auth") or {}).get("setup_required"):
            action = first_time_setup
        elif ch in (ord("l"), ord("L")):
            session.lock()
        if action:
            curses.def_prog_mode()
            curses.endwin()
            try:
                action()
            except Exception as e:  # noqa: BLE001 - never leave the console dead
                msg(f"Something went wrong: {type(e).__name__}: {e}", 10)
            finally:
                curses.reset_prog_mode()
                scr.clear()
                scr.refresh()


def main() -> None:
    import locale

    os.environ.setdefault("TERM", "linux")
    locale.setlocale(locale.LC_ALL, "")   # UTF-8 drawing (° and ·) on the Linux console
    threading.Thread(target=collector, daemon=True).start()
    snap.refresh()
    curses.wrapper(run)


if __name__ == "__main__":
    main()
