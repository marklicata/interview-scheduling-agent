# Deployment Guide

The scheduling agent runs as a **Custom Engine Agent** in Microsoft Teams and M365 Copilot.
Recruiters open it directly or @mention it in any Teams or Copilot chat.

---

## How the pieces connect

```
Recruiter (Teams / M365 Copilot)
  → Azure Bot Service               — routes messages by bot ID; mandatory for any Teams bot
  → App Service or Container Apps   — runs the Python agent (POST /api/messages)
  → Anthropic / AI Foundry          — the LLM backend (configured by env var)
  → Microsoft Graph                 — recruiter's calendar and mail (delegated OAuth)
```

The Teams app package (manifest + icons) is metadata only — no code, no secrets.
All configuration lives on Azure and never travels through Teams.

---

## Deployment tiers

| Tier | Model provider | Recommended compute | Azure Bot needed? |
|---|---|---|---|
| Local dev | `copilot` (GitHub Copilot CLI) | Your machine | No |
| Prototype | `anthropic` (direct API key) | Container Apps | Yes |
| Production | `foundry` (Azure AI Foundry) | Container Apps | Yes |

For local development the agent can be tested without Teams using the Agents Playground
(`SCHEDULING_PROVIDER=memory`), which bypasses Azure Bot Service entirely.

**Why Container Apps over App Service?** The agent only runs when recruiters are actively
using it. Container Apps scales to zero when idle — you pay nothing between conversations.
An App Service plan runs 24/7 regardless of load. For a bot workload, Container Apps is
the better fit at both prototype and production scale.

---

## Local development

No Azure setup required. Uses your existing GitHub Copilot login.

```bash
cd agent
pip install -e ".[copilot,m365,dev]"

# SCHEDULING_MODEL_PROVIDER defaults to "copilot" — no env var needed
SCHEDULING_PROVIDER=memory python -m scheduling_agent.hosts.m365.app
```

Open the [Teams Agents Playground](https://aka.ms/teams-agents-playground) and point it
at `http://localhost:3978/api/messages` to test without a real Teams tenant.

---

## Prototype deployment (Anthropic API)

### Prerequisites

- Azure subscription
- Azure CLI (`az login`)
- An Anthropic API key
- A Teams tenant where you can sideload apps

### Step 1 — Deploy the app to Azure App Service

```bash
# Create a resource group and App Service plan (Basic B1 is sufficient for a prototype)
az group create --name rg-scheduling-agent --location eastus
az appservice plan create --name plan-scheduling-agent --resource-group rg-scheduling-agent --sku B1 --is-linux

# Create the web app (Python 3.12)
az webapp create --name hr-scheduling-agent --resource-group rg-scheduling-agent --plan plan-scheduling-agent --runtime "PYTHON:3.12"
```

Deploy your code using zip deploy, GitHub Actions, or VS Code — whichever fits your workflow.
The app must serve `POST /api/messages` on port 8000 (App Service default) or configure
`WEBSITES_PORT` to match the port in `app.py` (default 3978).

### Step 2 — Configure application settings

```bash
az webapp config appsettings set \
  --name <your-app-name> \
  --resource-group rg-scheduling-agent \
  --settings \
    SCHEDULING_MODEL_PROVIDER=anthropic \
    ANTHROPIC_API_KEY=<your-api-key> \
    SCHEDULING_PROVIDER=graph \
    SCHEDULING_ORGANIZER=<recruiting-mailbox-upn-or-blank>
```

### Step 3 — Register an Azure Bot

```bash
# Create a bot registration (messaging endpoint = your App Service URL)
az bot create --name intervew-scheduling-bot --resource-group rg-scheduling-agent --kind registration --endpoint https://<your-app-name>.azurewebsites.net/api/messages

# Note the app (client) ID and create a client secret
az ad app credential reset --id <bot-app-id>
```

Add the bot credentials to your app settings:

```bash
az webapp config appsettings set \
  --name <your-app-name> \
  --resource-group rg-scheduling-agent \
  --settings \
    CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID=<bot-app-id> \
    CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET=<bot-secret> \
    CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID=<tenant-id>
```

Enable the **Microsoft Teams** channel on the bot in the Azure portal.

### Step 4 — Configure the Graph OAuth connection

The agent reads the recruiter's calendar on their behalf using delegated OAuth.

1. In the bot's Azure portal page, go to **Configuration > OAuth Connection Settings**
2. Add a new connection named exactly `graph` (the name in `env.sample`)
3. Provider: **Azure Active Directory v2**
4. Scopes: `Calendars.ReadWrite Calendars.ReadWrite.Shared Mail.Send User.ReadBasic.All MailboxSettings.Read`

Add the connection name to your app settings:

```bash
az webapp config appsettings set \
  --name <your-app-name> \
  --resource-group rg-scheduling-agent \
  --settings \
    AGENTAPPLICATION__USERAUTHORIZATION__HANDLERS__GRAPH__SETTINGS__AZUREBOTOAUTHCONNECTIONNAME=graph
```

### Step 5 — Upload to Teams

1. Fill in `appPackage/manifest.json`: replace `${{TEAMS_APP_ID}}`, `${{BOT_ID}}`, and
   `${{BOT_DOMAIN}}` with your actual values (Teams Toolkit does this automatically if you
   use it; otherwise edit manually).
2. Zip the `appPackage/` folder: `manifest.json` + both icon files.
3. In Teams: **Apps > Manage your apps > Upload an app > Upload a custom app**.
4. Select your zip. The agent appears in Teams and in M365 Copilot (@mention).

---

## Container deployment (recommended compute for all Azure tiers)

A `Dockerfile` and `.dockerignore` are included at the repo root. Build the image once
and deploy it to either App Service (container mode) or Azure Container Apps. The steps
below use Container Apps.

### Build and push to Azure Container Registry

```bash
# Create a container registry (once per subscription is fine)
az acr create --name <your-registry> --resource-group rg-scheduling-agent \
  --sku Basic --admin-enabled true

# Build and push (run from the repo root)
az acr build --registry <your-registry> \
  --image scheduling-agent:latest .
```

### Deploy to Azure Container Apps

```bash
# Create a Container Apps environment (once per resource group)
az containerapp env create \
  --name env-scheduling-agent \
  --resource-group rg-scheduling-agent \
  --location eastus

# Deploy the container
az containerapp create \
  --name scheduling-agent \
  --resource-group rg-scheduling-agent \
  --environment env-scheduling-agent \
  --image <your-registry>.azurecr.io/scheduling-agent:latest \
  --registry-server <your-registry>.azurecr.io \
  --target-port 8000 \
  --ingress external \
  --min-replicas 0 \
  --max-replicas 10 \
  --env-vars \
      SCHEDULING_MODEL_PROVIDER=anthropic \
      ANTHROPIC_API_KEY=secretref:anthropic-api-key \
      SCHEDULING_PROVIDER=graph \
      CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID=<bot-app-id> \
      CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET=secretref:bot-client-secret \
      CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID=<tenant-id> \
      AGENTAPPLICATION__USERAUTHORIZATION__HANDLERS__GRAPH__SETTINGS__AZUREBOTOAUTHCONNECTIONNAME=graph \
  --secrets \
      anthropic-api-key=<your-api-key> \
      bot-client-secret=<your-bot-secret>
```

`--min-replicas 0` enables scale-to-zero. The first message after a cold start takes a
few extra seconds while the container wakes; subsequent messages in the same conversation
are unaffected.

The container's URL is `https://<app-name>.<env-domain>/api/messages` — use this as the
messaging endpoint when registering the Azure Bot (Step 3 of the prototype instructions).

### Case store caveat

`JsonFileCaseStore` writes case files to the local filesystem. In a container that scales
to zero, the filesystem is ephemeral — files written during one run are gone on the next
cold start. For anything beyond a quick demo, set `SCHEDULING_CASE_DIR` to a mounted
Azure Files volume, or replace the store with Cosmos DB. See `hosts/m365/case_store.py`.

---

## Production deployment (Azure AI Foundry)

Same as the prototype, with these differences:

**Model provider** — deploy Claude from the Azure AI Foundry model catalog, then set:

```bash
az webapp config appsettings set \
  --name <your-app-name> \
  --resource-group rg-scheduling-agent \
  --settings \
    SCHEDULING_MODEL_PROVIDER=foundry \
    ANTHROPIC_FOUNDRY_RESOURCE=<foundry-resource-name> \
    SCHEDULING_AGENT_MODEL=claude-opus-5-5
```

**Managed identity** — assign the app's managed identity the `Azure AI User` role on the
Foundry resource. Remove `ANTHROPIC_API_KEY`; no secret is needed.

```bash
# Enable managed identity on the web app
az webapp identity assign --name <your-app-name> --resource-group rg-scheduling-agent

# Grant it access to the Foundry resource
az role assignment create \
  --assignee <principal-id-from-above> \
  --role "Azure AI User" \
  --scope /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<foundry-resource>
```

**Secrets in Key Vault** — replace the bot client secret with a Key Vault reference:

```
CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET=
  @Microsoft.KeyVault(SecretUri=https://<vault>.vault.azure.net/secrets/bot-client-secret/)
```

**Case store** — `JsonFileCaseStore` (the default) is development-only. For production,
replace it with a tenant-scoped store such as Azure Cosmos DB. See `hosts/m365/case_store.py`.

---

## Configuration reference

All settings are environment variables. See `agent/src/scheduling_agent/hosts/m365/env.sample`
for the full list.

| Variable | Required for | Description |
|---|---|---|
| `SCHEDULING_MODEL_PROVIDER` | All Azure tiers | `copilot` / `anthropic` / `foundry` (default: `copilot`) |
| `ANTHROPIC_API_KEY` | `anthropic` | Direct Anthropic API key |
| `ANTHROPIC_FOUNDRY_RESOURCE` | `foundry` | Azure AI Foundry resource name |
| `ANTHROPIC_FOUNDRY_API_KEY` | `foundry` (optional) | Foundry API key; omit to use managed identity |
| `SCHEDULING_AGENT_MODEL` | All | Model ID (default: `claude-opus-5.5`) |
| `SCHEDULING_AGENT_EFFORT` | All | Reasoning effort: `low` / `medium` / `high` / `xhigh` / `max` (default: `high`) |
| `SCHEDULING_PROVIDER` | All | `graph` (real calendars) or `memory` (offline testing) |
| `SCHEDULING_ORGANIZER` | Graph | Shared recruiting mailbox UPN; blank = signed-in user |
| `CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID` | Azure | Bot app (client) ID |
| `CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET` | Azure | Bot client secret |
| `CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID` | Azure | Entra tenant ID |
| `AGENTAPPLICATION__USERAUTHORIZATION__HANDLERS__GRAPH__SETTINGS__AZUREBOTOAUTHCONNECTIONNAME` | Azure | OAuth connection name (default value: `graph`) |
