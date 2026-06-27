# napd

**Focus-aware power manager for Linux — a user-space "App Nap".**

`napd` watches which window you're actually using and throttles the background
CPU hogs that quietly drain your battery, releasing them the instant they regain
focus. It also gives you a clear read-out of **who is burning the battery** and
**how much of your power draw you can even do anything about**.

All user-space: no root daemon, no kernel patches, no `sudo`. It rides the cgroup
v2 controllers that systemd already delegates to your session.

```
power: BATTERY · power-saver · ENFORCE · focus: org.kde.konsole
────────────────────────────────────────────────────────────────────
POWER  (~W calibrated · 16 cores)
  total (real)             6.5 W   135%
  SoC/GPU (amdgpu)         4.2 W
  ├ addressable            3.3 W   135%   ← throttling can reduce
  │   apps                 2.8 W   114%
  │   system/kernel        0.5 W    21%
  └ fixed baseline         3.2 W          screen/GPU/wifi/idle
  napd itself              0.0 W     1%

ACTIVE HARDWARE
  📶 bluetooth    on · idle
  📡 wi-fi        on · 17 KB/s
  🔆 display      30%

  APPS (managed)              CPU       ~W   STATE       MANAGEMENT
  org.kde.konsole           83.3%    2.0 W   focused     FOCUS
  com.google.Chrome         20.1%    0.5 W   background  CAPPED
  firefox                   10.3%    0.2 W   background  PROTECTED · focus-grace
```

## Why it's not TLP / powertop

`napd` manages the **software** layer (which app gets CPU, by focus). Tools like
`power-profiles-daemon` (PPD) manage the **hardware** layer (CPU EPP, platform
profile). They share no knobs, so — unlike TLP — `napd` does **not** conflict
with the native systemd/upower/PPD stack. Run both.

## Features

- 🎯 **Focus-aware throttling** — background CPU hogs are capped (cgroup `cpu.max`,
  default 10% of one core) and released **instantly** when you focus them.
- 🔋 **Battery-only** — on AC charger nothing is throttled (full speed). Configurable.
- 🛡️ **Never throttles** an app using the **camera/mic** (video calls), playing
  **audio**, the desktop shell (allowlist), or one you focused in the last 30s.
- 📊 **Power read-out** — real total draw (battery), SoC/GPU package (amdgpu), and
  a calibrated split into *addressable* (what throttling can reduce) vs *fixed
  baseline* (screen/GPU/wifi — untouchable).
- ⚡ **Active-hardware panel** — camera, mic, audio, Bluetooth (+device), Wi-Fi
  throughput, display, keyboard backlight — shows only what's on.
- 🪶 **Cheap** — event-driven (focus is a D-Bus push), ~1–2% of one core idle.
- 🔌 **D-Bus contract** — every UI (the `napctl` CLI, a future Plasma applet, a
  GNOME extension) is a thin client of one `Status()` call. See
  [`INTERFACE.md`](INTERFACE.md).

## Requirements

- KDE Plasma 6 on **Wayland** (focus comes from a KWin script)
- cgroup v2 with the `cpu` controller delegated to the user session (systemd default)
- `python3`, `python3-dbus`, `python3-gi` (present on most KDE/GNOME systems)
- AMD GPU for the `amdgpu` watt read-out (optional; everything else works without it)

## Install

### Debian / Ubuntu / KDE neon (`.deb`)

Grab the latest `.deb` from [**Releases**](https://github.com/metallcorn/napd/releases/latest):

```sh
curl -LO https://github.com/metallcorn/napd/releases/download/v0.1.0/napd_0.1.0_all.deb
sudo apt install ./napd_0.1.0_all.deb
systemctl --user enable --now napd        # or just log out and back in
```

Or build it yourself (only needs `dpkg-deb`):

```sh
./packaging/build-deb.sh                  # → napd_0.1.0_all.deb
```

### From source (any distro)

```sh
git clone https://github.com/metallcorn/napd
cd napd
./install.sh          # systemd --user service, no root
```

> Use **one** method. The source `install.sh` puts a unit in `~/.config`, which
> shadows the packaged one — run `./uninstall.sh` before switching to the `.deb`.

### Use

```sh
napctl                        # status read-out  (./napctl from a source checkout)
journalctl --user -u napd -f  # live decisions log
```

## Configuration

Edit the `CONFIG` block at the top of `napd.py`, then
`systemctl --user restart napd`:

| Key | Default | Meaning |
|---|---|---|
| `mode` | `enforce` | `enforce` (throttle) or `observe` (only report) |
| `throttle_pct` | `10` | background cap, % of one core |
| `enforce_on_ac` | `False` | also throttle on AC charger |
| `bg_cpu_flag_pct` | `2.0` | a background app above this is a candidate |
| `focus_grace_sec` | `30` | don't touch an app for N s after it loses focus |
| `sample_interval_sec` | `10` | how fast caps apply / decisions refresh |
| `protect_apps` | konsole, kwin, … | substrings of app-ids that are never throttled |

## How it works

| Concern | Mechanism |
|---|---|
| Focus → app | KWin script `napd-focus.js` → `FocusChanged` over D-Bus |
| App → cgroup | `/proc/PID/cgroup` → `app-*.scope`/`app-*.service` under the user `app.slice` |
| Throttle / release | write `cpu.max` to the app's scope (delegated, no root) |
| Per-app CPU | `cpu.stat usage_usec` deltas → % of one core |
| Mic / audio | `pactl` (forced `LC_ALL=C`), mapped via flatpak `portal.app_id` → pid → binary |
| Camera | in-process `/proc/*/fd` scan for `/dev/video*` holders |
| Watts | real total from `BAT0`; per-app via a calibrated `watts ≈ base + k·cpu%` model |

## Power model — "what are we actually fighting for"

No kernel interface exposes per-process wattage, so `napd` derives it. On battery
it fits `watts ≈ base + k·cpu_core%` over a rolling window
(`~/.local/state/napd/calib.json`) and splits the **real** total draw into:

- **addressable (CPU-dynamic)** `= k·cpu%` — what throttling can reclaim (further
  split into *our apps* vs *system/kernel*);
- **fixed baseline** — the rest (screen, GPU, wifi, idle SoC) which no amount of
  app throttling can touch.

This is the honest answer to "is this even worth it": on a typical laptop the
fixed baseline dominates, and app-level power management buys the CPU-dynamic
slice — most valuable for heat/fan noise and runaway background apps.

## Coverage

- **managed** (see + throttle): user `app.slice` units.
- **visible, not managed** (read-only): `session.slice` (compositor/shell) and root
  `system.slice` daemons — `cpu.stat` is world-readable, so they're surfaced but
  never throttled.
- **unattributable**: kernel threads, and non-CPU power (display/GPU/wifi/radio) —
  these have no per-device meter and live in the fixed baseline.

## Status

Works on the author's AMD ThinkPad (Plasma 6 / Wayland). It's a focused personal
tool, not (yet) a packaged distro service — see the issues/roadmap for `.deb`
packaging and a Plasma applet.

## License

MIT — see [LICENSE](LICENSE).
