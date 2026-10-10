# Setup readiness — 10 October 2026

The new API-only interface and customer backend are deployed. Not ready for live trading.

Implemented and tested locally:
- Separate API-only setup and trading pages; capital is configured only on the trading page.
- Explicit Continue button after both verified, encrypted connections exist, independent of capital.
- Saved credentials are masked placeholders; later replacement is available in Settings.
- Clean form-level errors and account-ID validation prevent confusing login IDs with API keys.
- Encrypted replacement credentials; no saved secrets returned to the browser.
- Replacement denied while running, positions open, heartbeat stale, or credential acknowledgement mismatched.
- Failed verification preserves the working connection; transactional guard rechecks after verification.
- Worker rotates a flat account automatically; preserves old exit monitoring when positions remain open.
- Snapshot credential versions prevent stale key generations reporting ready.
- Per-customer failure handling retains last-known positions and redacts exception messages.

Tests: 94 Python tests run, 93 passed and one old-dashboard test skipped.
Worker stress: 100 mocked customer accounts / 1,000 snapshots, plus failure isolation and rotation.
Backend: 500 mocked customers / 1,000 HTTP operations, 25 concurrent requests per batch,
with real RSA encryption/decryption but mocked database and exchange verification.
Additional worker API tests cover token auth, paid access, tenant scoping, stale versions,
heartbeat freshness, malformed input and pause/stop safety.
Frontend: 1,000 mock-DOM portal lifecycles plus Settings and replacement interaction tests.
Browser rendering and interactions were also checked on desktop and phone-sized views
using a clearly labelled local fake API: independent saves, validation, masking,
Continue navigation, Settings and cancellation. Published assets were checked, and
the protected live page correctly redirects an unauthenticated browser to login.
These are NOT production load, real PostgreSQL concurrency, payment,
testnet order or live-money acceptance tests. They do not establish 100-customer VM capacity.

Read-only live checks:
- Backend /health returns ok=true, paypalConfigured=false, phoneOtpConfigured=false.
- Vercel production now serves the new API-only portal and separate trading page.
- Render production revision 62fafa1 runs encrypted Oracle account loading with the previous
  Binance ban deadline preserved; its production /health response confirms that revision.
- Backend dependency audit reports zero known vulnerabilities after updating proxy-addr.
- Authenticated /worker/accounts check from the new Oracle VM returns HTTP 200.
- Oracle zukunft-customers-01: worker inactive, disabled, EXECUTION_ENABLED=false.
- The separate read-only zukunft-verifier service is active/enabled on Oracle release
  8e6ce41, with no restart failures after correcting its encryption directory access.
- Backend CI passed; queue tests cover worker authentication, RSA encryption, one-time
  claiming, preserved cooldown, completion cleanup and timeout cleanup.
- Real-key verification remains untested until the current ban expires and the user
  adds the customer server IP to the Binance key's trusted-IP list.
- Connection UX now uses Connect Binance: a successful signed account-info request
  connects the account, saves encrypted keys and displays a timestamped wallet/available
  balance summary. Readable accounts with canTrade=false may connect; bot start is denied.
  No separate Verify button. Cooldown retry-date parsing was fixed and regression-tested.
- Existing tradebot VM/repository unchanged.

Before user end-to-end testing:
1. Complete a real paid test user's API verification after the recorded cooldown
   expires (2026-10-10 20:50:43 UTC). Verification now uses an authenticated encrypted
   job queue consumed by the separate read-only Oracle verifier, not Render egress.
   Add customer VM outbound IP 137.23.35.138 to the Binance key's trusted IP list.
   The verifier checks only time/account endpoints, serializes requests, persists
   418/429 cooldown across restarts, and never loads the trading engine.
   Temporary unverified ciphertext is deleted after completion, timeout or expiry.
   This does NOT yet provide shared request budgeting for trading engine processes
   or shared market WebSockets; live trading remains disabled pending that work.
2. Enable only read-only customer-worker monitoring after inspecting its release/configuration.
3. Configure PayPal for a sandbox payment test (normal customer onboarding needs this).
4. Confirm real encrypted API handoff, worker version acknowledgement and exchange snapshots.
5. Verify database transactions and recovery on the deployed setup with the paid test account.
6. Complete Binance request budgeting/websocket work, worker ownership/leases, full trade
   metadata projection and supervised testnet order/recovery tests before enabling execution.

Never enable live execution merely because these mock stress tests passed.
