# LinkedIn draft

I'd like to share a software project from my portfolio: a self-hosted XAU/USD trade copier using Python and the cTrader Open API.

The application copies confirmed position openings from a master account to multiple follower accounts. It then synchronizes stop-loss, take-profit, partial closes, and full closes. A web dashboard displays accounts, position mappings, activity, and analytics based on imported broker data.

One technical detail I find particularly interesting is uncertain execution: when a broker response is missing, the order may still have executed. The engine therefore persists trading intents and reconciles them against broker state instead of simply sending the order again.

The architecture uses separate Python processes for the web server and copy engine, a shared SQLite database, and an asynchronous TLS/Protobuf transport. Tests with simulated broker responses cover duplicate events, position closes, and connection failures.

The project provides a practical starting point for discussing asynchronous processing, state management, and the limits of automated tests when working with external APIs.

Code and architecture: [Add GitHub repository link before publishing]

#Python #SoftwareEngineering #AsyncIO #Portfolio

---

Before publishing, add the repository link. Describe your personal development contribution, studies, or use of development tools according to the work you actually did; this draft does not assume those facts. Choose screenshots without account numbers, personal information, or credentials.
