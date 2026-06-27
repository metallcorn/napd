#!/usr/bin/env python3
"""napd — focus-aware power observer for KDE Wayland.

v1 = OBSERVE MODE: watches which app is focused, attributes CPU/power to each
app via cgroup v2 cpu.stat, and reports who is burning battery in the
background. It NEVER throttles anything in observe mode — it only logs what it
*would* do, and flags apps it is unsure about (audio/protected) with a reason.

Design rules:
  * event-driven, no fast polling — focus arrives as a D-Bus push from KWin,
    the GLib loop is blocked on epoll otherwise (≈0% CPU at idle).
  * a slow sampler (default 20s) reads cgroup cpu.stat to attribute power.
    Reading a dozen tiny sysfs files costs microseconds; one wakeup / 20s.
  * self-accounting — napd reports its OWN cpu cost so you can verify it saves
    more than it spends.
  * everything runs in user-space, no root, touching only the cpu cgroup
    controller that systemd already delegated into the user session.
"""

import os
import re
import sys
import json
import glob
import time
import signal
import collections
import subprocess

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

BUS_NAME = "ai.palabra.NapD"
OBJ_PATH = "/ai/palabra/NapD"
IFACE = "ai.palabra.NapD"

HERE = os.path.dirname(os.path.abspath(__file__))
KWIN_SCRIPT = os.path.join(HERE, "napd-focus.js")
KWIN_SCRIPT_NAME = "napd-focus"

CONFIG = {
    "mode": "enforce",          # observe | enforce
    "sample_interval_sec": 10,  # slow sampler cadence (also how fast caps apply)
    "bg_cpu_flag_pct": 2.0,     # background app above this %core = candidate
    "watts_per_core": 3.0,      # rough W per 100% of one core (approx, labelled)
    "throttle_pct": 10,         # enforce: cap background to this % of one core
    "enforce_on_ac": False,     # only throttle on battery; full speed on charger
    "focus_grace_sec": 30,      # don't touch an app for N s after it lost focus
    "protect_apps": [           # substring match on app-id; never throttle
        "konsole", "yakuake", "kwin", "plasmashell", "kded", "krunner",
        "pipewire", "wireplumber", "pulseaudio", "napd", "ksmserver",
        "xdg-desktop-portal", "polkit",
    ],
}


def log(msg):
    print(f"napd: {msg}", flush=True)


def read_first_int(path, key):
    try:
        with open(path) as f:
            for ln in f:
                if ln.startswith(key):
                    return int(ln.split()[1])
    except OSError:
        pass
    return None


def read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def read_total_watts():
    """Real instantaneous system draw from the battery gauge, in watts.
    Returns (watts|None, on_battery). None when on AC (discharge not measurable)."""
    for bat in sorted(glob.glob("/sys/class/power_supply/BAT*")):
        try:
            status = open(bat + "/status").read().strip()
        except OSError:
            continue
        on_batt = status == "Discharging"
        if not on_batt:
            # On AC the gauge measures charge current, not system draw — the
            # system's real consumption is simply not observable here.
            return (None, on_batt)
        pw = read_int(bat + "/power_now")           # microwatts (most laptops)
        if not pw:                                   # None or 0 → fall back to I*V
            cur = read_int(bat + "/current_now")
            volt = read_int(bat + "/voltage_now")
            if cur and volt:
                pw = cur * volt // 1_000_000
        if pw:
            return (round(pw / 1_000_000, 1), on_batt)
        return (None, on_batt)
    return (None, False)


def read_cpu_total():
    """System-wide busy/idle jiffies from /proc/stat (aggregate of all cores)."""
    try:
        with open("/proc/stat") as f:
            v = list(map(int, f.readline().split()[1:]))
    except (OSError, ValueError):
        return None
    idle = v[3] + (v[4] if len(v) > 4 else 0)        # idle + iowait
    return sum(v), idle


_SYSBUS = None


def sysbus():
    """One reused system-bus connection (creating a fresh one each call is slow)."""
    global _SYSBUS
    if _SYSBUS is None:
        _SYSBUS = dbus.SystemBus()
    return _SYSBUS


def read_power_profile():
    """Active power-profiles-daemon profile, read as a D-Bus property
    (~2 ms) instead of spawning `powerprofilesctl` (~150 ms)."""
    for name in ("org.freedesktop.UPower.PowerProfiles", "net.hadess.PowerProfiles"):
        try:
            obj = sysbus().get_object(name, "/org/freedesktop/UPower/PowerProfiles"
                                      if "UPower" in name else "/net/hadess/PowerProfiles")
            p = obj.Get(name, "ActiveProfile",
                        dbus_interface="org.freedesktop.DBus.Properties")
            return str(p)
        except dbus.DBusException:
            continue
    return None


def read_amdgpu_watts():
    """SoC/GPU package power from the amdgpu hwmon, in watts. Readable without
    root and available even on AC — a real 'active silicon' signal."""
    for h in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            if open(os.path.join(h, "name")).read().strip() != "amdgpu":
                continue
        except OSError:
            continue
        for f in ("power1_average", "power1_input"):
            v = read_int(os.path.join(h, f))
            if v:
                return round(v / 1_000_000, 1)
    return None


STATE_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "napd")
CALIB_FILE = os.path.join(STATE_DIR, "calib.json")


class Calibrator:
    """Learns the real power model on battery: watts ≈ base + k·cpu_core%.
    `k` is measured watts per 1% of one core; `base` is the fixed floor
    (screen/GPU/wifi/idle SoC) that throttling apps can NOT reclaim.
    A rolling window adapts to drift (e.g. brightness changes)."""

    MIN_SAMPLES = 6
    MIN_VAR = 25.0   # need real spread in cpu% (std ≳ 5 core-%) to trust a slope

    def __init__(self, path=CALIB_FILE, maxlen=256):
        self.path = path
        self.pts = collections.deque(maxlen=maxlen)  # (cpu_core_pct, watts)
        self.k = self.base = self.r2 = None
        self._load()
        self._fit()

    def add(self, cpu_pct, watts):
        if cpu_pct is None or not watts or watts <= 0:
            return
        self.pts.append((round(cpu_pct, 2), round(watts, 3)))
        self._fit()
        self._save()

    def _fit(self):
        n = len(self.pts)
        if n < self.MIN_SAMPLES:
            self.k = self.base = self.r2 = None
            return
        xs = [p[0] for p in self.pts]
        ys = [p[1] for p in self.pts]
        mx, my = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        if sxx / n < self.MIN_VAR:
            self.k = self.base = self.r2 = None
            return
        k = sxy / sxx
        if k <= 0:                       # nonsensical (more CPU → less power)
            self.k = self.base = self.r2 = None
            return
        base = my - k * mx
        ss_tot = sum((y - my) ** 2 for y in ys)
        ss_res = sum((y - (base + k * x)) ** 2 for x, y in zip(xs, ys))
        self.k = k
        self.base = max(0.0, base)
        self.r2 = (1 - ss_res / ss_tot) if ss_tot > 1e-9 else None

    def _load(self):
        try:
            with open(self.path) as f:
                for pair in json.load(f):
                    self.pts.append((pair[0], pair[1]))
        except (OSError, ValueError, TypeError, IndexError):
            pass

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w") as f:
                json.dump(list(self.pts), f)
        except OSError:
            pass


class CgroupView:
    """Maps the user's app.slice and reads per-app cpu usage."""

    def __init__(self):
        uid = os.getuid()
        self.base = (f"/sys/fs/cgroup/user.slice/user-{uid}.slice/"
                     f"user@{uid}.service/app.slice")
        self.own = self._own_cgroup_dir()

    @staticmethod
    def _own_cgroup_dir():
        try:
            with open("/proc/self/cgroup") as f:
                rel = f.read().strip().split("::", 1)[1]
            return "/sys/fs/cgroup" + rel
        except (OSError, IndexError):
            return None

    @staticmethod
    def appid_from_scope(scope_name):
        # app-flatpak-com.google.Chrome-5146.scope      -> com.google.Chrome
        # app-org.kde.konsole-32271.scope               -> org.kde.konsole
        # app-firefox@976b...ed6b.service               -> firefox
        m = re.match(r"app-(?:flatpak-)?(.+?)(?:@[0-9a-f]+|-\d+)?\.(?:scope|service)$",
                     scope_name)
        raw = m.group(1) if m else scope_name
        # systemd escapes '-' as '\x2d' etc. in unit/cgroup names — decode it.
        return re.sub(r"\\x([0-9a-fA-F]{2})",
                      lambda g: chr(int(g.group(1), 16)), raw)

    def scopes(self):
        # apps land in app.slice as either .scope (KDE/flatpak) or .service
        # (e.g. Firefox via its systemd launcher) — collect both.
        return sorted(glob.glob(os.path.join(self.base, "app-*.scope")) +
                      glob.glob(os.path.join(self.base, "app-*.service")))

    @staticmethod
    def usage_usec(scope_dir):
        return read_first_int(os.path.join(scope_dir, "cpu.stat"), "usage_usec")

    def scope_for_pid(self, pid):
        try:
            with open(f"/proc/{pid}/cgroup") as f:
                rel = f.read().strip().split("::", 1)[1]
        except (OSError, IndexError):
            return None
        m = re.search(r"app-[^/]+\.(?:scope|service)", rel)
        if not m:
            return None
        return os.path.join(self.base, m.group(0))

    # --- enforcement primitives (used only in enforce mode) -------------
    @staticmethod
    def set_cap(scope_dir, pct):
        period = 100000
        quota = max(1000, int(pct / 100.0 * period))
        try:
            with open(os.path.join(scope_dir, "cpu.max"), "w") as f:
                f.write(f"{quota} {period}")
            return True
        except OSError as e:
            log(f"set_cap failed for {scope_dir}: {e}")
            return False

    @staticmethod
    def clear_cap(scope_dir):
        try:
            with open(os.path.join(scope_dir, "cpu.max"), "w") as f:
                f.write("max 100000")
        except OSError:
            pass


def pactl_streams(kind):
    """Parse `pactl list <kind>` (sink-inputs | source-outputs). Forces LC_ALL=C
    so field names stay English on localized systems — this box is ru_RU, where
    the default output reads 'Выход источника' / 'Поток приостановлен' instead of
    'Source Output' / 'Corked', which silently broke every string match.
    Returns per-stream dicts with the three identifiers we can map to a cgroup:
    flatpak portal app_id, host pid, and binary name."""
    header = "Sink Input #" if kind == "sink-inputs" else "Source Output #"
    try:
        out = subprocess.run(["pactl", "list", kind], capture_output=True,
                             text=True, timeout=2,
                             env={**os.environ, "LC_ALL": "C"}).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    recs = []
    for block in out.split(header)[1:]:
        appid = re.search(r'portal\.app_id = "([^"]+)"', block)
        pid = re.search(r'application\.process\.id = "(\d+)"', block)
        binary = re.search(r'application\.process\.binary = "([^"]+)"', block)
        recs.append({
            "corked": re.search(r"Corked:\s*yes", block) is not None,
            "appid": appid.group(1) if appid else None,
            "pid": int(pid.group(1)) if pid else None,
            "binary": binary.group(1) if binary else None,
        })
    return recs


def camera_holder_pids():
    """PIDs holding a camera device (/dev/video*) open. In-process /proc scan —
    fast readlink()s, no `fuser` (which costs ~400 ms scanning all of /proc)."""
    targets = {os.path.realpath(v) for v in glob.glob("/dev/video*")}
    if not targets:
        return set()
    pids = set()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        fddir = f"/proc/{pid}/fd"
        try:
            entries = os.listdir(fddir)
        except OSError:
            continue
        for fd in entries:
            try:
                if os.readlink(f"{fddir}/{fd}") in targets:
                    pids.add(int(pid))
                    break
            except OSError:
                continue
    return pids


def read_rfkill():
    """Radio on/off from sysfs: {'bluetooth': True, 'wlan': True, ...}."""
    st = {}
    for d in glob.glob("/sys/class/rfkill/rfkill*"):
        try:
            t = open(os.path.join(d, "type")).read().strip()
            soft = open(os.path.join(d, "soft")).read().strip()
            hard = open(os.path.join(d, "hard")).read().strip()
            st[t] = (soft == "0" and hard == "0")
        except OSError:
            pass
    return st


def read_backlight_pct():
    for d in glob.glob("/sys/class/backlight/*"):
        cur = read_int(os.path.join(d, "brightness"))
        mx = read_int(os.path.join(d, "max_brightness"))
        if cur is not None and mx:
            return round(cur * 100 / mx)
    return None


def read_kbd_backlight():
    for d in glob.glob("/sys/class/leds/*kbd*backlight*"):
        v = read_int(os.path.join(d, "brightness"))
        if v is not None:
            return v
    return None


def bt_connected_devices():
    """Names of currently-connected Bluetooth devices via org.bluez (rootless)."""
    try:
        mgr = dbus.Interface(sysbus().get_object("org.bluez", "/"),
                             "org.freedesktop.DBus.ObjectManager")
        names = []
        for _path, ifaces in mgr.GetManagedObjects().items():
            dev = ifaces.get("org.bluez.Device1")
            if dev and dev.get("Connected"):
                names.append(str(dev.get("Alias") or dev.get("Name") or "?"))
        return names
    except Exception:
        return []


class NapDaemon(dbus.service.Object):
    def __init__(self, bus):
        super().__init__(bus, OBJ_PATH)
        self.cg = CgroupView()
        self.focused_scope = None
        self.focused_app = None
        self.last_focus_change = time.monotonic()
        self.lost_focus_at = {}        # scope_dir -> ts when it stopped being focused
        # separate delta-state: the background tick and the live Status() view
        # each keep their own previous sample so they never corrupt each other.
        self.tick_store = {}           # used by the 20s enforce/logging tick
        self.live_store = {}           # used on-demand by Status() (napctl)
        self.report = {}               # last tick snapshot (fallback)
        self.sysinfo = {}
        self.ncpu = os.cpu_count() or 1
        self._cache = {}               # ttl cache for subprocess reads
        self.calib = Calibrator()      # learns watts = base + k·cpu% on battery
        self._net_prev = None          # (rx+tx bytes, ts) for wifi throughput
        self.capped = set()            # scopes we currently throttle (enforce)
        GLib.timeout_add_seconds(CONFIG["sample_interval_sec"], self._tick)
        log(f"started in {CONFIG['mode'].upper()} mode "
            f"(tick={CONFIG['sample_interval_sec']}s, live=on-demand, "
            f"flag>{CONFIG['bg_cpu_flag_pct']}%core)")
        self._collect(self.live_store)  # prime the live baseline
        self._tick()                    # prime tick baseline + first decisions

    def _cached(self, key, ttl, fn):
        """Memoize a (possibly subprocess) read for `ttl` seconds — keeps
        watch -n1 from spawning a pactl/powerprofilesctl every second."""
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit is not None and (now - hit[1]) < ttl:
            return hit[0]
        val = fn()
        self._cache[key] = (val, now)
        return val

    # ---- D-Bus: receive focus events from the KWin script --------------
    @dbus.service.method(IFACE, in_signature="sss", out_signature="")
    def FocusChanged(self, pid, cls, caption):
        try:
            pid = int(pid)
        except ValueError:
            return
        scope = self.cg.scope_for_pid(pid)
        if scope == self.focused_scope:
            return
        if self.focused_scope:
            self.lost_focus_at[self.focused_scope] = time.monotonic()
            if CONFIG["mode"] == "enforce" and self.focused_scope in self.capped:
                pass  # grace handled in tick
        self.focused_scope = scope
        self.focused_app = self.cg.appid_from_scope(os.path.basename(scope)) if scope else None
        self.last_focus_change = time.monotonic()
        # focused app must never stay capped — release immediately
        if CONFIG["mode"] == "enforce" and scope in self.capped:
            self.cg.clear_cap(scope)
            self.capped.discard(scope)
        log(f"focus -> {self.focused_app or '?'} ({cls})")

    # ---- D-Bus: status for napctl / future Plasma applet ---------------
    @dbus.service.method(IFACE, in_signature="", out_signature="s")
    def Status(self):
        # live measurement: delta since the previous Status() call, so
        # `watch -n1 napctl` updates at the watcher's cadence.
        report, sysinfo, _, _, unmanaged = self._collect(self.live_store)
        apps = sorted(report.values(),
                      key=lambda a: a["cpu_pct"] if a["cpu_pct"] is not None else -1,
                      reverse=True)
        meta = {
            "mode": CONFIG["mode"],
            "focused": self.focused_app,
            "sample_interval_sec": CONFIG["sample_interval_sec"],
        }
        meta.update(sysinfo)
        # report napd's own cost from the steady background tick, not this
        # heavier on-demand call (which would over-report it)
        meta["napd_cpu_pct"] = self.sysinfo.get("napd_cpu_pct", meta["napd_cpu_pct"])
        meta["napd_watts_est"] = self.sysinfo.get("napd_watts_est", meta["napd_watts_est"])
        return json.dumps({"meta": meta, "apps": apps, "unmanaged": unmanaged})

    # ---- core sampler: measure once, relative to `store`'s prev sample --
    def _collect(self, store):
        now = time.monotonic()
        cpu_prev = store.setdefault("cpu", {})

        # watts attribution: calibrated slope if learned, else rough constant.
        slope = self.calib.k                       # watts per 1 core-% (or None)

        def watts_of(pct):
            if pct is None:
                return None
            if slope is not None:
                return round(slope * pct, 2)
            return round(pct / 100.0 * CONFIG["watts_per_core"], 2)

        audio_scopes = self._cached("audio", 3.0, self._audio_scopes)
        capmap = self._cached("capture", 3.0, self._capture_map)
        # protect at the APP level: if any scope of an app uses camera/mic,
        # every scope of that app is protected (Chrome holds the camera in a
        # utility process but the meeting renderer lives in a sibling scope).
        capture_apps = {}
        for s, tag in capmap.items():
            aid = self.cg.appid_from_scope(os.path.basename(s))
            prev = set(filter(None, capture_apps.get(aid, "").split("+")))
            capture_apps[aid] = "+".join(sorted(prev | set(tag.split("+"))))

        # power source decides whether we actually throttle (full speed on AC)
        total_w, on_batt = read_total_watts()
        enforcing = (CONFIG["mode"] == "enforce"
                     and (on_batt or CONFIG["enforce_on_ac"]))

        report = {}
        would_throttle = []
        eligible = set()           # background + unprotected (cap may persist here)
        for scope in self.cg.scopes():
            usage = self.cg.usage_usec(scope)
            if usage is None:
                continue
            appid = self.cg.appid_from_scope(os.path.basename(scope))
            prev = cpu_prev.get(scope)
            cpu_prev[scope] = (usage, now)
            if prev is None:
                pct = None
            else:
                dt = (now - prev[1]) * 1_000_000  # to usec
                pct = round(100.0 * (usage - prev[0]) / dt, 1) if dt > 0 else 0.0

            focused = scope == self.focused_scope
            # app-level capture: show the whole app's cam/mic on every scope
            cap_tag = capture_apps.get(appid, "") or capmap.get(scope, "")
            protected_by = None
            if cap_tag:                       # camera/mic in use — highest priority
                protected_by = cap_tag
            elif any(p in appid for p in CONFIG["protect_apps"]):
                protected_by = "allowlist"
            elif scope in audio_scopes:
                protected_by = "audio"
            elif scope in self.lost_focus_at and (now - self.lost_focus_at[scope]) < CONFIG["focus_grace_sec"]:
                protected_by = "focus-grace"

            if not focused and not protected_by:
                eligible.add(scope)

            busy = pct is not None and pct > CONFIG["bg_cpu_flag_pct"]
            if focused:
                status, reason = "focused", ""
            elif not busy:
                status, reason = "idle", ""
            elif protected_by:
                status, reason = "watching", f"⚠ busy but protected ({protected_by})"
            else:
                status = "throttled" if enforcing else "would-throttle"
                reason = f"background, {pct}%core"
                would_throttle.append((scope, appid, pct))

            # a scope we're actively capping reads as low-CPU — still show it CAPPED
            if scope in self.capped:
                status = "throttled"
                if not reason:
                    reason = "capped (background)"

            report[scope] = {
                "app": appid,
                "state": "focused" if focused else "background",
                "cpu_pct": pct,
                "watts_est": watts_of(pct),
                "status": status,
                "reason": reason,
                "capture": cap_tag,
            }

        # ---- system-wide breakdown: total / manageable / unmanageable -----
        manageable_pct = sum(r["cpu_pct"] for r in report.values()
                             if r["cpu_pct"] is not None)
        sys_core_pct = None
        stat = read_cpu_total()
        if stat is not None:
            sp = store.get("stat")
            if sp is not None:
                dtot, didle = stat[0] - sp[0], stat[1] - sp[1]
                if dtot > 0:
                    sys_core_pct = (1.0 - didle / dtot) * self.ncpu * 100.0
            store["stat"] = stat
        unmanaged_pct = (max(0.0, sys_core_pct - manageable_pct)
                         if sys_core_pct is not None else None)

        # ---- self-cost accounting -----------------------------------------
        own_pct = store.get("own_pct", 0.0)
        if self.cg.own:
            own_usage = read_first_int(os.path.join(self.cg.own, "cpu.stat"), "usage_usec")
            op = store.get("own")
            if own_usage is not None and op is not None:
                dt = (now - op[1]) * 1_000_000
                own_pct = 100.0 * (own_usage - op[0]) / dt if dt > 0 else own_pct
            if own_usage is not None:
                store["own"] = (own_usage, now)
            store["own_pct"] = own_pct

        cal = self.calib
        calibrated = slope is not None
        # CPU-dynamic watts = what throttling can actually reclaim; the rest of
        # the total is the fixed baseline (screen/GPU/wifi) we cannot touch.
        cpu_dyn_w = watts_of(sys_core_pct) if (calibrated and sys_core_pct is not None) else None
        if calibrated and total_w is not None and cpu_dyn_w is not None:
            fixed_base_w = round(max(0.0, total_w - cpu_dyn_w), 1)   # measured floor
        elif calibrated:
            fixed_base_w = round(cal.base, 1)                       # learned intercept
        else:
            fixed_base_w = None

        sysinfo = {
            "source": ("battery" if on_batt else "AC"),
            "total_watts": total_w,
            "amdgpu_watts": read_amdgpu_watts(),
            "power_profile": self._cached("profile", 3.0, read_power_profile),
            "ncpu": self.ncpu,
            "sys_core_pct": round(sys_core_pct, 1) if sys_core_pct is not None else None,
            "manageable_core_pct": round(manageable_pct, 1),
            "unmanageable_core_pct": round(unmanaged_pct, 1) if unmanaged_pct is not None else None,
            "manageable_watts_est": watts_of(manageable_pct),
            "napd_cpu_pct": round(own_pct, 2),
            "napd_watts_est": watts_of(own_pct),
            # --- calibration + addressability (the "what are we fighting for") -
            "calibrated": calibrated,
            "calib_per_core_w": round(slope * 100, 2) if calibrated else None,
            "calib_baseline_w": round(cal.base, 1) if calibrated else None,
            "calib_n": len(cal.pts),
            "calib_r2": round(cal.r2, 2) if (calibrated and cal.r2 is not None) else None,
            "addressable_watts": watts_of(manageable_pct) if calibrated else None,
            "cpu_dynamic_watts": cpu_dyn_w,
            "sys_kernel_watts": watts_of(unmanaged_pct) if (calibrated and unmanaged_pct is not None) else None,
            "fixed_baseline_w": fixed_base_w,
            "peripherals": self._cached("peripherals", 3.0, self._peripherals),
        }
        # scanning all of system.slice is the heaviest read here — cache it so
        # `watch -n1 napctl` doesn't re-glob dozens of units every second.
        unmanaged = self._cached("unmanaged", 3.0,
                                 lambda: self._collect_unmanaged(store, now, watts_of))
        return report, sysinfo, would_throttle, eligible, unmanaged

    def _collect_unmanaged(self, store, now, watts_of):
        """Read-only view of consumers we can SEE but do NOT manage:
        the user's session.slice (compositor/shell) and the root system.slice
        (daemons like the antivirus). cpu.stat is world-readable, but we never
        write cpu.max here — throttling the compositor or security software is
        not ours to do."""
        prev = store.setdefault("unmanaged", {})
        uid = os.getuid()
        roots = [
            (f"/sys/fs/cgroup/user.slice/user-{uid}.slice/"
             f"user@{uid}.service/session.slice", "session"),
            ("/sys/fs/cgroup/system.slice", "system"),
        ]
        out = []
        for root, src in roots:
            for unit in (glob.glob(root + "/*.service") + glob.glob(root + "/*.scope")):
                usage = read_first_int(os.path.join(unit, "cpu.stat"), "usage_usec")
                if usage is None:
                    continue
                p = prev.get(unit)
                prev[unit] = (usage, now)
                if p is None:
                    continue
                dt = (now - p[1]) * 1_000_000
                pct = 100.0 * (usage - p[0]) / dt if dt > 0 else 0.0
                if pct < CONFIG["bg_cpu_flag_pct"]:
                    continue
                name = re.sub(r"\.(service|scope)$", "", os.path.basename(unit))
                name = re.sub(r"\\x([0-9a-fA-F]{2})",
                              lambda g: chr(int(g.group(1), 16)), name)
                out.append({"app": name, "source": src,
                            "cpu_pct": round(pct, 1),
                            "watts_est": watts_of(pct)})
        out.sort(key=lambda u: u["cpu_pct"], reverse=True)
        return out[:8]

    def _streams_to_scopes(self, recs, skip_corked=True):
        """Map pactl streams to cgroup scopes, robust to flatpak PID namespaces:
        prefer the portal app_id (exact), then host pid, then binary name."""
        scope_appid = {s: self.cg.appid_from_scope(os.path.basename(s))
                       for s in self.cg.scopes()}
        out = set()
        for r in recs:
            if skip_corked and r["corked"]:
                continue
            if r["appid"]:
                out |= {s for s, a in scope_appid.items() if a == r["appid"]}
            elif r["pid"]:
                s = self.cg.scope_for_pid(r["pid"])
                if s:
                    out.add(s)
            elif r["binary"]:
                b = r["binary"].lower()
                out |= {s for s, a in scope_appid.items() if b in a.lower()}
        return out

    # cached raw probes — shared by audio/capture/peripherals so we never run
    # the same pactl/fuser more than once per 3s, even under `watch -n1`.
    def _sinks(self):
        return self._cached("pa_sinks", 5.0, lambda: pactl_streams("sink-inputs"))

    def _sources(self):
        return self._cached("pa_sources", 5.0, lambda: pactl_streams("source-outputs"))

    def _cam_pids(self):
        # camera state changes slowly (it's open for a whole call) — scan rarely
        return self._cached("cam_pids", 12.0, camera_holder_pids)

    def _appids_of(self, scopes):
        return sorted({self.cg.appid_from_scope(os.path.basename(s)) for s in scopes})

    def _audio_scopes(self):
        return self._streams_to_scopes(self._sinks())

    def _capture_map(self):
        """scope -> 'cam' / 'mic' / 'cam+mic' for apps using camera or
        microphone. These are NEVER throttled — a backgrounded video call
        must keep capturing."""
        m = {}
        for pid in self._cam_pids():
            s = self.cg.scope_for_pid(pid)
            if s:
                m.setdefault(s, set()).add("cam")
        for s in self._streams_to_scopes(self._sources()):
            m.setdefault(s, set()).add("mic")
        return {s: "+".join(sorted(t)) for s, t in m.items()}

    def _peripherals(self):
        """Activated, energy-relevant hardware/states worth seeing at a glance:
        camera, mic, audio, Bluetooth, Wi-Fi, display, keyboard backlight.
        `hot` marks the notable ones (active capture / connected BT / bright)."""
        items = []
        cam = {s for s in (self.cg.scope_for_pid(p) for p in self._cam_pids()) if s}
        if cam:
            items.append({"icon": "🎥", "name": "camera",
                          "detail": ", ".join(self._appids_of(cam)), "hot": True})
        mic = self._streams_to_scopes(self._sources())
        if mic:
            items.append({"icon": "🎤", "name": "microphone",
                          "detail": ", ".join(self._appids_of(mic)), "hot": True})
        aud = self._streams_to_scopes(self._sinks())
        if aud:
            items.append({"icon": "🔊", "name": "audio out",
                          "detail": ", ".join(self._appids_of(aud)), "hot": False})
        rf = read_rfkill()
        if rf.get("bluetooth"):
            dev = bt_connected_devices()
            items.append({"icon": "📶", "name": "bluetooth",
                          "detail": (f"on · {', '.join(dev)}" if dev else "on · idle"),
                          "hot": bool(dev)})
        if rf.get("wlan"):
            detail = "on"
            wifi = next((os.path.basename(n) for n in glob.glob("/sys/class/net/*")
                         if os.path.isdir(os.path.join(n, "wireless"))), None)
            if wifi:
                rx = read_int(f"/sys/class/net/{wifi}/statistics/rx_bytes") or 0
                tx = read_int(f"/sys/class/net/{wifi}/statistics/tx_bytes") or 0
                now = time.monotonic()
                prev, self._net_prev = self._net_prev, (rx + tx, now)
                if prev and now > prev[1]:
                    kbs = (rx + tx - prev[0]) / (now - prev[1]) / 1024
                    detail = f"on · {kbs:.0f} KB/s"
            items.append({"icon": "📡", "name": "wi-fi", "detail": detail,
                          "hot": False})
        b = read_backlight_pct()
        if b is not None:
            items.append({"icon": "🔆", "name": "display",
                          "detail": f"{b}%", "hot": b >= 70})
        k = read_kbd_backlight()
        if k:
            items.append({"icon": "⌨", "name": "kbd backlight",
                          "detail": f"level {k}", "hot": False})
        return items

    # ---- background tick: enforce decisions + periodic logging ---------
    def _tick(self):
        report, sysinfo, would_throttle, eligible, _ = self._collect(self.tick_store)
        self.report, self.sysinfo = report, sysinfo

        # feed the watt model — only on battery, where total draw is real
        if sysinfo.get("source") == "battery":
            self.calib.add(sysinfo.get("sys_core_pct"), sysinfo.get("total_watts"))

        enforcing = (CONFIG["mode"] == "enforce"
                     and (sysinfo.get("source") == "battery" or CONFIG["enforce_on_ac"]))
        if enforcing:
            # start capping a background hog once it's busy...
            for scope, appid, pct in would_throttle:
                if scope not in self.capped and self.cg.set_cap(scope, CONFIG["throttle_pct"]):
                    self.capped.add(scope)
                    log(f"THROTTLE {appid} -> {CONFIG['throttle_pct']}%core (was {pct}%)")
            # ...keep it capped while it stays background+unprotected (the cap
            # itself lowers its CPU, so don't release on low usage — only when it
            # gains focus / starts cam-mic-audio / exits). No flapping.
            for scope in list(self.capped):
                if scope not in eligible:
                    self.cg.clear_cap(scope)
                    self.capped.discard(scope)
                    log(f"release {os.path.basename(scope)}")
        else:
            # observe mode OR on AC charger → full speed, drop every cap
            if self.capped:
                log(f"on AC / observe — releasing {len(self.capped)} cap(s)")
            for scope in list(self.capped):
                self.cg.clear_cap(scope)
                self.capped.discard(scope)

        return True  # keep the GLib timer alive

    # ---- KWin script lifecycle -----------------------------------------
    def load_kwin_script(self, attempt=1):
        try:
            subprocess.run(["qdbus6", "org.kde.KWin", "/Scripting",
                            "org.kde.kwin.Scripting.loadScript",
                            KWIN_SCRIPT, KWIN_SCRIPT_NAME],
                           capture_output=True, timeout=5)
            subprocess.run(["qdbus6", "org.kde.KWin", "/Scripting",
                            "org.kde.kwin.Scripting.start"],
                           capture_output=True, timeout=5)
            log("KWin focus script loaded")
        except (OSError, subprocess.SubprocessError) as e:
            if attempt < 10:
                GLib.timeout_add_seconds(3, lambda: self.load_kwin_script(attempt + 1) and False)
            else:
                log(f"could not load KWin script: {e}")
        return False

    def unload_kwin_script(self):
        try:
            subprocess.run(["qdbus6", "org.kde.KWin", "/Scripting",
                            "org.kde.kwin.Scripting.unloadScript",
                            KWIN_SCRIPT_NAME], capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass

    def shutdown(self):
        log("shutting down — releasing caps and unloading KWin script")
        for scope in list(self.capped):
            self.cg.clear_cap(scope)
        self.calib._save()
        self.unload_kwin_script()


def main():
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    name = dbus.service.BusName(BUS_NAME, bus, do_not_queue=True)  # noqa: F841
    daemon = NapDaemon(bus)
    daemon.load_kwin_script()

    loop = GLib.MainLoop()

    def stop(*_):
        daemon.shutdown()
        loop.quit()

    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, lambda: (stop(), False)[1])

    try:
        loop.run()
    finally:
        daemon.shutdown()


if __name__ == "__main__":
    main()
