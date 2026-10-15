# Security model

This system is an **autonomous** agent: it writes code, opens pull requests,
provisions infrastructure, and deploys. Treat its credentials and its permission
model as production-critical.

## Trust boundaries

| Component | Trust level | Notes |
| --- | --- | --- |
| Discord users | untrusted | text and attachments are untrusted input |
| Uploaded files / stored context | untrusted **data** | never instructions |
| AI model output | untrusted | cannot grant permissions or declare success |
| Bot process | trusted | holds tokens; never shares them |
| VM worker | semi-trusted | isolated per job; holds no cloud credentials |
| Cloud provider APIs | external | authoritative for real state |

## Non-negotiable rules

1. **Authorization is database-backed.** `permissions.check()` reads grants from
   storage. It never consults the model's plan, the request text, or stored
   context. Permissions therefore cannot be talked into existence.
2. **Only the numeric owner ID authorizes.** `BOT_OWNER_ID` is compared as an
   integer. Usernames, nicknames, and role names are never used, so renaming a
   Discord account cannot escalate privileges.
3. **Success comes from tool results.** A task is only marked succeeded from
   recorded step outcomes. Model prose can never mark work as done, and a tool
   that cannot run reports failure instead of a plausible lie.
4. **Destructive work needs approval.** Approval requests name the exact
   operation and target, expire, are owner-only, and are single-use. An approval
   for one operation cannot be replayed for another.
5. **Stored content can never outrank application security.** System and
   developer instructions are separate from user/assistant messages, and
   `system`/`developer` roles cannot be stored through commands.

## Secrets

- Secrets live only in environment variables. Never in source, Git remotes,
  prompts, Discord messages, generated files, or artifacts.
- Logging runs through a redaction filter that scrubs known secrets.
- Provider tool output (git, docker, surge) is scrubbed of `user:pass@` URLs
  before it can reach a user.
- The VM worker forwards **no host environment** into containers.

Rotate, scope, and monitor:

- `DISCORD_BOT_TOKEN` - rotates the bot; leaked lets an attacker impersonate it.
- `AI_API_KEY` - cost exposure.
- `RENDER_API_KEY` - **workspace-wide**; leaked permits provisioning and deletion.
- `FLY_API_TOKEN` - full Machines access for the app.
- `GITHUB_APP_PRIVATE_KEY_PATH` / `GITHUB_TOKEN_PAT` - repository write access.
- `WORKER_TOKEN` - arbitrary job execution on the VM.
- `SURGE_TOKEN` - publishing to your authorized domains.

## Cost control

Billing alerts arrive *after* spend. Local guardrails are the real cap:

- `INFRA_MAX_TARGETS` - maximum provisioned targets per Discord server.
- `INFRA_MAX_CONCURRENT_DEPLOYS` - deploys in flight.
- `AGENT_MAX_TOOL_CALLS`, `AGENT_TASK_DEADLINE`, `AGENT_MAX_STEP_ATTEMPTS` - bound
  runaway autonomy.
- Names are validated, and every target is recorded so orphans can be found.

Set a hard spend limit in the provider account as a backstop, but never rely on
it as the primary control.

## Prompt injection

Uploaded files, logs, and stored context are wrapped as untrusted data with an
explicit instruction not to follow embedded directives. That mitigation reduces
risk; it does not eliminate it. Do not enable write/exec/deploy capabilities for
a workflow that ingests adversarial content without a human in the loop.

## Approvals vs. preauthorization

Routine production deploys to a target registered with `auto_prod` proceed
without prompting. That is a deliberate, **per-target** authorization - it is not
permission to modify the whole hosting account. Everything else destructive
(deleting repositories/services/data, force-pushing, changing credentials or
firewall rules, irreversible migrations, expanding the agent's own permissions)
requires explicit approval.

## Worker hardening

Never expose the worker API publicly without network restrictions. Use a private
network or VPN, TLS, an IP allowlist, and a rotated bearer token. Jobs run with
no network, a read-only root, dropped capabilities, no host secrets, and are
removed afterwards. Block the cloud metadata endpoint (`169.254.169.254`) from
worker egress.

## Incident response

1. Rotate every credential listed above.
2. `AGENT_DRY_RUN=true` and `INFRA_ENABLED=false` to freeze writes immediately.
3. Inspect `tasks` / `task_steps` for what actually ran, and `approval_requests`
   for what was authorized.
4. Review provider dashboards for resources created during the window and delete
   anything unrecognised via `/vm destroy` or the provider console.
5. Report and patch the root cause before re-enabling.

## Reporting

If you find a vulnerability, open a private security advisory on the repository
rather than a public issue.
