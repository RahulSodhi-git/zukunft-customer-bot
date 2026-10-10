# Zukunft customer worker

Multi-tenant Oracle worker for Zukunft Trading. One process reconciles many paid customer accounts while keeping each account's credentials and runtime state isolated.

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

Each successful CI run on `main` triggers an atomic deployment. The previous five releases remain on the VM for rollback.

