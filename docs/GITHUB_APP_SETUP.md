# GitHub authentication setup

Two methods are supported through one interface. Configure **one**; switch later
by changing `GITHUB_AUTH` without touching anything else.

## Preferred: GitHub App

A GitHub App scoped to selected repositories is preferred because it issues
**short-lived installation access tokens** rather than a long-lived secret.

### 1. Create the App

1. Go to <https://github.com/settings/apps> → **New GitHub App**.
2. Set a name and homepage URL (any URL is fine).
3. Uncheck **Active** under Webhook (this bot does not need webhooks).
4. Under **Repository permissions**, grant the minimum you need:

   | Permission | Access | Enables |
   | --- | --- | --- |
   | Contents | Read & write | reading files, branches, commits |
   | Pull requests | Read & write | opening PRs |
   | Metadata | Read-only | required baseline |
   | Actions | Read-only | inspecting workflow runs |
   | Issues | Read-only | reading issue context (optional) |

5. Under **Where can this GitHub App be installed?**, choose *Only on this
   account* unless you need wider reach.
6. Create the App, then click **Generate a private key** and download the `.pem`.
7. Note the **App ID** shown at the top of the App settings.

### 2. Install it on selected repositories

1. On the App page, click **Install App** → choose the account.
2. Select **Only select repositories** and pick exactly the repos the agent may
   touch. This is your first line of defence; `GITHUB_ALLOWED_REPOS` is the second.
3. After installing, the URL looks like
   `https://github.com/settings/installations/12345678` — that number is the
   **installation ID**.

### 3. Configure the bot

```env
GITHUB_AUTH=app
GITHUB_APP_ID=123456
GITHUB_APP_INSTALLATION_ID=12345678
GITHUB_APP_PRIVATE_KEY_PATH=/etc/discord-bot/github-app.pem
GITHUB_ALLOWED_REPOS=your-org/your-repo,another-org/another-repo
```

```bash
sudo install -m 600 -o discordbot github-app.pem /etc/discord-bot/github-app.pem
```

The private key file must be readable by the bot process and **nothing else**.
It is read once at startup and never written to logs, prompts, or artifacts.

### 4. Verify

- `/infrastructure status` shows `github: app`.
- `/repo inspect repository:your-org/your-repo` returns metadata and branches.

Tokens are minted on demand, cached in memory, and refreshed a couple of minutes
before expiry, so a long-running bot does not need restarts.

## Fallback: fine-grained personal access token

Use this when an App is unsuitable (for example, a repository you cannot install
the App on).

1. Go to <https://github.com/settings/personal-access-tokens/new>.
2. Set an expiry.
3. Under **Repository access**, choose *Only select repositories*.
4. Grant the same minimal permissions as above (Contents, Pull requests,
   Metadata, optionally Actions).
5. Copy the token into the environment:

```env
GITHUB_AUTH=pat
GITHUB_TOKEN_PAT=github_pat_...
GITHUB_ALLOWED_REPOS=your-org/your-repo
```

PATs are long-lived: rotate them, and never place them in a repository, a
`.env` committed to git, a Discord message, or a generated file.

## Switching methods

Change `GITHUB_AUTH` and restart. Nothing else in the application changes,
because both sit behind the same `GitHubAuth` interface.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `GitHub rejected the App credentials (HTTP 401)` | Wrong App ID, wrong installation ID, or the private key does not match the App |
| `Could not reach GitHub to mint an installation token` | Outbound network blocked from the host |
| `GitHub API error (HTTP 404)` | The App is not installed on that repository |
| `GitHub is not configured` | Neither method is configured, **and** `AGENT_DRY_RUN=false`. In dry-run a simulated client is used instead |
