# logsite data source for Grafana

Puts logsite under **Connections** in Grafana: error groups, incidents, ranked explanations
and history annotations, read from `logsite alert-server`. What users see is in
[src/README.md](src/README.md) (the catalog page); setup is in
[docs/grafana.md](../../docs/grafana.md#the-logsite-data-source-grafana-plugin).

Frontend only: Grafana's data source proxy calls the alert-server through the `logsite`
route in `src/plugin.json`, which adds the token from `secureJsonData`. Scaffolded with
[`@grafana/create-plugin`](https://grafana.com/developers/plugin-tools/); don't edit `.config/`.

## Develop

```bash
npm install
npm run dev            # build and watch, into dist/
npm run test:ci        # unit tests: the tables built from real alert-server output
npm run typecheck && npm run lint
```

Run it in Grafana with the plugin directory pointing at `dist/`:

```bash
logsite alert-server --host 0.0.0.0 --token dev-token   # in a project with a logsite.toml
npm run server         # Docker: Grafana on :3000 with the plugin and a provisioned data source
```

Without Docker, set `plugins = <a directory>` with a link named
`rajpaynaik-logsite-datasource` to `dist/`, and
`allow_loading_unsigned_plugins = rajpaynaik-logsite-datasource` in `grafana.ini`.
Grafana caches the plugin by its version, so after a rebuild, hard-reload the page (or bump
the version) to see the change. Changes to `plugin.json` need a Grafana restart.

## Release

The plugin has its own version (`package.json`, `CHANGELOG.md`). Each logsite release
attaches `rajpaynaik-logsite-datasource-<version>.zip`. For the Grafana catalog, it must be
signed: see [Publish a plugin](https://grafana.com/developers/plugin-tools/publish-a-plugin/).
