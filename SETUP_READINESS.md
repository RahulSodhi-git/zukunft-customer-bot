# Setup readiness — 10 October 2026

Not ready for end-to-end customer or live trading tests.

Implemented and tested locally:
- Initial API setup followed by Settings inside the customer interface.
- Encrypted replacement credentials; no saved secrets returned to the browser.
- Replacement denied while running, positions open, heartbeat stale, or credential acknowledgement mismatched.
- Failed verification preserves the working connection; transactional guard rechecks after verification.
- Worker rotates a flat account automatically; preserves old exit monitoring when positions remain open.
- Snapshot credential versions prevent stale key generations reporting ready.
- Per-customer failure handling retains last-known positions and redacts exception messages.

Tests: 91 Python tests run, 90 passed and one old-dashboard test skipped.
Worker stress: 100 mocked customer accounts / 1,000 snapshots, plus failure isolation and rotation.
Backend: 500 mocked customers / 1,000 HTTP operations, 25 concurrent requests per batch,
with real RSA encryption/decryption but mocked database and exchange verification.
Additional worker API tests cover token auth, paid access, tenant scoping, stale versions,
heartbeat freshness, malformed input and pause/stop safety.
Frontend: 1,000 mock-DOM portal lifecycles plus Settings and replacement interaction tests.
These are NOT browser rendering, production load, real PostgreSQL concurrency, payment,
testnet order or live-money acceptance tests. They do not establish 100-customer VM capacity.

Read-only live checks:
- Backend /health returns ok=true, paypalConfigured=false, phoneOtpConfigured=false.
- Deployed backend /worker/accounts returns 404: customer integration is not deployed.
- Oracle zukunft-customers-01: worker inactive, disabled, EXECUTION_ENABLED=false.
- Existing tradebot VM/repository unchanged.

Before user end-to-end testing:
1. Configure the worker token/public encryption key on Render and deploy the tested integration.
2. Deploy the matching frontend and customer worker, initially read-only.
3. Configure PayPal for a sandbox payment test (normal customer onboarding needs this).
4. Confirm a paid test user's encrypted API handoff, version acknowledgement and snapshots.
5. Verify real UI rendering, database transactions and recovery on the deployed setup.
6. Complete Binance request budgeting/websocket work, worker ownership/leases, full trade
   metadata projection and supervised testnet order/recovery tests before enabling execution.

Never enable live execution merely because these mock stress tests passed.
