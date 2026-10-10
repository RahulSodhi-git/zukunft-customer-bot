# Zukunft customer worker

Oracle worker for Zukunft Trading. A supervisor reconciles customer accounts; each admitted trading engine runs in a separate process and state directory. The initial 1 GB VM pilot admits only one live engine. This is logical account separation, not separate OS/container security boundaries.

## Safety model

- Binance and Coinalyze credentials are RSA-OAEP encrypted by the Render control plane.
- Only the Oracle worker holds the private decryption key.
- Decrypted credentials remain in memory and are never written to disk or logs.
- The initial production rollout is read-only: `EXECUTION_ENABLED=false`.
- Stop is blocked while positions are open; pause keeps exchange monitoring active.
- One customer failure cannot stop other customer runtimes.

## Local test

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .
python -m unittest discover -s tests -v
```

## Production

Run `sudo bash deploy/install-host.sh` once on the Oracle VM, configure `/etc/zukunft-worker.env`, and add the repository deployment secrets described below.

Required GitHub environment secrets:

- `PROD_HOST`
- `PROD_USER`
- `PROD_SSH_KEY`
- `PROD_KNOWN_HOSTS`

Required Render environment variables:

- `WORKER_TOKEN`: a long random value shared only with the Oracle service.
- `WORKER_PUBLIC_KEY_B64`: base64 of `/etc/zukunft-worker/public.pem` on one line.

Deployment is disabled until repository variable `PRODUCTION_DEPLOY_ENABLED=true` and the production secrets are configured. Successful main-branch push CI then triggers deployment. Execution remains separately locked by `EXECUTION_ENABLED=false` on Oracle. The current deployment script shares a virtual environment between releases: switching the source symlink is not a complete dependency rollback.

See [ENGINE_MIGRATION.md](ENGINE_MIGRATION.md) for migrated rules and the outstanding live-trading acceptance checks. Use Python 3.10–3.13; CI runs Python 3.10 to match Ubuntu 22.04.

