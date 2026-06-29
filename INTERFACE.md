# napd D-Bus interface

This is the contract between the `napd` service and any UI (the `napctl` CLI, a
KDE Plasma applet, a GNOME Shell extension, …). A UI is a **thin client**: it
calls one method and renders the result. It never reads `napd`'s code or files.

## Endpoint

| | |
|---|---|
| Bus | **session** bus |
| Service name | `ai.palabra.NapD` |
| Object path | `/ai/palabra/NapD` |
| Interface | `ai.palabra.NapD` |

### `Status() → s`

Returns a single JSON string: a snapshot of the current state. Computed live on
each call (a fresh CPU sample relative to the previous call) — so polling at your
own cadence gives you that cadence's resolution.

**Polling guidance:** call every **2–3 s** for a live tray. It's cheap — cgroup
`cpu.stat` reads plus probes (audio/camera/etc.) that are internally cached for
several seconds. There is no change signal yet (planned); poll for now.

### `DailyUsage() → s`

Returns a JSON string with per-app energy accumulated since local midnight —
for a "today's battery use" view. Poll rarely (e.g. only while the popup is open).

```jsonc
{ "day": "2026-06-29",
  "apps": [ { "app": "com.google.Chrome", "wh": 4.21 } ] }   // sorted desc, top ~15
```

### `FocusChanged(s pid, s class, s caption)` — internal

Called by the bundled KWin script to push focus changes. **Not for UIs.**

## `Status()` JSON schema

```jsonc
{
  "meta": {
    // --- session / mode ---
    "mode":        "enforce",        // "enforce" | "observe"
    "focused":     "org.kde.konsole",// app-id of the focused window, or null
    "sample_interval_sec": 10,

    // --- power source & real meters ---
    "source":      "battery",        // "battery" | "AC"
    "total_watts": 6.5,              // real system draw; null on AC (unmeasurable)
    "amdgpu_watts":4.2,              // SoC/GPU package power; null if no amdgpu
    "power_profile":"power-saver",   // PPD active profile, or null
    "ncpu":        16,

    // --- CPU activity (% of ONE core; can exceed 100) ---
    "sys_core_pct":            135.0,// whole system
    "manageable_core_pct":     114.0,// our managed apps
    "unmanageable_core_pct":   21.0, // session + system + kernel

    // --- watt breakdown (estimated; see calibration) ---
    "manageable_watts_est":    2.8,
    "addressable_watts":       2.8,  // = manageable, what throttling can reduce
    "cpu_dynamic_watts":       3.3,  // all CPU-dynamic power
    "sys_kernel_watts":        0.5,
    "fixed_baseline_w":        3.2,  // screen/GPU/wifi/idle — untouchable
    "napd_cpu_pct":            1.0,  // napd's own cost (from the steady tick)
    "napd_watts_est":          0.0,

    // --- calibration (the watt model: watts ≈ base + k·cpu%) ---
    "calibrated":      true,         // false while learning / on too little data
    "calib_per_core_w":2.43,         // k·100  (W per 100% of one core)
    "calib_baseline_w":4.0,          // fitted intercept
    "calib_n":         256,          // samples in the rolling window
    "calib_r2":        0.67,         // fit quality (null if N/A)

    // --- active hardware (only entries that are ON) ---
    "peripherals": [
      { "icon": "📶", "name": "bluetooth", "detail": "on · idle", "hot": false },
      { "icon": "📡", "name": "wi-fi",     "detail": "on · 17 KB/s", "hot": false },
      { "icon": "🔆", "name": "display",   "detail": "30%", "hot": false }
      // hot=true marks notable consumers (active camera/mic, connected BT, bright)
    ]
  },

  // --- managed apps. NOTE: one app may appear as several entries (it can span
  //     several cgroup scopes). Aggregate by "app" for display. ---
  "apps": [
    {
      "app":      "org.kde.konsole", // app-id
      "state":    "focused",         // "focused" | "background"
      "cpu_pct":  83.3,              // % of one core, or null (first sample)
      "watts_est":2.0,               // null if cpu_pct is null
      "status":   "focused",         // see status values below
      "reason":   "",                // human note, e.g. the protection reason
      "capture":  "",                // "" | "cam" | "mic" | "cam+mic"
      "anom":     false,             // true = sustained CPU above this app's own usual
      "usual":    25.0               // median CPU% baseline (%core), or null until learned
    }
  ],

  // --- consumers we can SEE but not manage (read-only) ---
  "unmanaged": [
    { "app": "plasma-kwin_wayland", "source": "session", // "session" | "system"
      "cpu_pct": 5.1, "watts_est": 0.1 }
  ]
}
```

### `apps[].status` values

| value | meaning | suggested label |
|---|---|---|
| `focused`        | the active window | `FOCUS` |
| `idle`           | background, below the CPU flag | `idle` / `·` |
| `watching`       | background & busy but **protected** (see `reason`) | `PROTECTED · <why>` |
| `would-throttle` | background hog that *would* be capped (observe mode, or on AC) | `would cap` |
| `throttled`      | background hog **currently capped** | `CAPPED` |

`reason` for `watching` looks like `⚠ busy but protected (focus-grace)` — the tag
in parentheses is one of `cam+mic` / `audio` / `allowlist` / `focus-grace`.

## Consuming it

**Shell** (qdbus6 or gdbus):
```sh
qdbus6 ai.palabra.NapD /ai/palabra/NapD ai.palabra.NapD.Status
gdbus call -e -d ai.palabra.NapD -o /ai/palabra/NapD -m ai.palabra.NapD.Status
```

**Python** (`napctl` is the reference implementation):
```python
import dbus, json
obj = dbus.SessionBus().get_object("ai.palabra.NapD", "/ai/palabra/NapD")
data = json.loads(obj.Status(dbus_interface="ai.palabra.NapD"))
```

**Plasma applet / GNOME extension:** call `Status()` on a 2–3 s timer, parse the
JSON, render the tray icon + popup. Aggregate `apps` by `app`. A good tray glyph
maps from `meta.source` + whether any app is `throttled` + `meta.peripherals`
(`hot`).

## Stability

The contract is the JSON **shape** above. Changes will be additive (new fields);
consumers should ignore unknown fields and tolerate missing/null ones. If a
breaking change is ever needed, a `meta.schema` version field will be added
first.
