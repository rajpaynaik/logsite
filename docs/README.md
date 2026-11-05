# logsite documentation

logsite reads the logs you already have and tells you which line of your code each error
comes from. No SDK, nothing to add to your services.

**New here?** Start with [Get started](get-started.md). It takes about five minutes.

## Find what's failing

- [**Errors**](errors.md): thousands of errors grouped into a few causes, each with the
  line that raised it and the line that logged it.
- [**Incidents**](errors.md#incidents): the errors behind an outage, linked to the deploy and
  the commit that likely caused it.
- [**Following a request**](timeline.md): every log line of one request, across services, in
  order.
- [**History**](history.md): whether an error was seen before, what fixed it, and when it came
  back.

## Fix it

- [**Reproduce an error**](repro.md): a production error written as a failing test.
- [**Agent mode**](agent.md): test and build failures grouped by cause, and a check that a
  test fails the way production did.
- [**An error as a prompt**](prompt.md): everything about one error, ready to paste into an
  AI assistant.

## Watch production

- [**Watch**](watch.md): checks your logs every few minutes, keeps every error in your own
  database, and posts to Slack when something is new, back or spiking.
- [**Release health**](releases.md): whether a release made errors worse, with the suspect
  commits and a verdict.
- [**Latency**](latency.md): which endpoints got slower, from the timings your logs already
  carry.
- [**Set up in your browser**](app.md): connect your logs, Slack and database without the
  terminal.
- [**Run on Kubernetes**](kubernetes.md): one small deployment, on any cluster or on EKS.

## Logs worth keeping

- [**Lint**](lint.md): logging mistakes in your code, found as you type or in CI.
- [**Cost**](cost-and-leaks.md): what each log call costs you per month.
- [**Leaks**](cost-and-leaks.md#leak-scanning): secrets and personal data that reached your
  logs.

## Where you work

- [**VS Code**](vscode.md): production counts, cost, latency and errors above each line of code.
- [**Grafana**](grafana.md): dashboards for cost and errors, error summaries in alerts, and
  rules that drop noisy logs before you pay for them.
- [**Claude and other AI assistants**](mcp.md): ask about production in plain English.
- [**Claude Code**](claude-code.md): the same, inside Claude Code.
- [**GitHub Action**](github-action.md): a pull request for each new production error.

## Your log store

logsite reads a log file, [Loki](configuration.md), OpenSearch, CloudWatch,
[Sentry](sentry.md) and [OpenTelemetry](opentelemetry.md), in
[Python, Java, Go, TypeScript and JavaScript](languages.md).

## Reference

- [Commands](commands.md): every command, one line each.
- [Configuration](configuration.md): every setting in `logsite.toml`.
- [How it works](how-it-works.md): how logsite gets from a log line to your code.
- [Data flow and security](security.md): what logsite reads, stores and sends.
- [Roadmap](roadmap.md): what's coming next.
