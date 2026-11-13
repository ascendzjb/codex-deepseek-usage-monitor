---
name: deepseek-usage-monitor
description: 启动、刷新、查看或停止 Codex 桌面智能合作伙伴旁的 DeepSeek 费用与 Token 实时监控玩偶。
---

# DeepSeek 用量监控

使用 `scripts/start_widget.ps1` 启动常驻桌面玩偶。玩偶具有透明轮廓、红色边框、今日消费徽章、轻微漂浮、眨眼、手臂摆动和呼吸状态灯。鼠标停留片刻会展开用量卡片；按住玩偶拖动会取消悬停展开，可跨显示器移动，并自动吸附到附近屏幕边缘。卡片使用“总、入、出”表示总量、输入和输出，数量采用“万、亿”等中文单位。面板每三秒读取一次本地 Codex 会话用量。

Startup 快捷方式运行 `scripts/watch_for_codex.ps1`。后台监听器会等待 ChatGPT/Codex 桌面进程，应用启动时拉起玩偶，意外退出时自动重启，应用关闭时停止玩偶。用户主动关闭后不会被立即拉起，直到下次启动 ChatGPT/Codex。仅登录 Windows 不会显示玩偶。

## 常用命令

- 启动或唤起玩偶：
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<skill>/scripts/start_widget.ps1"`
- 停止玩偶：
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<skill>/scripts/stop_widget.ps1"`
- 重新创建桌面和启动快捷方式：
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<skill>/scripts/create_shortcuts.ps1"`
- 不打开界面，直接输出一次 JSON 统计：
  `python "<skill>/scripts/monitor.py" --snapshot`
- 仅在用户要求时启用或禁用 Windows 登录启动：
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<skill>/scripts/enable_autostart.ps1"`
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<skill>/scripts/disable_autostart.ps1"`

- 停止 ChatGPT/Codex 进程监听器：
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<skill>/scripts/stop_watcher.ps1"`

请把 `<skill>` 替换为当前技能目录的绝对路径。

## 统计与限制

玩偶读取 `%USERPROFILE%\.codex\sessions`，按响应 ID 去重，并将增量索引保存在 `%LOCALAPPDATA%\DeepSeekUsageMonitor`，不会保存 API Key。程序按北京时间判断 DeepSeek 峰谷时段，包含官方 2026 年法定节假日，应用历史价格周期，并预计算费用以加快 SQL 汇总。

展开面板后点击“明细”可切换最近每日与每月用量。面板显示平均缓存命中率、高峰与低峰费用占比以及每次响应的平均费用。点击 `×` 会停止玩偶，可使用桌面快捷方式重新启动。

统计范围仅包含当前 Codex 安装产生的 DeepSeek 调用，不包含其他设备或应用。压缩的 `.jsonl.zst` 会话通过 Python `zstandard` 模块或本机 7-Zip 读取；运行期间已观察到的数据会保存在持久索引中。未知模型或价格日期会标记为“未计价”，不会静默使用 Flash 价格。

DeepSeek 调整价格或公布新的节假日安排时，请更新 `scripts/pricing.json`。保留历史价格周期，不要直接覆盖，以便新旧记录使用正确费率。

运行日志写入 `%LOCALAPPDATA%\DeepSeekUsageMonitor\widget.log`。
