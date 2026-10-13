# LinkedIn project post

I built a self-hosted XAU/USD trade copier with Python and the cTrader Open API.

It mirrors confirmed master-account entries to as many configured follower accounts as broker and server capacity supports. An optional Overlord → relay master → follower chain lets the Overlord's trades pass through a confirmed intermediary account before they fan out. The engine keeps stop-loss, take-profit, partial closes, and full closes in sync. A private dashboard shows broker-backed account state, copy mappings, activity, and trading analytics. An iOS companion app wraps the dashboard in a SwiftUI app shell.

One engineering problem stood out: a timeout does not tell you whether a broker executed an order. The engine records trading intent before sending commands and reconciles uncertain outcomes against broker state instead of blindly retrying them. The web server and asynchronous copy engine run as separate processes and coordinate through SQLite.

The project includes session and CSRF protections, encrypted cTrader tokens, an emergency stop, and configurable safety rules. Automated tests use simulated broker responses; they cannot replace validation with broker demo accounts. The software can send real trading commands, so the repository makes no claims about returns or trading performance.

Code and architecture: https://github.com/TristanGimm/Trade-Copier

#Python #SoftwareEngineering #AsyncIO #FinTech #iOS

Before sharing screenshots, use a clean demo workspace and remove account numbers, balances, names, OAuth details, and any other personal or financial information.
