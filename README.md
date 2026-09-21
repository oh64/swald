# Swald

A small multi-protocol chat bridge. Swald connects different messaging platforms together, forwarding messages between them in real time. Each platform is handled by a dedicated library running in its own process.

## Supported protocols

- **Discord** -> via [pycord](https://github.com/Pycord-Development/pycord/)
- **Fluxer** -> via [fluxer.py](https://github.com/Fluxer-py/fluxer.py)
- **Spacebar** -> via [pycord](https://github.com/Pycord-Development/pycord/) but more patched
- **Stoat** -> via [stoat.py](https://github.com/MCausc78/stoat.py)

# How to use?

You can directly use the hosted version of the bot here :
(it command prefix is `!swald `)

[Stoat](https://go.sovrahi.com/swald-stoat)

[Fluxer](https://go.sovrahi.com/swald-fluxer)

[Spacebar (official instance)](https://go.sovrahi.com/swald-spacebar)

[Discord](https://go.sovrahi.com/swald-discord)



## Requirements

- Python ≥ 3.11
- [uv](https://docs.astral.sh/uv/)

## Installation

```sh
git clone https://git.sovrahi.com/oh64/swald.git
cd swald
uv sync
```

## Configuration

Copy the example config and edit it:

```sh
cp src/swald/swald.yml.example src/swald/swald.yml
```

Then edit `src/swald/swald.yml`. At minimum you need one connection:

```yaml
connections:
  my-discord:
    type: discord
    token: "your-discord-bot-token"
```

See `swald.yml.example` for all available options and protocol-specific fields.

By default Swald uses an SQLite database (`swald.db`). To use PostgreSQL instead, uncomment the `postgres` line in the config:

```yaml
postgres: "postgresql://user:pass@localhost:5432/swald"
```

## Usage

```sh
uv run swald
```

Logs are printed to stdout by default. Enable file logging with `logToFile: true` in the config (logs are written to `logs/`).

## Commands

Commands are available inside any connected channel using the configured prefix (default `!bridge `).
