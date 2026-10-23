# Security

## Reporting a vulnerability

Please report it privately through GitHub: **Security → Report a vulnerability** on
[this repository](https://github.com/rajpaynaik/logsite/security/advisories/new). Don't open a
public issue.

You'll get an answer within 3 days. Once it's confirmed, a fix is released as soon as it's
ready, and the advisory credits you unless you'd rather it didn't.

## Supported versions

The latest release gets security fixes. logsite is pre-1.0, so upgrade to the latest 0.x.

## What logsite does with your data

logsite runs on your machine or in your CI. It reads your logs and your code, and sends
nothing anywhere except to the log store and alert destinations you configure. It has no
telemetry. See [Data flow and security](docs/security.md) for what it reads, what it keeps on
disk, and what it masks.

## Verifying a release

From 0.4.1 on, each release's wheel, source package, editor extension and Grafana plugin
come with signed build provenance, and the release has an SBOM (`*.spdx.json`) of the package and its
dependencies:

```bash
gh attestation verify logsite-0.4.1-py3-none-any.whl --repo rajpaynaik/logsite
```

The packages on PyPI also carry PyPI's own provenance, from trusted publishing (0.4.0 already does).
