---
name: deepseek-usage-monitor
description: Launch, refresh, inspect, or stop the live DeepSeek cost and token widget that stays beside the Codex desktop partner.
---

# DeepSeek Usage Monitor

Use `scripts/start_widget.ps1` to start the persistent slim companion. It has a transparent silhouette, a red outline, today's estimated cost badge, gentle floating motion, blinking eyes, waving arms, and a breathing status light. A stationary hover expands the usage cards; pressing and dragging the companion cancels hover expansion, moves freely across monitors, and snaps to nearby screen edges. Cards use the single Chinese characters zong, ru, and chu for total, input, and output. Values use conversational Chinese units based on wan and yi. The panel refreshes every three seconds from local Codex session usage records.

The Startup shortcut runs `scripts/watch_for_codex.ps1`. That invisible watcher waits for the ChatGPT/Codex desktop process, starts the orb when the app starts, restarts it after an unexpected crash, and stops it when the app exits. An intentional close suppresses restart until ChatGPT/Codex starts again. It does not show the orb merely because Windows signed in.

## Commands

- Start or bring up the widget:
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<skill>/scripts/start_widget.ps1"`
- Stop it:
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<skill>/scripts/stop_widget.ps1"`
- Recreate the desktop and Startup shortcuts:
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<skill>/scripts/create_shortcuts.ps1"`
- Print one JSON snapshot without opening the widget:
  `python "<skill>/scripts/monitor.py" --snapshot`
- Enable or disable launch at Windows sign-in only when the user asks:
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<skill>/scripts/enable_autostart.ps1"`
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<skill>/scripts/disable_autostart.ps1"`

- Stop the ChatGPT/Codex process watcher:
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<skill>/scripts/stop_watcher.ps1"`

Replace `<skill>` with this skill's absolute directory.

## Behavior and limits

The widget reads `%USERPROFILE%\.codex\sessions`, deduplicates by response ID, persists an incremental SQLite index in `%LOCALAPPDATA%\DeepSeekUsageMonitor`, and never stores an API key. It classifies DeepSeek peak hours using China Standard Time, includes the official 2026 public holidays, applies historical pricing periods, and precomputes cost for fast SQL aggregation.

Use the `明细` button inside the expanded panel to switch between recent daily usage and monthly usage. The panel shows average cache-hit rate, peak/off-peak cost shares, and average cost per response. Closing with `×` stops the widget; the desktop shortcut `DeepSeek Usage` starts it again. The separate Startup shortcut only restarts it after the next Windows sign-in.

The totals cover DeepSeek calls recorded by this Codex installation. They do not include other devices or applications using the same API key. Compressed `.jsonl.zst` sessions are read through the optional Python `zstandard` module or the locally installed 7-Zip executable; the persistent index retains usage already observed while the widget was running. Unsupported models or dates are shown as unpriced instead of silently using the Flash rate.

Update `scripts/pricing.json` when DeepSeek publishes new prices or Chinese public-holiday dates. Keep historical pricing periods instead of overwriting them so new records use the correct rate.

Logs are written to `%LOCALAPPDATA%\DeepSeekUsageMonitor\widget.log`.
