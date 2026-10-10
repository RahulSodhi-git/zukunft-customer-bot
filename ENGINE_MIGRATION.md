# Original engine migration

Source: RahulSodhi-git/tradebot at c616d80702414b529c6144bbb2e8f668bf7ab2ee.
Original repository and original Oracle VM are unchanged.

Copied engine, strategy selection/backtesting, indicators, exchange adapter,
OI integration, exit ladders, entry guards, risk rules and original regression tests.
No original credentials, trade history, position state, dashboard or recorder copied.

Adaptations: separate customer subprocess/working directory; no shared .env;
customer-local history and risk settings; encrypted credentials decrypted by the
supervisor and sent through a pipe; start/pause/stop controls; entry permission
expires after 90 seconds without a successful control-plane update. Exit monitoring
continues when entries are paused. Manual/untracked positions block initial start.
Credential rotation pauses entries and waits for the old account to be flat before
replacing its process. The backend rejects replacement while running or while
positions are open and requires a fresh matching credential-version snapshot.

Original current configuration: minimum capital 100 USDT; 10% allocation per trade;
10 slots (7 regular + 3 special); normal leverage 3–5x; special leverage 5–7x;
normal floor -20% margin, special floor -10%; target +100% margin; strategy-specific
ladders, ADX/OI/BTC regime gates and persistent re-entry cooldowns. Gainer/news
new entries remain disabled by the copied configuration. These rules do not
guarantee profit.

Production is NOT trading-ready. EXECUTION_ENABLED must remain false until:
- Backend/worker encrypted credential configuration and deployment verified.
- Dedicated testnet integration verifies orders, native stops, fills and recovery.
- Shared Binance request budget/backoff and live price websockets are implemented.
- Worker ownership/leases prevent duplicate engines on concurrent deployments.
- Position/closed-trade projections include verified stop and exchange P/L metadata.
- Exit monitoring and account recovery procedures pass supervised testing.

The 1 GB VM live admission limit is one customer for the initial pilot, not 100.
No live orders were authorized or placed by this migration.

Local verification (2026-10-10, Python 3.10): 88 unit/regression tests run,
87 passed, 1 old-dashboard-only test skipped. Exchange calls are mocked;
this is not a testnet or live execution acceptance test.
