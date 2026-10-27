# Discord Channel Monitor

A macOS floating monitor for Discord channels. Configure a group of channel IDs to display raw messages, embeds, authors, timestamps, and attachment links. An optional strategy channel can parse the project's T1/S1 trade embeds; group channels remain generic.

## Start

```bash
./run.sh
```

To install in a fresh environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app_entry.py
```

## Configuration

The example file is [`config.example.json`](config.example.json). The application stores its user configuration locally and never stores credentials in this repository.

- `channel_ids`: comma-, space-, or semicolon-separated channel IDs. Each configured channel is monitored through the same Gateway connection, with history and deduplication tracked per channel.
- `channel_id`: optional channel ID for the built-in T1/S1 embed parser. Leave it empty to monitor only generic channels.
- Generic channels render original message content, embeds, author, channel, timestamp, and attachment links. `filter_keyword` can filter message text and embed content.
- Historical backfill covers the latest three US trading sessions and fills missing ranges incrementally. REST calls are serialized through the shared local Discord request budget. Leave at least five minutes between independent manual pull rounds; this is a conservative local recommendation, not a Discord guarantee.
- Monitoring hours follow the configured market-hours setting. The Gateway uses bounded reconnect backoff and stops on authentication or rate-limit errors that require user attention.

## Project layout

| File | Purpose |
| --- | --- |
| `ui_window.py` | Floating window, channel settings, message cards, history replay, and scheduling |
| `discord_listener.py` | Gateway heartbeat, session recovery, and reconnect behavior |
| `history_store.py` | Per-channel SQLite history, deduplication, and incremental paging |
| `discord_request_budget.py` | Shared request serialization and cooldown handling |
| `market_timer.py` | Trading-session calendar and monitoring window |
| `tape_parser.py` | Optional T1/S1 embed parser |
| `simulate_signal.py` | Local demo event injection |
| `skills/discord-channel-monitor/SKILL.md` | Project maintenance and launch instructions |

## Development

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python app_entry.py --smoke-test
```

Tests use offline fixtures and temporary databases; they do not connect to Discord. To build the macOS app, install PyInstaller and run `./build_app.sh`.

## Local data

The default build keeps configuration, history, and diagnostics under the user's home directory. These paths are not part of the repository. The project `.gitignore` excludes local config, credentials, SQLite files, history exports, logs, and build artifacts.
