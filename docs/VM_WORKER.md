# VM execution worker

The Discord bot **never** gets a shell on the VM. It calls a narrow,
token-authenticated HTTP API that runs jobs inside isolated containers and
returns status, logs, and cancellation.

## Why a separate worker

- Keeps long builds, tests, Docker, and dependency installs off the bot process.
- Gives the bot a small, auditable surface instead of remote shell access.
- Enforces resource limits and cleanup in one place.

## API

All endpoints except `/health` require `Authorization: Bearer <WORKER_TOKEN>`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | liveness (unauthenticated, for probes) |
| `POST` | `/jobs` | submit a job → `202 {job_id, status}` |
| `GET` | `/jobs/{id}` | status/exit code/output |
| `GET` | `/jobs/{id}/logs` | `{stdout, stderr}` |
| `POST` | `/jobs/{id}/cancel` | cancel a queued or running job |

Job body:

```json
{
  "command": ["pytest", "-q"],
  "image": "python:3.12-slim",
  "files": { "src/main.py": "print(1)\n" },
  "env": {},
  "limits": { "timeout_seconds": 120, "memory_mb": 512, "cpus": 1.0, "pids": 128, "network": false }
}
```

## Container isolation

Each job runs with:

- `--network none` (unless `limits.network` is explicitly true)
- `--memory` / `--cpus` / `--pids-limit` from the request limits
- `--read-only` root filesystem plus a small `tmpfs` at `/tmp`
- `--cap-drop ALL`, `--security-opt no-new-privileges`, `--user 65534:65534`
- **no host environment forwarded** — the container never receives the Discord
  token, AI key, GitHub credentials, or any other secret
- the workspace removed after the job, and the container `docker kill`ed on
  cancellation or timeout

## Running it

```bash
python -m worker.server  # or embed start_worker(port, token=...) in your own runner
```

In practice, run it under a supervisor (`systemd`, Docker Compose, or a
Pterodactyl-style container) on the VM:

```ini
[Unit]
Description=Discord agent VM worker
After=docker.service

[Service]
WorkingDirectory=/opt/discord-worker
ExecStart=/opt/discord-worker/.venv/bin/python -m worker.server
Restart=always
User=worker
Environment=WORKER_TOKEN=replace-me

[Install]
WantedBy=multi-user.target
```

Configure the bot:

```env
WORKER_URL=http://10.0.0.5:8080
WORKER_TOKEN=replace-me
```

## Hardening checklist

- [ ] **Never** expose the worker port to the public internet. Bind it to a
      private network/VPN address and firewall it; the bearer token is a second
      layer, not the first.
- [ ] Use TLS (a reverse proxy) if any traffic crosses an untrusted network.
- [ ] Rotate `WORKER_TOKEN` regularly, and give the bot its own token.
- [ ] Run the worker as an unprivileged user that can only talk to the Docker
      socket (never `--privileged`, never host networking).
- [ ] Keep images patched; pin digests for anything long-lived.
- [ ] Do **not** mount the host filesystem or the cloud metadata endpoint into
      jobs. Block `169.254.169.254` at the network level.
- [ ] Cap concurrency (`max_concurrent`) and job history.
- [ ] Treat worker output as untrusted text when showing it in Discord.

## Limitations

- Job state is held **in memory**: restarting the worker loses job history.
  Persist it if you need durable job records.
- Docker is required. Without it the worker reports that clearly instead of
  pretending a job ran.
