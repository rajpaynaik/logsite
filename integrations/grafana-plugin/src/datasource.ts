import { getBackendSrv, getTemplateSrv, isFetchError } from '@grafana/runtime';
import {
  CoreApp,
  DataQueryRequest,
  DataQueryResponse,
  DataSourceApi,
  DataSourceInstanceSettings,
  ScopedVars,
} from '@grafana/data';
import { lastValueFrom } from 'rxjs';

import { explanationsFrame, groupsFrame, historyFrame, incidentsFrame, summaryFrame } from './frames';
import { DEFAULT_QUERY, ErrorsResponse, HistoryEvent, LogsiteOptions, LogsiteQuery } from './types';

/** Panels on one dashboard ask for the same window at once: share one request between them. */
const SHARE_MS = 10_000;

export class DataSource extends DataSourceApi<LogsiteQuery, LogsiteOptions> {
  /** Grafana's proxy to the `logsite` route in plugin.json, which adds the token. */
  base: string;
  private recent = new Map<string, { at: number; response: Promise<unknown> }>();

  constructor(instanceSettings: DataSourceInstanceSettings<LogsiteOptions>) {
    super(instanceSettings);
    this.base = `${instanceSettings.url}/logsite`;
    this.annotations = {
      getDefaultQuery: () => ({ queryType: 'history' }),
    };
  }

  getDefaultQuery(_: CoreApp): Partial<LogsiteQuery> {
    return DEFAULT_QUERY;
  }

  async query(options: DataQueryRequest<LogsiteQuery>): Promise<DataQueryResponse> {
    const window = { from: String(options.range.from.valueOf()), to: String(options.range.to.valueOf()) };
    const data = await Promise.all(
      options.targets
        .filter((t) => !t.hide)
        .map(async (t) => {
          const type = t.queryType ?? 'groups';
          if (type === 'history') {
            return historyFrame(t.refId, await this.history(window));
          }
          const params = { ...window, ...this.labels(t.labels, options.scopedVars) };
          const r = await this.get<ErrorsResponse>('/errors', params);
          switch (type) {
            case 'summary':
              return summaryFrame(t.refId, r);
            case 'incidents':
              return incidentsFrame(t.refId, r);
            case 'explanations':
              return explanationsFrame(t.refId, r);
            default:
              return groupsFrame(t.refId, r);
          }
        })
    );
    return { data };
  }

  /** The history markers; none when the server runs without a history store. */
  async history(window: Record<string, string>): Promise<HistoryEvent[]> {
    try {
      return (await this.get<{ events: HistoryEvent[] }>('/history/events', window)).events;
    } catch (err) {
      if (err instanceof HttpError && err.status === 404) {
        return [];
      }
      throw err;
    }
  }

  /** `app=$app, namespace=prod` -> {app: "checkout", namespace: "prod"}; empty values mean all. */
  labels(text: string | undefined, scopedVars: ScopedVars): Record<string, string> {
    const out: Record<string, string> = {};
    for (const part of getTemplateSrv()
      .replace(text ?? '', scopedVars)
      .split(',')) {
      const [key, ...rest] = part.split('=');
      const value = rest.join('=').trim();
      if (key.trim() && value && value !== '$__all') {
        out[key.trim()] = value;
      }
    }
    return out;
  }

  async get<T>(path: string, params: Record<string, string> = {}): Promise<T> {
    const url = `${this.base}${path}?${new URLSearchParams(params)}`;
    const now = Date.now();
    for (const [key, hit] of this.recent) {
      if (now - hit.at > SHARE_MS) {
        this.recent.delete(key);
      }
    }
    let hit = this.recent.get(url);
    if (!hit) {
      const response = lastValueFrom(getBackendSrv().fetch<T>({ url, showErrorAlert: false })).then((r) => r.data);
      response.catch(() => this.recent.delete(url)); // don't share a failure
      hit = { at: now, response };
      this.recent.set(url, hit);
    }
    try {
      return (await hit.response) as T;
    } catch (err) {
      throw new HttpError(describe(err), isFetchError(err) ? err.status : undefined);
    }
  }

  /** Save & test: the server answers, the token is right, and it can read the logs. */
  async testDatasource() {
    try {
      await this.get('/healthz');
    } catch (err) {
      return { status: 'error', message: `Can't reach logsite alert-server: ${describe(err)}` };
    }
    try {
      const to = Date.now();
      const r = await this.get<ErrorsResponse>('/errors', { from: String(to - 15 * 60_000), to: String(to) });
      return {
        status: 'success',
        message: `Connected to logsite: ${r.events.toLocaleString()} error(s) in the last 15 minutes, ${r.groups_total} group(s).`,
      };
    } catch (err) {
      return { status: 'error', message: describe(err) };
    }
  }
}

class HttpError extends Error {
  constructor(
    message: string,
    readonly status?: number
  ) {
    super(message);
  }
}

function describe(err: unknown): string {
  if (isFetchError(err)) {
    const body = err.data as { error?: string; message?: string } | undefined;
    if (err.status === 401) {
      return 'the token is missing or wrong (it must match the alert-server --token / LOGSITE_ALERT_TOKEN)';
    }
    return `${err.status ?? ''} ${body?.error ?? body?.message ?? err.statusText ?? 'request failed'}`.trim();
  }
  return err instanceof Error ? err.message : String(err);
}
