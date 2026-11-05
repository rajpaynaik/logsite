# VS Code

The logsite extension shows what production says about your code, right above each line.

```java
return restTemplate.postForObject(url + "/labels", req, LabelResponse.class);  // ⛔ IllegalStateException 400 (new) in 24h
log.info("label created shipment={}", shipment.getId());                        // 📈 1.2M in 24h ▲ 8.0x · p95 180ms · $42/mo
log.warn("legacy rate table used for {}", carrier);                             // no logs in 24h
```

## Install

1. Install the extension from the
   [Visual Studio Marketplace](https://marketplace.visualstudio.com/items?itemName=rajpaynaik.logsite)
   or [Open VSX](https://open-vsx.org/extension/rajpaynaik/logsite) (VSCodium, Cursor,
   Windsurf).
2. Install logsite with the editor support: `pip install "logsite[lsp]"`.
3. Point it at your logs with a `logsite.toml` in the project. `logsite init` writes one.
   Lint works without it.

## What you see

- **Errors** above the line that raised them, with how often and whether they're new.
- **How often** each log call ran, with an arrow when it spiked and "no logs" when it never
  runs.
- **How long it took**, as p95, when the line carries a timing.
- **What it costs** per month, once you set your price per GB.
- **Lint** as you type: logging mistakes, with a quick fix for most of them.

Hover over any of them for the details and the last commits that changed the code.

Any editor that speaks the Language Server Protocol gets the same with `logsite serve`.
