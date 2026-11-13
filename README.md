# Codex DeepSeek Usage Monitor

A Windows floating desktop companion for Codex that monitors local DeepSeek API cost and token usage.

<p align="center">
  <img src="plugins/deepseek-usage-monitor/assets/icon.svg" alt="DeepSeek Usage Monitor icon" width="96">
</p>

## Features

- Floating animated companion with today's estimated cost.
- Hover to expand today, week, and month summaries.
- Daily and monthly usage details.
- Separate total, input, and output token counts.
- Cache-hit rate and peak/off-peak cost shares.
- Official DeepSeek historical pricing periods and 2026 Chinese public holidays.
- Incremental SQLite indexing from local Codex session logs.
- Multi-monitor positioning and screen-edge snapping.
- Automatic launch with ChatGPT/Codex, plus crash recovery.

The widget never stores a DeepSeek API key. All calculations use local Codex session usage records and the pricing configuration in `plugins/deepseek-usage-monitor/skills/deepseek-usage-monitor/scripts/pricing.json`.

## Requirements

- Windows 10 or Windows 11
- Codex desktop
- Python 3.10 or newer with Tkinter
- 7-Zip recommended for reading compressed Codex session logs

## Install

Clone the repository and add it as a local Codex plugin marketplace:

```powershell
git clone https://github.com/<owner>/codex-deepseek-usage-monitor.git
cd codex-deepseek-usage-monitor

codex plugin marketplace add .
codex plugin add deepseek-usage-monitor@deepseek-usage-monitor
```

Restart the ChatGPT/Codex desktop application after installation so it reloads the plugin inventory.

## Use

Start the widget directly:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File ".\plugins\deepseek-usage-monitor\skills\deepseek-usage-monitor\scripts\start_widget.ps1"
```

- Hold the companion to drag it.
- Move it near a screen edge and release to snap.
- Rest on the companion for about 0.65 seconds to expand the usage panel.
- Use the `明细` button for daily and monthly details.
- Closing the companion intentionally prevents automatic restart until ChatGPT/Codex starts again.

Create the desktop and Startup shortcuts:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File ".\plugins\deepseek-usage-monitor\skills\deepseek-usage-monitor\scripts\create_shortcuts.ps1"
```

## Scope and accuracy

- Costs are local estimates based on Codex session records.
- Usage from other devices or applications is not included.
- DeepSeek Search and Fetch request fees are not included.
- Unsupported models or pricing periods are reported as unpriced instead of silently using another model's price.

## License

MIT
