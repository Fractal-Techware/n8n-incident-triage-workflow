# Alertmanager → LLM triage → chat: one importable n8n workflow, tested end to end

[![e2e tests](https://github.com/Fractal-Techware/n8n-incident-triage-workflow/actions/workflows/test.yml/badge.svg)](https://github.com/Fractal-Techware/n8n-incident-triage-workflow/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![n8n 2.39.7](https://img.shields.io/badge/n8n-2.39.7-ea4b71?logo=n8n&logoColor=white)
![Workflow: 1](https://img.shields.io/badge/workflow-1%20importable%20JSON-informational)
![e2e tests: 10](https://img.shields.io/badge/e2e%20tests-10%20passing-brightgreen)

At 3am `KubePodCrashLooping` in `#alerts` tells you almost nothing. **This workflow does the first
five minutes for you:** Alertmanager posts the alert group to n8n, an LLM of your choice reads it,
and one chat message arrives with the summary, the likely cause, concrete first checks and links to
the runbook, the alert source and a prefilled silence.

It is one file you import into n8n. No sub-workflows, no database, no LangChain nodes — core nodes only
(Webhook, Set, Code, If, HTTP Request), so it runs on n8n Cloud and on self-hosted alike.

What makes it safe to put in front of a real alert stream:

- **Secrets never reach the model.** Label and annotation values whose key looks sensitive become
  `[REDACTED]`, and tokens, keys, JWTs, private keys, connection strings and e-mail addresses are
  stripped from the text — before the prompt and again before the chat message.
- **The notification always arrives.** LLM down, slow, or answering prose instead of JSON? You get the
  alert with a one-line note saying the summary is unavailable. Nothing is guessed.
- **Resolved groups cost nothing** — they skip the model and post a short "it is over" message.
- **One message per Alertmanager group**, not per alert, at the highest severity in the group.
- **Alert data is treated as untrusted** in the prompt, so an alert annotation cannot give the model
  instructions.

Every one of those claims has an end-to-end test in this repository ([see below](#how-it-is-tested)).

By [Fractal Techware](https://github.com/Fractal-Techware). MIT licensed.

## What's included

| File | What it is |
|---|---|
| `workflows/alertmanager-ai-triage.json` | The workflow: webhook → settings → normalise + redact → LLM → chat message |
| `tests/run_tests.py` | 10 end-to-end tests that run the real workflow inside a pinned n8n container |
| `tests/mock/mock.py` | Stdlib mock of the LLM endpoint and the chat webhook (no network, no API keys) |
| `tests/lib/stack.py` | The Docker harness: imports the shipped JSON, publishes it, drives its webhook |
| `tests/lib/payloads.py` | Alertmanager webhook v4 payload builders |
| `examples/docker-compose.yml` | A pinned n8n to try it in |
| `examples/alertmanager.yml` | The receiver config, with the shared secret kept in a file |
| `examples/samples/*.json` | A firing and a resolved sample payload |
| `examples/send-test-alert.sh` | `curl` the sample payload at your webhook |
| `docs/setup.md` | Credentials, every settings field, costs, troubleshooting |

1 workflow · 9 nodes · 10 end-to-end tests · pinned to n8n 2.39.7.

## 60-second quick start

```bash
git clone https://github.com/Fractal-Techware/n8n-incident-triage-workflow.git
cd n8n-incident-triage-workflow
./run-tests.sh      # proves it works: pinned n8n + mocked LLM and chat, no API keys needed
```

```
stack ready in 23s (http://127.0.0.1:5699)
PASS  test_fenced_json_answer_is_parsed  (0.6s)
PASS  test_firing_group_is_triaged_and_posted  (0.6s)
...
10/10 tests passed in 28s
```

Then, in your own n8n:

1. **Import** `workflows/alertmanager-ai-triage.json` (Workflows → Import from File).
2. **Create two Header Auth credentials**: `Triage webhook auth`
   (`Authorization: Bearer <openssl rand -hex 24>`) and `Triage LLM API key`
   (`Authorization: Bearer <your LLM key>`).
3. **Edit the Settings node**: your `llm.url` and `llm.model`, and `notify.url` (a Slack incoming
   webhook or any endpoint that accepts a JSON POST).
4. **Activate**, then point Alertmanager at
   `https://<your-n8n>/webhook/ftw-alertmanager-triage` using `examples/alertmanager.yml`.

Try it before touching Alertmanager:

```bash
FTW_WEBHOOK_SECRET=<the value from the credential, without "Bearer "> \
  ./examples/send-test-alert.sh http://localhost:5678
```

Full walkthrough, every setting and the troubleshooting table: [docs/setup.md](docs/setup.md).

## What the message looks like

```
:rotating_light: [FIRING:2] HighErrorRate, HighLatency (namespace=shop service=checkout)

Checkout is returning 12% 5xx and p99 latency has tripled since 09:41 UTC.

*Likely cause:* the payment client is retrying against a saturated connection pool.

*First checks:*
• Compare the error rate with the last deploy of checkout
• kubectl -n shop logs deploy/checkout --since=20m | grep -i pool
• Check the payment provider status page

*Alerts:*
• [critical] HighErrorRate — namespace=shop service=checkout pod=checkout-7d9f-abc: checkout is serving 12% 5xx responses
• [warning] HighLatency — namespace=shop service=checkout: p99 latency 2.4s

<https://runbooks.example.com/shop/HighErrorRate.md|Runbook> · <…|Source> · <…|Silence>

_AI-generated with gpt-5-mini — verify before acting._
```

(The summary, likely cause and first checks come from your model; everything else from the alert.)

## How it is tested

`./run-tests.sh` starts one **n8n 2.39.7** container and one Python container that pretends to be your
LLM endpoint and your chat webhook. The workflow JSON is imported exactly as it ships — only the
Settings node is overlaid so the two URLs point at the mock — then published and driven through its
real webhook. The tests assert on **what n8n actually sent**:

| Test | Proves |
|---|---|
| `test_firing_group_is_triaged_and_posted` | The prompt carries the alert and the model from Settings; the message carries the summary, cause, checks, runbook and silence links |
| `test_resolved_group_skips_the_llm` | A resolved group produces a short message and **zero** LLM calls |
| `test_secrets_are_redacted_before_the_llm_and_chat` | A `password` label, a `ghp_…` token and an e-mail address never appear in the LLM request or the chat message |
| `test_llm_failure_still_notifies` | LLM answers 500 → the alert still reaches chat with an honest note |
| `test_malformed_llm_answer_is_not_posted_as_a_summary` | Prose instead of JSON → the same note, no raw model text |
| `test_fenced_json_answer_is_parsed` | JSON inside a ```json fence is still understood |
| `test_group_of_alerts_is_one_message_at_the_highest_severity` | 3 alerts → exactly 1 message, critical icon |
| `test_webhook_requires_the_header_credential` | No credential / wrong credential → rejected, and no upstream is touched |
| `test_non_alertmanager_payload_fails_loudly` | A payload without `alerts[]` fails instead of posting a half-empty message |
| `test_shipped_sample_payload_produces_a_message` | The sample payload in `examples/samples/` works as shipped |

No test reaches the internet and no API key is involved; CI runs the same script
([`.github/workflows/test.yml`](.github/workflows/test.yml)). Change the workflow, run it again.

## Want all the workflows?

This repository is the free edition of the
**[n8n AI Incident Triage Workflows](https://store.fractaltechware.com/l/n8n-incident-triage-workflows?utm_source=github&utm_medium=readme&utm_campaign=free-repo)**
pack — same build, same test standard, more of the incident lifecycle:

| | **Free** (this repo) | **Starter** $19 | **Pro** $49 | **Agency** $299 |
|---|:---:|:---:|:---:|:---:|
| Importable workflows | 1 | 6 | 12 | 16 |
| Alertmanager triage | yes | yes | yes | yes |
| LLM providers | any OpenAI-compatible endpoint | + Anthropic, Ollama, OpenAI Responses API (router) | same | same |
| Chat destinations | any JSON webhook (Slack-style text) | Slack Block Kit, Teams Adaptive Card, Discord embed, generic | same | same |
| Grafana Alerting triage, daily alert-noise digest | – | yes | yes | yes |
| Runbook-aware triage, Kubernetes pod failure explainer (read-only RBAC) | – | – | yes | yes |
| Dedup & flap suppression, PagerDuty escalation, postmortem drafts, error workflow | – | – | yes | yes |
| Loki log context, per-team routing, weekly on-call handover, prompt eval kit (12 golden alerts) | – | – | – | yes |
| License | MIT | own organization | own organization | client / agency use |

[See the full pack on Gumroad →](https://store.fractaltechware.com/l/n8n-incident-triage-workflows?utm_source=github&utm_medium=readme&utm_campaign=free-repo)

More free, tested building blocks: [github.com/Fractal-Techware](https://github.com/Fractal-Techware).

## Contributing

Issues and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Every behaviour change
needs a test in `tests/run_tests.py`.

## More free editions from Fractal Techware

Every pack has a free, MIT-licensed edition. These are the other nine — all runnable, all with
their own tests.

- [Prometheus Alert Rules & Runbooks](https://github.com/Fractal-Techware/prometheus-alert-rules) — tested alert rules, each with a runbook
- [Kubernetes Hardening Baseline](https://github.com/Fractal-Techware/kubernetes-hardening-baseline) — Kyverno policies proven with enforce semantics
- [Production Helm Chart](https://github.com/Fractal-Techware/helm-production-chart) — library chart with secure defaults and helm-unittest suites
- [OpenTelemetry Collector Recipes](https://github.com/Fractal-Techware/opentelemetry-collector-recipes) — collector configs: tail sampling, PII redaction, Kubernetes
- [Grafana Dashboards](https://github.com/Fractal-Techware/grafana-dashboards) — provisioned dashboards for hosts, Kubernetes and Prometheus
- [SLO as Code](https://github.com/Fractal-Techware/slo-as-code) — SLIs, error budgets and burn-rate alerts generated from YAML
- [VPS Observability Stack](https://github.com/Fractal-Techware/vps-observability-stack) — single-server Prometheus, Grafana and Loki behind Caddy
- [n8n Production Compose](https://github.com/Fractal-Techware/n8n-production-compose) — hardened n8n with Postgres and automatic HTTPS
- [n8n GitHub PR Summary](https://github.com/Fractal-Techware/n8n-github-pr-summary) — AI pull-request review and summary workflows

The paid tiers and the full catalogue are at [fractaltechware.com](https://fractaltechware.com).

## License

[MIT](LICENSE) © 2026 Fractal Techware SRL
