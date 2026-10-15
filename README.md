# Discord AI Chatbot

An AI-first Discord chatbot with personality, file-based coding, safe file
editing, and a persistent context system that **only** the configured owner can
control.

It is **not** a moderation bot. It chats naturally, generates real code files as
attachments, reads and edits uploaded source files, and keeps owner-managed
context isolated per server, channel, and user.

---

## Table of contents

- [Features](#features)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Discord Developer Portal setup](#discord-developer-portal-setup)
- [Inviting the bot](#inviting-the-bot)
- [Slash commands](#slash-commands)
- [Configuration reference](#configuration-reference)
- [Owner context system](#owner-context-system)
- [Context priority](#context-priority)
- [File handling](#file-handling)
- [Security model](#security-model)
- [Testing](#testing)
- [Deployment](#deployment)
- [Troubleshooting](#troubleshooting)

---

## Features

- **Natural conversation** with personality and humour, only when summoned.
- **Three activation paths**: a direct `@mention`, a reply to a bot message
  (when enabled), or a supported slash command.
- **Multi-server / multi-channel memory** with strict isolation - conversations
  never leak between servers, channels, or users.
- **Provider-independent AI backend** (any OpenAI-compatible API: OpenAI,
  OpenRouter, Groq, Together, DeepSeek, Ollama, LM Studio, Azure OpenAI, or a
  self-hosted endpoint) with retries and exponential backoff.
- **Code generation as real files**: single files are attached directly;
  multi-file projects are returned as a ZIP that preserves directory structure.
- **Read & edit uploads**: explain, fix, refactor, convert, review, or minimally
  patch uploaded files. The original is never overwritten.
- **Safe ZIP handling**: path-traversal, symlink, zip-bomb, depth, count, and
  size protection.
- **Owner-only context administration** enforced by numeric Discord ID inside
  every handler.
- **Persistent storage** in SQLite behind a repository interface designed to be
  swapped for PostgreSQL.

---

## Architecture

```
bot.py                     Application entry point and lifecycle
config.py                  Environment-based configuration (typed, validated)
logging_setup.py           Logging with automatic secret redaction
utils.py                   Small shared helpers (snowflake parsing, chunking)

discord_client.py          Intents, mention/reply detection, slash commands
engine.py                  Framework-independent request pipeline (chat/files)
health.py                  Tiny HTTP health endpoint for uptime monitoring

ai/
  provider.py              AIProvider interface + OpenAI-compatible client
  conversation.py          Priority layering, token budgeting, summarization

context/
  models.py                Scope / Role / Operation enums and dataclasses
  repository.py            SQLite CRUD for context, conversations, files
  service.py               Business rules: scopes, roles, export/import

files/
  validation.py            Extension/size/encoding/path validation
  reader.py                Safe decoding of uploaded text files
  archive.py               Safe ZIP read/write
  generator.py             Parse AI `### FILE:` blocks, build projects/ZIPs
  editor.py                Minimal search/replace patches, output naming

database/
  schema.py                SQL schema and forward-only migrations
  database.py              Async SQLite wrapper (aiosqlite)

sandbox/
  runner.py                Optional isolated execution + static validation

infra/                     Owner-controlled infrastructure control plane
  models.py                Target / CreateSpec / DeployPath and error types
  provider.py              InfraProvider interface + safe DryRunProvider
  render.py                Render service adapter
  fly.py                   Fly Machines adapter (real microVMs)
  policy.py                Name/quota/confirmation/concurrency guardrails
  registry.py              Tracked targets + active-target pointer
  pipeline.py              Git push and Docker build/push publishers
  service.py               Orchestration + provider factory

discord_infra.py           Owner-only `/vm` command group
discord_agent.py           Owner-only agent command surface

docs/
  GITHUB_APP_SETUP.md      GitHub App (and PAT fallback) setup
  VM_WORKER.md             VM worker deployment and hardening
  DEPLOYMENT.md            Render + VM hybrid deployment
  SECURITY.md              Threat model and guardrails

permissions/               Explicit, owner-granted scopes per resource
approvals/                 Owner-approved destructive operations

github/
  auth.py                  GitHub App (RS256) + PAT behind one interface
  client.py                REST client + dry-run implementation

agent/
  planner.py               Deterministic rule-based planning
  tools.py                 Tool registry with per-tool permissions
  orchestrator.py          Durable execution, retries, budgets, approvals
  store.py                 Durable task/step persistence + recovery
  targets.py               Trusted infra targets (VM/domain/service)
  runtime.py               Wiring

worker/
  protocol.py              Job wire format and limits
  client.py                Authenticated worker client
  server.py                Narrow API + isolated container runner

tests/                     Unit and integration tests (179 tests)
```

The Discord layer, AI provider, conversation management, context storage, file
processing, and permission system are fully independent modules.

---

## Requirements

- **Python 3.12+** (3.10+ works for the test suite; the project targets 3.12)
- A Discord application/bot token
- An API key for an OpenAI-compatible AI provider

---

## Quick start

```bash
# 1. Install dependencies
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Create your environment file from the template
cp env.example .env
#    ...then edit .env and fill in DISCORD_BOT_TOKEN, BOT_OWNER_ID, AI_API_KEY

# 3. Run the bot
python bot.py
```

> **Note on `.env.example`:** this repository ships the template as
> `env.example` (the platform's file tooling reserves `.env*` names). Copy it to
> `.env` as shown above. The format is identical.

The database is created and migrated automatically on first start.

---

## Discord Developer Portal setup

1. Go to <https://discord.com/developers/applications> and create an
   application, then open the **Bot** tab and **Reset Token** to get your token.
   Put it in `.env` as `DISCORD_BOT_TOKEN`.
2. Under **Bot → Privileged Gateway Intents**, enable **Message Content Intent**.
   This is required to read the text around a mention. **Do not** enable
   Presence or Server Members intents - the bot does not need them.
3. Get your own numeric user ID: enable **Developer Mode** in Discord
   (User Settings → Advanced), right-click your avatar, and choose
   **Copy User ID**. Put it in `.env` as `BOT_OWNER_ID`.
   This must be the *numeric* ID, never a username or nickname.
4. Slash commands are registered automatically on startup.

### Intents used

| Intent              | Why |
| ------------------- | --- |
| `guilds`            | resolve guild/channel context |
| `guild_messages`    | read guild messages |
| `dm_messages`       | support direct messages |
| `message_content`   | **privileged** - read mention text |

No other privileged intents are enabled.

---

## Inviting the bot

Use the OAuth2 URL generator (Scopes: `bot`, `applications.commands`) with the
**minimum** permissions:

- View Channels
- Send Messages
- Send Messages in Threads (optional)
- Embed Links
- Attach Files
- Read Message History
- Use Slash Commands (via `applications.commands` scope)

No administrator or moderation permissions are required.

---

## Slash commands

| Command          | Access | Purpose |
| ---------------- | ------ | ------- |
| `/ask`           | everyone | Ask the AI anything (`private:true` for an ephemeral reply) |
| `/code`          | everyone | Ask the AI to generate or edit code as files |
| `/setcontext`    | **owner** | Add / replace / remove stored context |
| `/listcontext`   | **owner** | List stored entries and their scopes |
| `/getcontext`    | **owner** | Show an entry by ID or label |
| `/deletecontext` | **owner** | Delete an entry by ID |
| `/clearcontext`  | **owner** | Clear a scope (requires `confirm:true`) |
| `/exportcontext` | **owner** | Export context as a JSON attachment |
| `/importcontext` | **owner** | Import context from a JSON attachment |
| `/vm create`     | **owner** | Create a compute target (`render` or `fly`) |
| `/vm list`       | **owner** | List targets and show the active one |
| `/vm swap`       | **owner** | Choose which target deploys go to |
| `/vm destroy`    | **owner** | Destroy a target (requires `confirm:true`) |
| `/vm deploy`     | **owner** | Publish code to the active target and deploy it |
| `/vm status`     | **owner** | Live status of a target |
| `/vm inspect`    | **owner** | Full target details |
| `/vm logs`       | **owner** | Retrieve logs for a VM worker job |
| `/vm exec`       | **owner** | Run a command on a registered VM worker |
| `/infrastructure status` | **owner** | Agent capabilities and integrations |
| `/infrastructure permissions` | **owner** | List granted scopes |
| `/infrastructure targets` | **owner** | List trusted targets |
| `/infrastructure register` | **owner** | Register a target and grant a scope |
| `/infrastructure unregister` | **owner** | Remove a trusted target |
| `/repo list`     | **owner** | List authorized repositories |
| `/repo inspect`  | **owner** | Inspect a repository |
| `/repo actions`  | **owner** | Run an authorized GitHub Actions workflow |
| `/repo pr`       | **owner** | Open a pull request |
| `/render services` | **owner** | List tracked Render services |
| `/render logs`   | **owner** | Recent service logs |
| `/render deploy` | **owner** | Trigger a deployment |
| `/surge deploy`  | **owner** | Generate and publish a static site |
| `/task run`      | **owner** | Start an autonomous task |
| `/task status`   | **owner** | Show a task and its steps |
| `/task cancel`   | **owner** | Cancel a running task |
| `/approve`       | **owner** | Approve or deny a pending operation |

Mentions also work for everyday chat, e.g. `@Bot explain how a binary search
tree works`.

---

## Configuration reference

All settings are environment variables. See `env.example` for a documented
template.

### Required

| Variable            | Description |
| ------------------- | ----------- |
| `DISCORD_BOT_TOKEN` | Bot token |
| `BOT_OWNER_ID`      | Numeric Discord ID of the sole owner |
| `AI_PROVIDER`       | `openai`, `openrouter`, `groq`, `together`, `deepseek`, `ollama`, `lmstudio`, `azure-openai`, or `openai_compatible` |
| `AI_MODEL`          | Model name (e.g. `gpt-4o-mini`) |
| `AI_API_KEY`        | Provider API key |
| `AI_BASE_URL`       | Optional custom endpoint (required for `openai_compatible` / `azure-openai`) |

### Optional

`AI_TEMPERATURE`, `AI_MAX_TOKENS`, `AI_TIMEOUT`, `AI_MAX_RETRIES`,
`AI_API_VERSION`, `ENABLE_REPLY_CONVERSATIONS`, `STRICT_MENTION`,
`DISCORD_GUILD_IDS`, `DATABASE_URL`, `MAX_HISTORY_MESSAGES`,
`MAX_CONTEXT_TOKENS`, `SUMMARIZE_AFTER_MESSAGES`, `CONTEXT_RETENTION_DAYS`,
`MAX_ATTACHMENT_BYTES`, `MAX_ARCHIVE_*`, `MAX_GENERATED_*`,
`ENABLE_SANDBOX_EXECUTION`, `SANDBOX_*`, `LOG_LEVEL`, `LOG_PRIVATE_CONTENT`.

`LOG_PRIVATE_CONTENT` is off by default so message text is never written to logs.

### Example: self-hosted model

```env
AI_PROVIDER=openai_compatible
AI_BASE_URL=http://localhost:11434/v1
AI_MODEL=llama3.1
AI_API_KEY=not-needed
```

---

## Owner context system

Authorization compares the invoking user's **numeric Discord ID** against
`BOT_OWNER_ID`. Usernames, nicknames, display names, and role names are never
considered, so renaming a Discord account cannot affect authorization. The check
runs **inside** each command handler, not only via Discord's command
permissions.

### Scopes and roles

- `scope:all` - global, applies everywhere (no target).
- `scope:user` - applies only to the target user.
- `scope:channel` - applies only in the target channel.
- `scope:server` - applies only in the current server.

`role:user` stores a note or fact (treated as user-provided, not verified
truth). `role:assistant` inserts the text as an assistant message. System and
developer roles are **reserved** and cannot be stored through commands.

### Examples

```
/setcontext scope:all      role:user      operation:append content:"The server uses Python 3.13."
/setcontext scope:user     target:@ExampleUser role:user operation:append content:"Prefers concise explanations."
/setcontext scope:channel  target:#coding role:user operation:append content:"This channel is for Python projects."
/setcontext scope:all      role:assistant operation:append content:"We previously agreed to use PostgreSQL."
```

### Operations

- `append` - add a new entry. If a `label` already exists in that scope, it is
  updated in place (upsert) so a topic stays canonical.
- `replace` - with a `label`, overwrite that label; without one, replace the
  whole scope/target bucket.
- `remove` - delete by `label`, or clear the whole scope/target bucket.

Destructive bulk operations require confirmation (`/clearcontext confirm:true`).

---

## Context priority

Higher items always outrank lower ones, and stored content can never override
application security:

1. Safety / application restrictions
2. Developer-defined bot behaviour
3. Owner-defined global context
4. Applicable server and channel context
5. Applicable user context
6. Recent conversation history
7. The current message and its attachments

When the token budget is exceeded, the assembler trims in reverse priority order
(oldest history first), and the current message is always retained.

---

## File handling

### Generated files

The model replies with file blocks:

```
### FILE: relative/path.ext
```lang
<complete file contents>
```
```

The bot parses these into real files, keeps explanations short, and attaches
them. A single file is attached directly; multiple files become a
`generated_project.zip` that preserves structure.

### Uploaded files

- Supported: Python, JS/TS, Java/Kotlin/C/C++/C#/Rust/Go, HTML/CSS, JSON/JSONC,
  YAML, TOML, XML, INI, Markdown, `.txt/.log/.csv/.sql/.sh/.ps1/.bat`,
  `.properties`, `.env.example`, common build/config files, and ZIP archives of
  text projects.
- Edits prefer **minimal patches** and never overwrite the original - output is
  named `<name>_edited.<ext>`.
- Ambiguous patches are rejected rather than guessed.

---

## Infrastructure control plane (`/vm`)

Optional, **owner-only**, and **disabled + dry-run by default**. It lets the owner
provision compute and push generated code to it.

### What "VM" means here

- `render` → a Render **service** (managed web/worker/cron/static). Render is not
  a VM provider.
- `fly` → a real Fly.io **microVM**. Fly has no free allowance, so it stays a
  dry-run stub unless you configure a paid token.

### Deploy paths

- **git** - writes files to a scratch checkout, commits, and `git push`es to
  `INFRA_GIT_REMOTE`/`INFRA_GIT_BRANCH`, then triggers a deploy. The bot never
  force-pushes, so a diverged branch fails loudly instead of destroying history.
- **docker** - builds an image from the generated files (a `Dockerfile` is
  required) and pushes it to `DOCKER_REGISTRY`, then deploys that image.

Neither path embeds credentials: git relies on the host's credential helper and
Docker on the host's registry login. Tool output is scrubbed of `user:pass@`
URLs before being surfaced.

### Enabling it

All three switches must be set before anything real happens:

```env
INFRA_ENABLED=true
INFRA_DRY_RUN=false
RENDER_API_KEY=...        # and/or FLY_API_TOKEN + FLY_APP_NAME
```

With the defaults, `/vm` still responds but every provider is a dry-run stub and
no code is pushed anywhere.

### Guardrails

- Names validated against `INFRA_NAME_PATTERN`.
- At most `INFRA_MAX_TARGETS` targets per Discord server.
- At most `INFRA_MAX_CONCURRENT_DEPLOYS` deploys in flight.
- `/vm destroy` requires `confirm:true`.
- Targets are isolated per server, exactly like context.

### Hosting the bot on Render free + an uptime bot

Render free **web services** sleep after ~15 min without inbound traffic, and a
Discord gateway connection is *outbound*, so it does not keep the service awake.
That is what the health endpoint is for: set `HEALTH_PORT` (Render injects
`$PORT`) and point an uptime monitor at `/health`. One always-on free service
consumes roughly **730 of the 750 free instance-hours/month**, so a second
always-on free service will not both fit.

> Caveat: free services have an **ephemeral filesystem**, so the SQLite database
> is lost on restart/redeploy. Back it up with `/exportcontext`, or move to an
> external datastore.

## Autonomous agent

Owner-only and **dry-run by default** (`AGENT_DRY_RUN=true`). The agent plans a
task, checks permissions, executes tools, retries recoverable failures, verifies
the final state, and reports only what the tools actually returned.

### Permission model

Permissions are explicit grants stored in the database and are **never** inferred
from the model's plan or from stored context. Scopes: `ai_conversation`,
`repo_read`, `repo_write`, `vm_inspect`, `vm_exec`, `deploy_dev`, `deploy_prod`,
`infra_admin`, `context_admin`. Grants are per repository, VM, service, or
domain. The configured owner is implicitly authorized for everything, since the
owner is the one who grants.

### Approval

Destructive or privileged steps pause the task (`awaiting_approval`) and create an
approval request naming the **exact operation and target**. Only `BOT_OWNER_ID`
can decide it, requests expire after `AGENT_APPROVAL_TTL`, and each approval is
single-use and bound to the operation it authorized. Routine production deploys
to a target registered with `auto_prod` proceed without prompting.

### Durable execution

Task and step state live in the database. On startup, tasks left `running` by a
previous process are reset to `pending` so they can be resumed - a restart can
never silently drop a job or make one look successful. Every task is bounded by
a deadline (`AGENT_TASK_DEADLINE`), a tool-call budget (`AGENT_MAX_TOOL_CALLS`)
and a per-step retry limit (`AGENT_MAX_STEP_ATTEMPTS`), and cancellation is
always available through `/task cancel`.

### Integrations

| Integration | Detail |
| --- | --- |
| GitHub | GitHub App installation tokens (preferred) or a fine-grained PAT (fallback), behind one interface. See [docs/GITHUB_APP_SETUP.md](docs/GITHUB_APP_SETUP.md). |
| VM worker | Narrow token-authenticated HTTP API running jobs in isolated containers. The bot never gets a shell on the VM. See [docs/VM_WORKER.md](docs/VM_WORKER.md). |
| Render | Inspect services, list deploys, read logs, restart, and deploy via the control plane. |
| Surge | Publish static sites, restricted to an explicit domain allowlist. |

### Agent tools

Every capability is a registered tool that declares the scope it needs, whether
it mutates state, and whether it is destructive. The orchestrator checks
permission *before* invoking a tool and takes success from the returned result,
never from model prose.

| Tool | Scope | Purpose |
| --- | --- | --- |
| `repo.get` | `repo_read` | Repository metadata |
| `repo.branch` | `repo_write` | Create a working branch |
| `repo.commit` | `repo_write` | Commit files to a branch |
| `repo.pr` | `repo_write` | Open a pull request |
| `actions.dispatch` | `repo_write` | Run an authorized GitHub Actions workflow |
| `vm.inspect` | `vm_inspect` | List registered VM targets |
| `vm.logs` | `vm_inspect` | Read logs from a worker job |
| `worker.run` | `vm_exec` | Run a command on the VM worker |
| `render.services` | `deploy_dev` | List tracked services |
| `render.logs` | `deploy_dev` | Recent service logs |
| `render.status` | `deploy_dev` | Recent deploy records |
| `render.restart` | `deploy_dev` | Routine, targeted restart |
| `render.deploy` | `deploy_dev` → `deploy_prod` | Trigger a deploy; the scope is upgraded from the database when the target has an `auto_prod` grant |
| `surge.publish` | `deploy_dev` | Publish a static site (destructive → needs approval) |
| `task.note`, `task.verify` | - | Record an answer; verify final state |

### Current limitations

Implemented honestly, not stubbed into silence:

- **Render logs go through the workspace-wide `/logs` endpoint.** The response
  shape is parsed defensively: an unrecognised payload is reported as an error
  rather than presented as if it were real output. Historically the schema has
  changed, so expect to adjust the field names when connecting a live account.
- **Fly exposes no REST log endpoint**, so `fetch_logs` reports that plainly
  instead of inventing output. Use `fly logs` on the host for Fly Machines.
- **VM job logs come from the worker's own `/jobs/{id}/logs` endpoint** and are
  also returned inline by `/vm exec`.
- **The worker runs `docker`.** If Docker is absent the worker reports that
  clearly instead of pretending a job ran.
- **Changing a repository still needs a source of file contents.** `repo.commit`
  refuses to commit without an explicit diff rather than guessing.

## Security model

- Secrets live only in environment variables; they are redacted from logs and
  never placed in prompts, generated files, or replies.
- Uploaded files and stored context are treated as **untrusted data**. The
  system prompt explicitly forbids following instructions embedded inside them.
- The AI never receives unrelated private conversations.
- `system` / `developer` roles are application-only and cannot be set by users.
- Executing uploaded or generated code on the host is **never** done. By default
  only static syntax validation runs (`ENABLE_SANDBOX_EXECUTION=false`). If
  execution is enabled with `SANDBOX_BACKEND=docker`, code runs in a container
  with no network, limited memory/CPU/PIDs, a read-only root, and no host
  secrets - and the bot still states clearly when nothing was executed.
- Owner-only commands log denied attempts without logging context contents.

---

## Testing

```bash
pip install -r requirements-dev.txt
pytest -q
```

Coverage includes: config parsing, owner authorization (including the real
in-handler check), context scope isolation and persistence, file validation,
ZIP traversal/symlink/bomb rejection, generation parsing, minimal patching,
conversation priority/token budgeting, provider retries/rate limits, and
prompt-injection framing.

---

## Deployment

The hybrid model (bot on Render, execution on a Linux VM, GitHub as source of
truth) is documented in **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**. The
threat model, secret handling, cost guardrails and incident response are in
**[docs/SECURITY.md](docs/SECURITY.md)**.

### systemd (example)

```ini
[Unit]
Description=Discord AI Chatbot
After=network-online.target

[Service]
WorkingDirectory=/opt/discord-ai-bot
EnvironmentFile=/opt/discord-ai-bot/.env
ExecStart=/opt/discord-ai-bot/.venv/bin/python bot.py
Restart=on-failure
User=discordbot

[Install]
WantedBy=multi-user.target
```

### Docker (example)

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "bot.py"]
```

Pass secrets with `--env-file .env`. Persist the SQLite file by mounting a
volume at the `DATABASE_URL` directory (e.g. `/app/data`).

---

## Troubleshooting

| Symptom | Fix |
| ------- | --- |
| Bot ignores mentions | Enable **Message Content Intent** in the portal |
| "Could not read that file" | The file type is binary or unsupported |
| Reply-based chat not working | Set `ENABLE_REPLY_CONVERSATIONS=true` |
| Slash commands missing | Global sync can take up to an hour; set `DISCORD_GUILD_IDS` for instant sync |
| "restricted to the bot owner" | `BOT_OWNER_ID` must be your numeric Discord user ID |
| AI provider errors | Check `AI_API_KEY`, `AI_BASE_URL`, and `AI_MODEL`; transient 429/5xx errors are retried automatically |
| Nothing was executed | Expected: sandbox execution is off by default (static checks only) |

---

## License

MPL-2.0 (see `LICENSE`).
