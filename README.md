# ai-watch

[![CI](https://github.com/fmorency/ai-watch/actions/workflows/ci.yml/badge.svg)](https://github.com/fmorency/ai-watch/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Your Codex and Claude Code quotas, in one terminal.**

See how much of each subscription's usage allowance you've used, when it resets,
and which account still has room. Monitor one account or several, with a live
dashboard or a single snapshot.

```text
CODEX
  personal  alex@example.com  (pro)
    █████░░░░░░░░░  34.0%  5h window
    █████████░░░░░  62.0%  7d window
  work  alex@example.org  (team)
    ███████████░░░  78.0%  5h window

CLAUDE
  personal  alex@example.com  (max)
    ███░░░░░░░░░░░  23.0%  session
    █████████████░  91.0%  week (all models)
    ███░░░░░░░░░░░  18.0%  week (Sonnet)
```

Abbreviated synthetic example. The dashboard also shows reset dates, countdowns,
credits when available, and cache or error status.

- Codex and Claude Code, with named profiles for multiple accounts.
- Live provider quotas, including weekly and model-specific windows.
- Built-in refresh loop. Works from Bash, Zsh, Fish, and other shells; no `watch` command needed.
- Python standard library only at runtime. Linux, macOS, and WSL; Python 3.10+.
- JSON output, email hiding, ASCII mode, and an offline demo.

## Try it

```sh
git clone https://github.com/fmorency/ai-watch.git
cd ai-watch
./ai-watch --demo --once
./ai-watch
```

The demo works without either provider installed or any login. For live data,
first sign in through the [Codex CLI](https://developers.openai.com/codex/cli/)
or [Claude Code](https://code.claude.com/docs/en/authentication). Each provider
is optional. Subscription quotas require a ChatGPT or claude.ai login; API-key
billing, Bedrock, and other third-party providers are outside this tool's scope.

## Install

To make `ai-watch` and the one-shot `ai-usage` command available on your PATH:

```sh
pipx install git+https://github.com/fmorency/ai-watch.git
ai-watch
```

Alternatively, install from your checkout in a virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/ai-watch
```

Or keep using `./ai-watch`, `./ai-usage`, or `python3 -m ai_watch` from the
checkout without installing anything. There is no need to configure your shell.
If you already have Fish functions or aliases with these names, use the explicit
executable path or `command ai-watch` to bypass them.

## Usage

```sh
ai-watch                           # refresh every 60 seconds
ai-watch 30                        # refresh every 30 seconds
ai-watch --codex                    # only Codex
ai-watch --claude                   # only Claude
ai-watch --once                     # one snapshot
ai-usage                           # also one snapshot
ai-watch --json                     # one JSON snapshot
ai-watch --hide-email               # omit addresses from text and JSON
ai-watch --ascii --no-color         # plain ASCII output
ai-watch --demo                     # live demo using fake accounts
ai-watch --help
```

Press **Ctrl+C** to leave the dashboard and restore your terminal. Piped output
automatically prints one snapshot and exits. `--json` is also always one-shot.
Colors follow terminal detection and `NO_COLOR`; `--color` and `--no-color`
override that choice. Usage percentages mean **used**, not remaining.

Exit codes: `0` for a successful snapshot, `1` when any selected account is
unavailable or stale (or no profiles exist), `2` for invalid configuration or
arguments, and `130` for Ctrl+C. Live mode keeps refreshing through provider
errors. `--timeout SECONDS` defaults to 20: it bounds each Codex probe and each
Claude network operation. An in-flight Claude request can delay shutdown until
its network timeout.

## Accounts and configuration

Without a config file, ai-watch uses these existing directories:

| Provider | Default directory | Environment override |
| --- | --- | --- |
| Codex | `~/.codex` | `CODEX_HOME` |
| Claude | `~/.claude` | `CLAUDE_CONFIG_DIR` |

Each detected account is named `default`. Custom directories are explicit:
ai-watch does not search your disk for additional accounts.

For several accounts, copy [examples/config.json](examples/config.json) to
`~/.config/ai-watch/config.json` and keep the profiles you use:

```json
{
  "profiles": [
    {"provider": "codex", "name": "personal", "home": "~/.codex"},
    {"provider": "codex", "name": "work", "home": "~/.codex-work"},
    {"provider": "claude", "name": "personal", "home": "~/.claude"},
    {"provider": "claude", "name": "work", "home": "~/.claude-work"}
  ]
}
```

This file holds **paths and labels only**. Leave login tokens in the provider's
credential store. Sign in to each directory through its provider, for example:

```sh
env CODEX_HOME="$HOME/.codex-work" codex login
env CLAUDE_CONFIG_DIR="$HOME/.claude-work" claude
```

An existing config file replaces automatic discovery. Configuration precedence:
`--config PATH`, then `AI_WATCH_CONFIG`, then
`${XDG_CONFIG_HOME:-~/.config}/ai-watch/config.json`, then automatic discovery.
A missing explicit file is an error. `~` and environment variables expand in
paths; relative profile paths resolve from the config file's directory.

```sh
ai-watch --config ./my-profiles.json
ai-watch --profile work             # work accounts from both providers
ai-watch --profile codex:work        # only the Codex work account
ai-watch --profile personal --profile work
```

### Claude on macOS

Claude Code normally stores its login in the macOS Keychain. ai-watch reads
`<profile home>/.credentials.json` when available; otherwise the default profile
uses the `Claude Code-credentials` Keychain service. macOS may ask you to allow
Keychain access.

Custom profile Keychain names can vary with Claude Code versions. If the default
lookup fails, or you use a custom directory without a credentials file, locate
the matching entry in **Keychain Access** and set its exact service name:

```json
{
  "provider": "claude",
  "name": "work",
  "home": "~/.claude-work",
  "keychain_service": "the-exact-service-name-from-Keychain-Access"
}
```

Put this object inside your `profiles` array. ai-watch does not fall back to a
different profile's Keychain login. If Claude has no saved email for a profile,
the quota still appears under its configured name.

## How it works

Codex queries go through a short-lived `codex app-server` scoped to the selected
`CODEX_HOME`. ai-watch initializes it, reads the account and rate limits, then
closes it. Both primary and secondary quota windows are displayed, along with
additional buckets. See the [Codex app-server protocol](https://learn.chatgpt.com/docs/app-server).

Claude queries use the profile's existing OAuth login to read
`https://api.anthropic.com/api/oauth/usage`. This endpoint and the credential
format are provider internals and may change. ai-watch understands both the
structured `limits` response and older `five_hour` / `seven_day` fields. It does
not infer subscription quotas from local conversation history.

Claude results are cached for 120 seconds under
`${XDG_CACHE_HOME:-~/.cache}/ai-watch/`. The cache is locked across processes,
separated by profile and login, and contains quota data rather than tokens.
After errors, a previous result can remain visible for up to an hour, explicitly
marked **STALE**. HTTP 429 responses honor `Retry-After`. A shorter dashboard
refresh interval does not bypass the Claude cache.

ai-watch sends no prompts or model requests and has no telemetry. It reads
existing logins; Codex itself owns its authentication lifecycle. ai-watch does
not write credential files or refresh Claude tokens. If Claude rejects an
expired login, open Claude Code for that profile to renew it. Account emails
appear locally by default; use `--hide-email` or `--demo` when sharing your screen.
JSON contains normalized quotas and optional emails, not credentials or raw API
responses. Profile names remain visible with `--hide-email`.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| No profiles found | Sign in with a provider or add its directory to the config. |
| Codex cannot start | Ensure `codex` is on PATH in the shell running ai-watch. |
| Codex rejects a read or times out | Verify that profile's login and network; update the CLI or increase `--timeout`. |
| Claude login expired / HTTP 401 | Open Claude Code for the same profile and renew its login. |
| Claude HTTP 403 | The login may not have access to the usage endpoint. Sign in again through Claude Code. |
| Claude HTTP 429 | Let the displayed retry delay expire; frequent restarts will not bypass it. |
| Claude Keychain lookup fails | Unlock your Keychain and check the profile's `keychain_service`. |
| Cache cannot be accessed | Make sure `XDG_CACHE_HOME` points to a directory you can write. |
| Quotas look unchanged | Claude caches successful reads for two minutes; look for the cache age. |

Native Windows monitoring is not supported in this version; use WSL. macOS
Keychain access requires a local user session with access to the saved login.

## Development

```sh
python3 -m unittest discover -s tests -v
python3 -m ai_watch --demo --once
```

Tests use synthetic credentials, a fake Codex process, and mocked HTTP responses.
They make no provider requests. CI checks Python 3.10 and 3.14 on Linux and macOS,
including package installation and the CLI entry points. Live provider access
depends on your installed CLIs, subscription, and current provider behavior.

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidance.

## License

[MIT](LICENSE). Created by Félix C. Morency, extracted from a personal multi-account
usage monitor. An independent project, unaffiliated with OpenAI or Anthropic.
