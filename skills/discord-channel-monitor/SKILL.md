---
name: discord-channel-monitor
description: Start and maintain the local Discord multi-channel monitor. When asked to monitor channels or open the monitor, launch the floating UI by default; configured channel groups display raw messages, with an optional T1/S1 parser for one channel.
---

# Discord Channel Monitor

Project source: this repository. Follow the shared work rules at `/Users/rouen/.agents/WORKFLOW.md` and read the [project README](../../README.md) for setup and operational details.

## Start monitoring

- When the user asks to start monitoring, open the monitor, or view live channel messages, run `./run.sh` from the project root. This opens the floating UI and starts monitoring the saved configuration.
- If an instance is already running, running `./run.sh` raises that instance instead of creating another listener.
- The window initially shows the full panel, then collapses to its edge after four seconds. Run `./run.sh` again or click the edge to expand it.
- For source edits, documentation work, and offline troubleshooting, follow the requested task without starting a live Discord connection.

## Channel behavior

- `channel_ids` configures any number of generic channels. They share one Gateway connection and use per-channel history cursors, storage, and deduplication.
- The optional `channel_id` field selects the built-in T1/S1 embed parser. Leave it blank for a generic-only setup. A channel listed in both fields uses the specialized parser.
- Reuse cached history and fetch only missing ranges. Keep REST requests serialized through the shared request budget and honor server cooldowns.
- History display is limited to the latest three US trading sessions; cached records outside the display window remain on disk.

## Maintenance

- Keep user credentials, real message exports, local databases, and generated app bundles out of the repository.
- Preserve the configured channel-group behavior, Gateway recovery safeguards, and per-channel history isolation when changing the listener.
- Run offline checks only when requested or required by the task. Never use tests as a reason to start the live monitor.
