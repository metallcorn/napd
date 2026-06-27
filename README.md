# napd — focus-aware power observer (KDE Wayland)

A user-space "App Nap" for Linux. It watches which window is focused and
attributes CPU/power to each app via cgroup v2, so you can see **who is
burning the battery in the background** — and (optionally) cap them.

Complements `power-profiles-daemon` rather than replacing it:
PPD manages the **hardware** layer (CPU EPP, platform profile); napd manages
the **software** layer (which app gets CPU, by focus). They share no knobs, so
unlike TLP there is no conflict with the native systemd/upower stack.

## Status: OBSERVE mode (v1)

Throttles **nothing**. It only reports and logs what it *would* do, and flags
apps it is unsure about (audio / allowlisted / recently focused) with a reason.
This is the "observe → trust → enforce" path: watch it, trust it, then enable.

## Why it's cheap

* **Event-driven**: focus arrives as a D-Bus push from a tiny KWin script; the
  GLib loop is blocked on epoll otherwise (~0% CPU at idle).
* A **slow sampler** (20s) reads cgroup `cpu.stat` (microseconds of work) to
  attribute power. No fast polling — napd must never be the wakeup source.
* **Self-accounting**: `napctl` shows napd's own cpu cost so you can verify it
  saves far more than it spends (measured ~0.05% core).

## Install / use

```sh
./install.sh                 # systemd --user service, no root
./napctl                     # status table (the future Plasma-applet backend)
journalctl --user -u napd -f # live log of focus + "would throttle" decisions
./uninstall.sh               # clean removal, releases any caps
```

## Enabling enforcement (later)

Edit `CONFIG["mode"]` in `napd.py` from `"observe"` to `"enforce"` and restart
(`systemctl --user restart napd`). In enforce mode, background apps above
`bg_cpu_flag_pct` that are **not** protected get `cpu.max` capped to
`throttle_pct` (default 10%) of one core, released the instant they regain
focus. Protected = camera/mic in use (whole app), on the allowlist, playing
audio, or within the focus grace window.

## How it works

| Concern | Mechanism |
|---|---|
| Focus → app | KWin script `napd-focus.js` → `FocusChanged` over D-Bus |
| App → cgroup | `/proc/PID/cgroup` → `app-*.scope` under the user `app.slice` |
| Power per app | `cpu.stat usage_usec` deltas → % of one core → rough watts |
| Audio protection | `pactl list sink-inputs` (non-corked) → protected PIDs |
| Capture protection | camera (`fuser /dev/video*`) + mic (`pactl source-outputs`); protects the **whole app**, so a backgrounded video call is never throttled |
| ⚡ ACTIVE panel | energy-relevant hardware/states: camera 🎥, mic 🎤, audio 🔊, Bluetooth 📶 (+connected device), Wi-Fi 📡, display 🔆, kbd backlight ⌨ — shows only what's on; `⚠ notable` flags the heavy ones |
| Stream→app mapping | `pactl` forced to `LC_ALL=C`; mapped via flatpak `portal.app_id` → host pid → binary (robust to sandbox PID namespaces) |
| Throttle | write `cpu.max` to the app scope (delegated, no root) |

## Power model — "what are we actually fighting for"

Per-process wattage isn't exposed by any kernel interface, so napd derives it:

* **Total draw** — real, from `BAT0` (`power_now`, or `current×voltage` when the
  gauge reports 0). Only meaningful on battery; `n/a` on AC.
* **SoC/GPU package** — real, from the `amdgpu` hwmon (`power1_average`).
  Readable even on AC; a direct "active silicon" number.
* **Calibration** — on battery, napd fits `watts ≈ base + k·cpu_core%` over a
  rolling window (`~/.local/state/napd/calib.json`). This splits the total into:
  * **addressable (CPU-dynamic)** = `k·cpu%` — what throttling can reclaim,
    broken into *our apps* vs *system/kernel*;
  * **fixed baseline** = the rest (screen/GPU/wifi/idle SoC) — untouchable.

  So the table answers directly: of N watts, you can fight for ~X. Needs a few
  minutes on battery with varying load to lock in `k` and `base`.

## Coverage — what we see vs manage

* **managed** (see + throttle): user `app.slice` units — both `app-*.scope`
  (KDE/flatpak) and `app-*.service` (e.g. Firefox).
* **visible, not managed** (read-only): `session.slice` (kwin/plasmashell) and
  root `system.slice` daemons — `cpu.stat` is world-readable, so we surface them
  (this is how we caught ESET antivirus eating ~20–40% CPU) but never throttle.
* **unattributable**: kernel threads, GPU/display/wifi power — stays in baseline.

## Known refinements (not yet done)

* RAPL CPU-package energy (`intel-rapl/energy_uj`) would give exact CPU watts but
  is root-gated here — would need a small system helper.
* Audio detection uses `pactl` per sample tick; a PipeWire event subscription
  would be fully event-driven for enforce mode.
