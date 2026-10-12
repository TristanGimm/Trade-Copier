# GIMM · Trade Copier

A self-hosted XAU/USD trade copier built with Python, the cTrader Open API, and a web dashboard. It copies confirmed position openings from one master account to up to ten follower accounts, then synchronizes stop-loss, take-profit, partial closes, and full closes.

The project connects a practical trading use case with asynchronous communication, persistent state, authentication, and error handling. A central engineering question is how to keep positions synchronized and traceable when connections fail or broker confirmations do not arrive.

The application sends real trading commands. Without authorized accounts, data views remain empty; no sample trades or invented returns are displayed. Initial execution checks should use broker demo accounts.

## Purpose and features

Managing several accounts manually requires repeating entries and subsequent changes. The copier handles that transfer and displays the state of each copy. Each follower has a configurable fixed lot size. Trading decisions originate on the master account; the copier does not implement an entry strategy.

Supported scope: one master, up to ten followers, and XAU/USD on hedging accounts. Account configuration includes Pepperstone and FTMO. FTMO capital is classified as simulated. The English interface calls followers **Slave**, while the internal API and database retain `follower`.

- cTrader OAuth and account discovery across multiple cTrader IDs.
- Confirmed market entries plus mirrored limit, stop and stop-limit orders, with their protection, amendments and cancellations.
- SL/TP synchronization, proportional partial closes, and full closes.
- Detection of manual follower changes and restoration of master SL/TP management.
- Combined portfolio equity and trading return (amount and percentage) since connection, with Berlin-day and period filters; position mappings, connection diagnostics, and activity log.
- Trade journal, equity recording, analytics, and CSV export based on broker data.
- Configurable technical protection rules and an emergency stop.
- Per-follower loss displays that do not trigger trading commands.

## Architecture

```mermaid
flowchart TD
    B[Browser: HTML, CSS, JavaScript] <-->|HTTP / HTTPS · JSON| W[Web server: server.py]
    W <-->|Configuration, sessions, status| D[(SQLite · WAL)]
    E[Copy engine: asyncio] <-->|Positions, mappings, commands| D
    E <-->|TLS / TCP · Protobuf · Port 5035| C[cTrader Open API: Live / Demo]
    W -->|OAuth code exchange over HTTPS| O[cTrader OAuth]
    E -->|Token refresh over HTTPS| O
```

**The web server and copy engine run as separate processes.** The web server serves the interface and JSON API, checks sessions, and stores settings and user actions. Only the engine executes trading commands. Closing the browser therefore does not stop synchronization.

The engine processes broker events through an `asyncio.Queue`. Concurrent tasks handle account initialization, position reconciliation, history imports, heartbeats, and protection monitoring. Commands for the same copy run in order; different followers can be processed concurrently. A local file lock prevents a second engine process from using the same database file.

Both processes exchange state through SQLite, without an additional message broker. SQLite uses WAL and short-lived database connections. This keeps a private single-instance deployment manageable; distributed operation with multiple active engines is not supported.

### Components

| File / directory | Responsibility |
|---|---|
| [`server.py`](server.py) | Python standard-library HTTP server, JSON API, login, CSRF/origin checks, and account configuration |
| [`public/`](public/) | HTML, CSS, and JavaScript without a frontend framework; analytics calculations and presentation |
| [`copier/engine.py`](copier/engine.py) | Account initialization, broker events, position synchronization, reconciliation, and history imports |
| [`copier/transport.py`](copier/transport.py) | TLS connections, Protobuf framing, request correlation, heartbeats, and request budgets |
| [`copier/store.py`](copier/store.py) | Schema, migrations, data access, and filtered dashboard state |
| [`copier/oauth.py`](copier/oauth.py) | Account authorization, code exchange, token encryption, and token management |
| [`copier/safety.py`](copier/safety.py) | Entry checks, persistent protection deadlines, and emergency liquidation |
| [`copier/risk.py`](copier/risk.py) | Loss displays and daily references using the `Europe/Berlin` day boundary |
| [`copier/protocol/`](copier/protocol/) | Spotware Protobuf schemas and generated Python classes |
| [`tests/`](tests/) | Tests with simulated broker responses, DOM tests, and browser tests |

### Lifecycle of a copy

1. The engine authenticates accounts and loads positions, symbol metadata, and broker volume rules. Master positions already open at startup are not copied retroactively.
2. With copying enabled, a newly confirmed master position creates a persistent mapping for each enabled follower. A unique constraint prevents duplicate mappings of the same master position to the same follower.
3. The configured fixed lot size is converted to a valid broker volume. Before sending, the engine checks pause state, safety latches, signal age, and limits.
4. The trading intent is recorded in `commands` and sent through the appropriate live or demo connection. Execution requires a matching broker confirmation.
5. The confirmed follower position is linked to its mapping. Subsequent master changes determine protection prices and target remaining volume. Manual SL/TP overrides are tracked separately for each field.
6. After a restart or disconnection, the engine reconciles stored mappings with broker state. Persistent `gimm:` labels help identify entries that have already executed.

**A timeout does not prove that an order failed.** The broker may already have executed it. Such commands remain stored as `uncertain` and are not blindly resent. Further trading commands for that copy wait for a verifiable state. This reduces the risk of duplicate orders but may require manual investigation.

### Data model

| Tables | Purpose |
|---|---|
| `accounts`, `discovered`, `oauth_tokens` | Account configuration, authorized broker accounts, and encrypted tokens |
| `positions`, `master_positions` | Last known broker state and master reference state |
| `mappings`, `commands` | Master-to-follower mappings and trading intents |
| `deals`, `equity_samples` | Imported executions and equity recorded after connection |
| `settings`, `engine_status`, `audit` | Configuration, process status, and activity records |
| `safety_timers`, `liquidations`, `safety_actions` | Protection deadlines and emergency actions that survive restarts |
| `risk_limits`, `risk_days` | Loss displays and daily references |
| `users`, `sessions`, `oauth_pending` | Administrator access, sessions, and pending authorizations |

### Technical choices

Python with `asyncio` suits the largely network-bound workload. The custom TLS transport uses the bundled Spotware Protobuf classes without an additional cTrader SDK. Each connection has budgets of 40 normal, five emergency, and four historical requests per second, plus heartbeats.

The frontend requires no build step. Node.js and jsdom are used only for development tests. Docker Compose runs the web server and engine separately with shared persistent storage. A Caddy reverse proxy can provide HTTPS.

Access is designed for a private administrator. Passwords use PBKDF2-SHA256 with 600,000 iterations. Sessions are stored server-side, with HttpOnly and SameSite cookies. Write requests check CSRF and origin. OAuth tokens are encrypted with Fernet; the encryption key is kept outside the database in environment configuration.

## Behavior and limitations

- **Pause:** Stops new copies. Existing mappings still receive protection and closing changes. `ENGINE_TRADING_ENABLED=false` disables every trading command, including emergency closes.
- **Execution:** New limit, stop and stop-limit entry orders are mirrored before execution, with fixed follower lots and SL/TP. Existing pending orders at startup are not copied retroactively. Each broker executes independently; prices and fill timing can differ. Fills reuse the pending mapping without a second market entry.
- **Reconnection:** Existing mappings are reconciled. Unmapped master positions created during an outage are not copied later.
- **Emergency stop:** Closes every position and cancels pending orders on assigned followers, including manual trades and other instruments. The master remains unchanged. An unavailable broker connection prevents immediate liquidation.
- **Protection rules:** May close follower positions independently of the master, for example when a position lacks an SL for too long. Defaults and reset conditions are documented in the operating guide.
- **Loss displays:** Show remaining headroom in account currency. They do not automatically block or close positions and do not implement complete prop-challenge rule monitoring.
- **Analytics:** Maximum drawdown uses cumulative realized trading P&L. History imports start with the last 90 days; equity samples begin after connection. There is no automatic currency conversion. API output limits may restrict long-term analysis.
- **Deployment:** One local engine instance, without distributed failover. Direct engine execution requires a Unix system such as Linux or macOS because it uses `fcntl`; Docker is an option on Windows.

## Running locally

Python 3.12 is the Docker image target. An approved cTrader Open API application and authorized accounts are also required.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(32))"
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set the generated values as `SETUP_TOKEN` and `TOKEN_ENCRYPTION_KEY`, then provide `CTRADER_CLIENT_ID` and `CTRADER_CLIENT_SECRET`. Register `http://localhost:8080/ctrader/callback` as the local redirect URI in your cTrader application.

In two terminals, load your trusted local `.env` and start one process in each:

```sh
set -a
source .env
set +a
.venv/bin/python server.py
```

```sh
set -a
source .env
set +a
.venv/bin/python -m copier.engine
```

Open `http://localhost:8080`, create the workspace, authorize cTrader, and assign accounts. New copies start paused and must be enabled in the dashboard.

The [operating guide](docs/OPERATIONS.md) covers OAuth edge cases, protection rules, exact copying behavior, demo checks, account removal, backups, and Docker deployment. `compose.server.yaml` contains an installation-specific domain and an existing external proxy network; use `compose.yaml` for an independent setup.

## Tests

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -B -m unittest discover -s tests -v
npm ci --ignore-scripts
npm run test:ui
```

Python tests cover duplicate events, partial and full closes, manual overrides, safety stops, uncertain execution, and mapping recovery using isolated databases and simulated broker responses. The jsdom test checks setup, navigation, settings, and CSV export.

An additional browser test uses Playwright:

```sh
.venv/bin/python -m playwright install chromium
.venv/bin/python tests/browser_smoke.py
```

Automated tests do not replace execution checks with your own broker demo accounts. They cannot fully establish behavior under slippage, broker rejections, or outages.

## Repository and presentation

`.env`, `data/`, local environments, and test artifacts are git-ignored. Credentials, databases, and screenshots containing account details should stay outside a public repository. Spotware protocol files have their [own license](copier/protocol/LICENSE); the generated classes are not an independently designed protocol implementation.

A draft project introduction is available in [docs/LINKEDIN.md](docs/LINKEDIN.md).
