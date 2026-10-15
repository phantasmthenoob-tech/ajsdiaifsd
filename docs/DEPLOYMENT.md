# Deployment (hybrid: Render + Linux VM)

Three roles, deliberately separated:

| Role | Runs on | Responsibility |
| --- | --- | --- |
| Orchestrator | Render (web service or worker) | Discord gateway, AI calls, context, commands, planning, approvals, GitHub/Render/Surge API calls |
| Execution worker | Dedicated Linux VM | git checkouts, builds, tests, docker, artifacts, long jobs |
| CI/CD + source of truth | GitHub | repos, branches, PRs, Actions |

The bot never shells into the VM; it calls the worker API. See
[VM_WORKER.md](VM_WORKER.md).

---

## 1. Render: the bot

### Option A - Render web service + uptime monitor (free tier)

Free web services sleep after ~15 minutes without **inbound** traffic, and a
Discord gateway connection is *outbound*, so it does not keep the service awake.
Bind the health endpoint and ping it:

```env
# Render injects PORT; the bot then serves /health on it
# (HEALTH_PORT is derived from PORT automatically)
```

Then point UptimeRobot / Better Stack / Healthchecks at
`https://<service>.onrender.com/health` every 5 minutes.

**Budget check:** one always-on service consumes roughly 730 of the **750 free
instance-hours per month**. A second always-on free service will not fit.

**Persistence warning:** Render free services have an **ephemeral filesystem**.
The SQLite database is lost on restart or redeploy. Options:

- Back up with `/exportcontext` and restore with `/importcontext`.
- Move to an external database before relying on long-lived state.

### Option B - paid always-on instance

Use a paid instance (or a background worker) for a durable process with a
persistent disk. Free background workers do not exist.

### Render settings

```
Build command:  pip install -r requirements.txt
Start command:  python bot.py
Health check:   /health
```

Environment: set every value from `env.example` in the Render dashboard. Never
commit secrets.

---

## 2. Linux VM: the execution worker

```bash
sudo useradd -r -m -s /bin/bash worker
sudo -u worker python -m venv /opt/discord-worker/.venv
sudo -u worker /opt/discord-worker/.venv/bin/pip install -r requirements.txt
```

Install Docker, add `worker` to the `docker` group, then run under `systemd`
(see [VM_WORKER.md](VM_WORKER.md)).

Set on the **bot**:

```env
WORKER_URL=http://<vm-private-ip>:8080
WORKER_TOKEN=<a long random string>
```

Do not expose the worker port publicly. Use a private network, VPN, or TLS
reverse proxy with an IP allowlist.

---

## 3. GitHub

Follow [GITHUB_APP_SETUP.md](GITHUB_APP_SETUP.md). Prefer the App; fall back to a
fine-grained PAT only when needed.

Set an allowlist so the agent can never wander:

```env
GITHUB_ALLOWED_REPOS=your-org/your-repo
```

---

## 4. First-run checklist

```env
DISCORD_BOT_TOKEN=...
BOT_OWNER_ID=...
AI_PROVIDER=...
AI_MODEL=...
AI_API_KEY=...
DATABASE_URL=sqlite:///data/bot.db

# Start safe: dry run, control plane disabled
INFRA_ENABLED=false
INFRA_DRY_RUN=true
AGENT_DRY_RUN=true
```

Verify, in order:

1. `/infrastructure status` - reports integrations and dry-run state.
2. `/repo inspect repository:your-org/your-repo` - GitHub works.
3. `/task run prompt:"inspect your-org/your-repo"` then `/task status` - planning
   and the orchestrator work.
4. `/infrastructure register kind:vm name:buildbox scope:vm_exec` - register the VM.
5. `/vm exec name:buildbox command:"python --version"` - the worker answers.

Only then relax the safety switches:

```env
AGENT_DRY_RUN=false     # allow real commits, PRs, deploys
INFRA_ENABLED=true      # enable the control plane
INFRA_DRY_RUN=false     # allow real provisioning/deploys
```

---

## 5. Migrations

Migrations run automatically on startup (forward-only). On a fresh database the
schema is created at version 3. Back up your database before upgrading.

---

## 6. Where a provider lacks a capability

Reported honestly rather than faked:

- **Render has no "upload source and deploy" API.** Deploys require either a
  connected Git repo or a pushed image, then a triggered deploy. The pipeline
  therefore uses git-push or docker-build paths.
- **Fly has no free allowance** (trial only), so that adapter stays a dry-run
  stub without a paid token.
- **Render logs use the workspace-wide `/logs` endpoint.** Response fields are
  parsed defensively and an unrecognised shape is reported rather than guessed.
- **Fly has no REST log endpoint**, so log retrieval reports that honestly.
- **VM logs** come from the worker's `/jobs/{id}/logs` endpoint and are also
  returned inline by `/vm exec`.
