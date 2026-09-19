# Setup

Everything the workflow needs: one n8n instance, one LLM endpoint, one chat webhook.
Nothing else is required — no database, no queue, no sub-workflows.

## 1. Requirements

| | Version tested |
|---|---|
| n8n | 2.39.7 (self-hosted, Docker; Cloud works too) |
| LLM endpoint | any OpenAI-compatible `POST /v1/chat/completions` — OpenAI, Azure OpenAI, OpenRouter, Together, vLLM, Ollama (`/v1/chat/completions`) |
| Alert source | Prometheus Alertmanager (webhook payload **version 4**) |
| Destination | any webhook that accepts a JSON POST — Slack incoming webhook, Mattermost, Discord, Teams workflow, your own endpoint |

The workflow uses only core n8n nodes (Webhook, Set, Code, If, HTTP Request), so it also runs on
n8n versions that do not ship the LangChain nodes.

## 2. Import the workflow

n8n UI → **Workflows** → **Import from File** → `workflows/alertmanager-ai-triage.json`.

Or from the CLI, inside the n8n container:

```bash
n8n import:workflow --input=/path/to/alertmanager-ai-triage.json
```

The import references two credentials by id. Create them in n8n (**Credentials** → **New**) — you can
keep the names, then reopen the workflow and pick them on the two nodes if n8n did not match them:

| Credential | Type | Value |
|---|---|---|
| `Triage webhook auth` | Header Auth | Name `Authorization`, value `Bearer <a long random string you invent>` |
| `Triage LLM API key` | Header Auth | Name `Authorization`, value `Bearer <your LLM API key>` |

Generate the webhook secret with `openssl rand -hex 24`. Alertmanager sends the same value
(see `examples/alertmanager.yml`).

Using a provider that does not take a bearer token (for example Azure OpenAI with `api-key`)?
Change the credential's header name to `api-key` and put the raw key in the value.

## 3. Fill in the Settings node

Open the **Settings** node. It is a plain JSON document:

| Field | Meaning |
|---|---|
| `llm.url` | Full chat-completions URL, e.g. `https://api.openai.com/v1/chat/completions` |
| `llm.model` | Model id, e.g. `gpt-5-mini`. Pin it — a "latest" alias changes your output without warning |
| `llm.timeout_seconds` | HTTP timeout for the model call (default 60) |
| `llm.max_alerts_in_prompt` | Alerts from one group sent to the model (default 20) |
| `llm.max_input_chars` | Hard cap on the prompt payload (default 12000) |
| `llm.redact.emails` | Replace email addresses before the prompt (default `true`) |
| `llm.redact.ipv4` | Replace IPv4 addresses too (default `false` — they are usually useful) |
| `llm.redact.drop_keys_pattern` | Label/annotation **keys** whose values are replaced with `[REDACTED]` |
| `prompt` | The system prompt. Edit it to match your stack's vocabulary |
| `notify.url` | The chat webhook URL the message is posted to |
| `notify.format` | `slack` (posts `{"text": ...}`) or `generic` (posts a structured JSON object) |
| `links.alertmanager_url` | Public Alertmanager base URL, used to build the "Silence" link. Empty falls back to the `externalURL` in the payload |

`notify.url` is a secret for Slack-style webhooks (anyone with the URL can post to the channel).
It is stored inside the workflow, so treat an exported workflow JSON as sensitive, and keep the
n8n `N8N_ENCRYPTION_KEY` and your n8n backups private.

## 4. Activate and point Alertmanager at it

Activate the workflow, then note the production webhook URL:
`https://<your-n8n>/webhook/ftw-alertmanager-triage`.

Add the receiver from `examples/alertmanager.yml` to your `alertmanager.yml`, check it with
`amtool check-config alertmanager.yml` and reload Alertmanager.

Send a test payload without waiting for a real incident:

```bash
FTW_WEBHOOK_SECRET=<the value from the credential, without "Bearer "> \
  ./examples/send-test-alert.sh http://localhost:5678
```

## 5. Environment variables

The workflow itself reads no environment variables — everything is in the Settings node and the two
credentials. These are used by the supporting files:

| Variable | Used by | Purpose |
|---|---|---|
| `N8N_ENCRYPTION_KEY` | `examples/docker-compose.yml` | Encrypts credentials in the n8n volume. Replace the placeholder with a long random string before storing a real key |
| `WEBHOOK_URL` | `examples/docker-compose.yml` | The public base URL n8n prints for webhooks |
| `FTW_WEBHOOK_SECRET` | `examples/send-test-alert.sh` | The webhook credential value, without the `Bearer ` prefix |

No file in this repository contains an API key, token or webhook URL — only placeholders.

## 6. What the workflow does, node by node

| Node | What it does |
|---|---|
| `Alertmanager webhook` | `POST /webhook/ftw-alertmanager-triage`, header-auth. A request without the credential is rejected by n8n before anything runs |
| `Settings` | The JSON above |
| `Prepare alert group` | Validates the payload (`alerts[]` must exist), normalises severities, builds the group title, `where` (namespace/service/pod…), runbook, source and silence links, replaces secret-looking values, and builds the prompt |
| `Firing?` | Resolved-only groups branch away from the model |
| `Ask LLM` | One `POST` to `llm.url` with `{model, messages}`. Retries once on a connection error or timeout; an HTTP error is passed on instead of failing the workflow |
| `Build notification` | Parses the model's JSON answer (including a ```json fence), and on anything unexpected produces a message that says the summary is unavailable instead of guessing |
| `Resolved: short message` | The no-AI message for resolved groups |
| `Send to chat` | One `POST` to `notify.url` |

## 7. Costs and limits

One LLM call per firing Alertmanager group — not per alert. With `group_by`, `group_wait: 30s` and
`repeat_interval: 4h` as in the example config, a noisy week is tens of calls, not thousands.
Resolved groups cost nothing.

## 8. Troubleshooting

| Symptom | Cause |
|---|---|
| Alertmanager logs `401`/`403` | The `Authorization` header value in the credential and in `credentials_file` differ (the file must contain `Bearer <secret>`, no trailing newline: use `echo -n`) |
| Execution fails at `Prepare alert group` with "alerts[] is missing" | Something other than Alertmanager posted to the webhook, or Alertmanager is configured with a non-webhook receiver type |
| Message says "AI summary unavailable (LLM HTTP 401)" | The LLM credential is wrong for that provider (wrong header name or key) |
| Message says "the model did not return the expected JSON" | The model is too small to follow the JSON instruction, or an endpoint returned a non-standard shape. Try a stronger model |
| Nothing arrives in chat | Open the execution in n8n: `Send to chat` shows the destination's HTTP response. Slack answers `invalid_token` for a revoked webhook |
| The workflow never runs | It is not activated, or Alertmanager is still grouping (`group_wait`) |

Run `./run-tests.sh` after any change you make: it imports your edited workflow into a throwaway
n8n and checks the behaviour above end to end.
