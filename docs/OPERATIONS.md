# Setup and operation

Detailed instructions for the application. The interface uses English labels; `Slave` corresponds to the internal term `follower`.

[Back to the project overview](../README.md)

## Local setup

Python 3.12 is recommended; the Python tests have also run on Python 3.9. Automated browser testing uses Chromium by default. Firefox can also be used to access the application.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(32))"
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Enter the generated values as `SETUP_TOKEN` and `TOKEN_ENCRYPTION_KEY` in `.env`. Both `.env` and the database are git-ignored. Keep the encryption key permanently and protect it separately from database backups. A different key cannot decrypt existing authorizations.

### Register a cTrader application

1. Register an application in the [cTrader Open API portal](https://openapi.ctrader.com/) and obtain approval.
2. Set `CTRADER_CLIENT_ID` and `CTRADER_CLIENT_SECRET` in `.env`.
3. Register **`http://localhost:8080/ctrader/callback`** as the local redirect URI. For another origin, adjust the URI exactly. The portal determines whether your application can use a local HTTP origin; otherwise, use your own HTTPS test origin.
4. Set `APP_ORIGIN=http://localhost:8080` without a trailing slash.

Direct Python execution does not load `.env` automatically. In two terminals, load your trusted local `.env` and start the respective process:

```sh
set -a
source .env
set +a
.venv/bin/python server.py
```

Second terminal:

```sh
set -a
source .env
set +a
.venv/bin/python -m copier.engine
```

1. Open `http://localhost:8080` and enter your email, password of at least 12 characters, and setup token.
2. Choose **Connect cTrader** and authorize accounts with the `trading` permission. Repeat for both IDs if Pepperstone and FTMO use separate cTrader IDs.
3. Authorized accounts appear under **Settings → Available cTrader accounts**. The OAuth flow may return without `state`. In that case, the application requires explicit completion of the authorization just started in the signed-in workspace; it does not silently exchange the code. When returned, state is strictly validated. Authorizations belonging to another session are rejected.
4. Choose **Add account** and assign roles: optionally one **Overlord** source, one **Master** relay, then as many **Slave** destinations as your broker authorizes. FTMO capital must be classified as simulated. Enter the exact broker symbol name for XAU/USD, such as `XAUUSD`; suffixes are not guessed.
5. Wait for the accounts to synchronize. Under **Copy Engine**, set a separate lot size for each destination and enable each copy rule. In an Overlord chain, enable the Master relay to receive Overlord trades; enable each Slave to receive the relay's confirmed orders.
6. Choose **Enable new copies**. Only **new** confirmed entries are copied. Positions already open at startup are not opened retroactively at the next hop.

`ENGINE_TRADING_ENABLED=true` permits trading commands, but new copies still start paused in the database. `ENGINE_TRADING_ENABLED=false` disables **all** trading commands, including management of existing copied positions. Use the dashboard pause control for normal pauses.

## Exact copying behavior and limitations

- **Hedging accounts:** Netting and limited-risk accounts are rejected with an explanation because they do not reliably support individual position mappings or entry without SL. Overlord, relay master, and slave accounts must support hedging.
- **XAU/USD only:** The engine also validates the selected symbol's base and quote assets. Other master instruments are ignored.
- **Fixed lots:** Each copy hop has an independent fixed lot size. There is no application-level follower-count cap; cTrader request budgets, the number of authorized accounts, and server resources limit practical scale. Each new source position creates an individual copy. Volume increases within the same position ID do not add another fixed lot allocation; the maximum source volume becomes the reference for subsequent partial closes.
- **Volume:** API volume is expressed in hundredths of units. The broker supplies `lotSize`, minimum, maximum, and step values. Selected lots are rounded down to a valid step; no order is sent outside broker limits.
- **Pending orders:** New limit, stop and stop-limit entry orders are mirrored with fixed follower lots, prices, SL/TP and expiry. Changes and cancellations are synchronized, including while new entries are paused. Existing orders at startup are not copied retroactively. Master and follower fills reuse the same mapping; a second market entry is not sent. Brokers execute independently, so either account can fill first. If the follower fills first and the master subsequently cancels, the follower position is closed. Pausing blocks new submissions; pending orders already at the broker remain executable.
- **Entry protection:** If the master has no SL/TP, no artificial protection is added. Existing SL/TP distances are included in the market entry as relative distances where representable. Absolute SL/TP prices are not allowed on cTrader market orders; after the fill, the confirmed follower position is adjusted to the master's absolute prices. This creates an intermediate phase and does not guarantee atomic execution at identical prices.
- **SL/TP:** Removal is synchronized too. Follower trailing protection is disabled so master prices remain authoritative. Trailing changes reported by the master are mirrored. A rejected protection price triggers a safety stop for that follower; the engine does not invent a replacement stop.
- **Partial closes:** Target remaining volume = initial follower volume × current remaining master volume / reference master volume, rounded down to broker steps. A positive remainder below minimum volume is retained at the minimum until the master fully closes. A partial close below the minimum valid order size is skipped until a valid reduction becomes possible.
- **Manual follower changes:** An independently closed follower position is never reopened, and reduced follower volume is not replenished. After the first confirmed synchronization, independent follower SL/TP changes are detected. Each changed field remains manually managed for that position through later master changes; the other field continues following the master. Under Position synchronisation, **Restore master SL/TP** restores master management for both fields. Wait for outstanding broker commands to confirm before making manual changes. Master partial and full closes remain active. Follower changes never send orders to the master.
- **Pause:** Stops new entries only. Mapped positions continue receiving SL/TP and closing changes. Lot-size and follower-enablement changes apply to future entries; existing copies retain their original scaling.
- **Closing reconciliation:** In addition to execution events, a dedicated monitor checks the master roughly every three seconds, plus API time and rate-limit delays. Missing positions are treated as closed. Closing deals without a complete position event also trigger reconciliation. Confirmed master closes are propagated to reachable followers even if the master subsequently disconnects. A partial follower closing fill is not considered a full close.
- **Disconnection:** Existing mappings are reconciled after reconnection. Previously unmapped master positions created during an outage are not copied late. Network or broker outages can prevent uninterrupted execution.
- **Uncertain execution:** An unconfirmed order is not resent. A follower position found during reconciliation with the persistent `gimm:` label is linked to its original copy. Without verifiable broker state, the case remains `uncertain`; inspect broker positions and orders. There is deliberately no blind retry-trade control.
- **Request budgets:** Each connection allows 40 normal requests/s, five reserved emergency requests/s, and four historical requests/s, plus heartbeats. Emergency closes do not wait in the normal request queue. Different followers run concurrently; commands for a single copy remain ordered. Additional CPU does not guarantee faster broker execution.

## Safety stop and emergency control

The red **Emergency stop** requires confirmation and immediately persists a block on new copies. With an Overlord chain, the engine cancels pending orders on the relay master and **all assigned slaves**, including disabled destinations, and closes **all their positions, including manual trades and other instruments**; the Overlord source remains unchanged. Without an Overlord, it closes slaves and leaves the master unchanged. Only subsequent broker reconciliation confirms `flat`; sending a request does not establish successful closure. Late fills are also closed while the safety stop remains active. Repeated clicks do not start a second concurrent emergency task.

Automatic failure stops affect all positions and orders on the **affected copy target** and pause new copies globally. These defaults are configurable under **Copy Engine → Slave protection**:

| Rule | Default |
|---|---:|
| New entries per follower | At most 3 in 10 seconds |
| Concurrent copies/positions per follower | At most 5, including reserved entries |
| Signal age before sending | At most 5 seconds |
| Unconfirmed SL/TP discrepancy | Safety stop after 5 seconds |
| Position without SL | Safety stop after 30 seconds |
| Required broker/account connection lost or stalled | Safety stop after 10 seconds |

A rejected SL/TP command, uncertain trading confirmation, or internal synchronization error also triggers the safety stop. `POSITION_NOT_FOUND` on a close/amend first triggers broker reconciliation: a confirmed absent position is a normal completion and does not latch the safety stop. An open position despite that error remains a failure. Deadlines are persisted so restarts do not reset them. Trading intents and mappings prevent duplicate entries; the safety latch, signal age, and limits are checked again immediately before writing to the socket. Brokers can reject prices and closes. Confirmed rejections are retried at most three times, after which manual intervention is required. **Uncertain closes are not blindly repeated.** If execution cannot be established, the interface displays `uncertain` and requires inspection in cTrader.

Initial entry without SL remains possible, as on the master. The 30-second deadline is an additional protection rule: leaving the position without SL longer, or removing it for longer, closes the follower. No artificial SL is added. Safety stops may therefore diverge from the master and realize gains or losses. These values are technical defaults, not a monetary risk budget calculated for a challenge.

After an emergency, affected accounts are checked continuously. **Reset safety stop** requires fresh, healthy connections, confirmed flat accounts, and no unresolved execution uncertainty. New copies remain paused afterward and must be enabled separately.

The green **Connected** indicator requires a fresh website response, worker and protection heartbeats, current broker/position reconciliation for the Overlord source, relay master and all enabled accounts or accounts holding positions, confirmed protection prices, and no active errors or safety latches. Click it to inspect the individual checks. An open browser alone does not establish broker connectivity. Losing browser access does not trigger an emergency; the independent engine is intended to operate with the browser closed.

**Technical limitation:** A disconnected broker connection cannot execute closes. The request remains persisted until reconnection. Protection monitoring also stops if the computer or engine stops. Docker restart behavior does not replace independent broker-side protection. Existing broker SL/TP orders remain relevant. These mechanisms do not guarantee control over slippage, gaps, broker rejections, or FTMO daily/total loss rules. `ENGINE_TRADING_ENABLED=false` also prevents emergency closes and leaves them waiting.

## Loss limits: display only, per follower

Under **Trading accounts → Loss limits**, or on a follower card, configure starting capital, Max Loss, Max Daily Loss, and a warning threshold. Monetary amounts use **account currency**. Empty loss limits disable the respective display. The default turns the warning red when at most 20% of the budget remains. Negative headroom indicates a breached limit. These values do not automatically block or close positions; exceeding them does not change copying pause state or orders.

- Total floor = configured starting capital − Max Loss.
- Daily floor = start-of-day balance − Daily Loss.
- Remaining headroom = current broker equity − respective floor.
- Day boundary: **00:00 Europe/Berlin**, including daylight saving time. The bundled timezone-data dependency supports Docker systems without a system timezone database.

Open results and costs already included in balance/net P&L affect headroom through equity. Future closing costs are not already booked losses. The display uses a **fixed total limit**, not an automatically trailing drawdown. Account for deposits, withdrawals, corrections, and different challenge rules in your reference values.

The daily reference is persisted from the first balance snapshot of the day. Starting at 14:00 does **not** provide a historical midnight balance. The dashboard therefore displays the reference source and supports manually entering today's start-of-day balance. Verify it against your provider. Manual input applies only to the displayed current date; a new reference is created the next day. Missing or outdated equity marks the display as incomplete or stale.

## Local verification after an update

1. Stop the website and engine with Ctrl+C in both terminals. Keep `.env`, the encryption key, and the database.
2. Run `.venv/bin/pip install -r requirements.txt` in the project directory.
3. In terminal 1, load the trusted `.env` using `set -a`, `source .env`, and `set +a`, then start `.venv/bin/python server.py`. Repeat in terminal 2 with `.venv/bin/python -m copier.engine`. New tables and columns are added automatically.
4. Hard-refresh the browser, using Cmd+Shift+R on macOS. Reset any safety stop only after confirmed flat accounts; enable new copies separately.
5. On demo accounts only, open a new trade and close it manually on the master. Then test new trades closed by master SL and TP. Verify the follower is actually closed in cTrader.
6. For a new copy, change follower SL and then master TP: follower SL should remain unchanged while TP follows the master. Test **Restore master SL/TP**. Fully close the master afterward; the follower must close despite its manual SL.
7. Save loss limits and check the daily reference. To check the warning display, temporarily set the warning threshold to 100% and configure a floor above current equity. Verify a red warning appears without closing positions or pausing copies, then restore the actual values.
8. On demo accounts only, create follower positions and a pending order, then confirm the emergency stop. Verify directly in cTrader that followers are flat and pending orders are gone; the master remains unchanged. Only subsequent broker reconciliation should confirm `flat` in the dashboard.

Existing technical protection rules, including the deadline without SL, continue to apply. Loss displays do not change them. Check deadlines before manual testing to distinguish technical safety stops from loss warnings.

## Figures and analytics

- Real equity and simulated FTMO capital are displayed separately.
- Accounts in different currencies are **not** added together without FX data. Select the currency in the dashboard; automatic conversion is not implemented.
- Balance and unrealized net P&L come from the API. Equity = balance + reported net P&L. Future closing commission is not part of reported open net P&L.
- Equity samples begin after the first connection and are recorded at minute intervals. They are not retrospective historical equity.
- History imports start with 90 days and continue incrementally, using split windows and deal-ID deduplication. `All time` means available imported history, not the account's entire lifetime.
- The API returns at most the latest **5,000 deals**, **20,000 equity samples**, **200 mappings**, **100 commands**, and **100 activities** to the interface. Long-term analysis may be incomplete because of these limits. Older data remains in the database. Broader history requires server-side aggregation or pagination.
- Closed trades group closing deals by account and position ID. A position still open does not count as a completed trade. Net results use the broker's reported closing components.
- Maximum drawdown is based on cumulative realized trading P&L, rather than intraday equity or FTMO rule drawdown. Deposits do not count as trades or profit.
- Latency values measure **order roundtrips**, from sending to confirmation. They are not master-to-follower end-to-end latency measurements or benchmarks against Traders Connect.
- No invented skill score, automatic recommendations, or fabricated FTMO loss limits. FTMO rule monitoring and automatic challenge limits are not implemented. Technical protection rules can close followers early and do not replace FTMO loss-rule calculations.

## Docker deployment

```sh
docker compose up -d --build
docker compose logs -f app engine
```

Compose reads `.env`. Website and engine share the persistent `copier-data` volume. Port 8080 binds only to `127.0.0.1`. `docker compose down` preserves data; `down -v` deletes it.

### Domain and TLS

The supplied configuration uses `copier-engine.gimmholding.com`. Adapt it for your installation. These instructions do not configure DNS or perform deployment automatically.

1. Point the `copier-engine` A record to your server IP. Add AAAA only with working IPv6 connectivity.
2. Set `APP_ORIGIN=https://copier-engine.gimmholding.com` in `.env`.
3. Register the corresponding redirect URI ending in `/ctrader/callback` with cTrader.
4. Configure host Caddy using `Caddyfile`, or route your existing reverse proxy to `127.0.0.1:8080`. Ports 80/443 and outbound cTrader TCP 5035/HTTPS 443 are required.
5. HTTPS enables Secure cookies and HSTS automatically. The browser origin and `APP_ORIGIN` must match exactly.

For a proxy in another Docker container, `127.0.0.1` refers to that container itself. Use a shared network and the service address `app:8080` instead. The supplied Caddy configuration assumes a proxy running on the host.

## Operation and data protection

- `/health` checks the website and reports whether the engine has a recent heartbeat. Compose monitors both processes separately.
- Tokens and the client secret are not exposed to the browser or application logs. Callback query strings are omitted from website logs. The upstream proxy should also avoid logging OAuth code query strings.
- The setup token protects initial registration only; afterward, public registration is disabled. Back up the administrator password and encryption key. Email password reset and MFA are not implemented.
- Login limit: 10 attempts per IP in five minutes, stored in process memory. Behind a proxy, users share the proxy IP. This suits private access but should be replaced for public multi-user operation.
- Create consistent SQLite backups using the SQLite backup API or while processes are stopped. Back up the key separately and test restoration.
- Do not run two active servers with separate copies of the same trading database. Active/active execution is not supported.
- Accounts from the earlier demo version are archived in `legacy_demo_accounts` and never displayed as real broker accounts.
- No Notion integration is implemented. The existing external journal is unchanged.

## Tests

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -B -m unittest discover -s tests -v
node --check public/app.js
npm ci --ignore-scripts
npm run test:ui
.venv/bin/python -m playwright install chromium
.venv/bin/python tests/browser_smoke.py
```

Tests use isolated temporary databases and simulated cTrader responses. They send **no broker orders**. The DOM test covers setup, all seven pages, filters, CSV, password changes, persisted rules, activation, navigation, and logout. Node/jsdom is needed only for development tests and is absent from the Docker runtime image.

The Chromium test additionally checks desktop and mobile layouts and saves screenshots under git-ignored `artifacts/`. If Playwright Firefox does not start in your environment, installed Firefox can be checked through Selenium:

```sh
.venv/bin/python tests/firefox_native_smoke.py
```

Selenium may download Mozilla's WebDriver. Both approaches use fresh test profiles rather than your personal Firefox profile. Chromium screenshots are saved as `artifacts/chromium-*.png`. Optionally, run `TEST_BROWSER=firefox .venv/bin/python -B tests/browser_smoke.py` after installing Playwright Firefox.

Important engine cases include duplicate events, setting/removing SL/TP, changes during opening, closure during opening, partial/full closes while paused, independent follower closure, minimum volume, fills from old pending orders, netting rejection, uncertain timeouts, and mapping recovery.

SL/TP confirmations without `clientMsgId` are correlated only when account, position, and the fully confirmed SL/TP pair match unambiguously. `ORDER_ACCEPTED` alone is not a confirmation; a matching confirmed position state can establish the change. Transport tests cover these cases.

### Official protocol sources

The bundled `.proto` files come from [Spotware's official repository](https://github.com/spotware/openapi-proto-messages). Generated Python classes are included; a compiler is required only when changing the schemas:

```sh
.venv/bin/python scripts/generate_protocol.py
```

Further references: [Connection and heartbeat](https://help.ctrader.com/open-api/connection/), [OAuth and account authorization](https://help.ctrader.com/open-api/account-authentication/), [Order and SL/TP messages](https://help.ctrader.com/open-api/messages/), and [Symbols, volume, and positions](https://help.ctrader.com/open-api/model-messages/).

## English interface and removing accounts

The website uses English labels, validation errors, alerts, dialogs, activity messages, date formatting and CSV headers. The visible roles are **Overlord**, **Master**, and **Slave**. The internal `follower` API/database value is retained for compatibility with existing configurations; it is displayed as Slave. Existing technical activity messages are translated for display without changing historical records or user-defined account names.

Under **Trading accounts**, choose **Remove**, then confirm **Remove account**. This pauses new copies globally and disables new entries to that account while the engine checks the broker. Removal requires an online, synchronized account with no positions, no pending orders, no active copy mappings and no unresolved broker commands. A blocked removal shows its reason beside the account: resolve it in cTrader and retry Remove. No position is closed by the removal action itself.

Successful removal deletes the copier account configuration and its loss-limit configuration. Broker accounts, encrypted cTrader authorization, recorded trades, equity samples and closed copy history are retained. The account becomes available under **Add account**, where it can be added again. Removing a master releases the master slot so another authorized account can be assigned as master. New copies remain paused until explicitly enabled again. Schema migration runs automatically at process startup. Keep the existing `.env` and database when updating.

## GIMM server deployment

`compose.server.yaml` joins the existing `gimmholding-credentials_default` Caddy network.
The application has the unique proxy alias `trade-copier-app:8080` and exposes no host ports.
Use `docker compose -f compose.server.yaml up -d --build` from `/root/trade-copier`.
The existing proxy needs a `copier-engine.gimmholding.com` site routing to `trade-copier-app:8080`.
Keep `.env` and `data/` private and outside Git. The data directory must be writable by UID 10001.
Before migrating an existing database, stop its old engine and take a SQLite backup. Never run
both engines against separate copies of the same trading configuration. New copies should stay
paused during migration. Register `https://copier-engine.gimmholding.com/ctrader/callback` in the
cTrader application before reconnecting accounts on the public domain.

## Portfolio returns

Overview combines real and simulated equity within the selected currency and accounts. Profit is broker-reported net closing results plus the change in open net P&L from the reference snapshot. Deposits/withdrawals are excluded from profit. Percentage return divides this profit by combined equity at the reference, rather than averaging account percentages. All time begins with the first recorded equity after connection and excludes earlier imported trades. Today begins at midnight Europe/Berlin (including DST); a later connection uses its first available snapshot. Periods without a boundary snapshot use the first available snapshot; these are recorded-data returns. Overview totals aggregate all stored deals on the server, independently of the browser history limit.
