import { DataFrame, FieldType, createDataFrame } from '@grafana/data';

import { ErrorsResponse, HistoryEvent, Incident } from './types';

const time = (iso?: string | null) => (iso ? Date.parse(iso) : null);

/** "com.example.orders.OrderException: …" -> "OrderException: …", as the alert-server does for groups */
const short = (name?: string | null) => (name ?? '').replace(/^(?:[a-z_$][\w$]*\.)+(?=[A-Z])/, '');

/** "src/main/java/com/example/CarrierApiClient.java:58" -> "CarrierApiClient.java:58" */
const base = (where?: string | null) => (where ?? '').split('/').pop() ?? '';

const services = (s?: Record<string, number>) => Object.keys(s ?? {}).join(', ');

/** A link on a column, to the URL in a hidden column of the same row. */
const linkTo = (field: string, title: string) => ({
  links: [{ title, url: `\${__data.fields["${field}"]}`, targetBlank: true }],
});

const hidden = { custom: { hidden: true, hideFrom: { viz: true, legend: true, tooltip: true } } };

export function summaryFrame(refId: string, r: ErrorsResponse): DataFrame {
  return createDataFrame({
    refId,
    fields: [
      { name: 'Errors', type: FieldType.number, values: [r.events] },
      { name: 'Before', type: FieldType.number, values: [r.previous_events] },
      { name: 'Groups', type: FieldType.number, values: [r.groups_total] },
      { name: 'New groups', type: FieldType.number, values: [r.groups.filter((g) => g.new).length] },
      { name: 'Incidents', type: FieldType.number, values: [(r.incidents ?? []).length] },
    ],
  });
}

export function groupsFrame(refId: string, r: ErrorsResponse): DataFrame {
  const g = r.groups;
  return createDataFrame({
    refId,
    meta: { preferredVisualisationType: 'table' },
    fields: [
      { name: 'Trend', type: FieldType.string, values: g.map((x) => x.trend_label ?? (x.new ? 'new' : x.trend ?? '')) },
      { name: 'Count', type: FieldType.number, values: g.map((x) => x.count) },
      { name: 'Error', type: FieldType.string, values: g.map((x) => x.title_short || x.title) },
      { name: 'Root cause', type: FieldType.string, values: g.map((x) => x.root_cause_short ?? '') },
      {
        name: 'Raised at',
        type: FieldType.string,
        values: g.map((x) => x.raised_name ?? ''),
        config: linkTo('raised_url', 'Open the code'),
      },
      {
        name: 'Logged at',
        type: FieldType.string,
        values: g.map((x) => x.logged_name ?? ''),
        config: linkTo('logged_url', 'Open the code'),
      },
      { name: 'Code', type: FieldType.string, values: g.map((x) => x.raised_code ?? '') },
      { name: 'Services', type: FieldType.string, values: g.map((x) => services(x.services)) },
      { name: 'History', type: FieldType.string, values: g.map((x) => x.history_label ?? '') },
      { name: 'First seen', type: FieldType.string, values: g.map((x) => x.first_seen_ever ?? '') },
      { name: 'Fixed before by', type: FieldType.string, values: g.map((x) => x.fixed_before ?? '') },
      { name: 'Example trace', type: FieldType.string, values: g.map((x) => x.example_trace ?? '') },
      { name: 'Group', type: FieldType.string, values: g.map((x) => x.id) },
      { name: 'Last seen', type: FieldType.time, values: g.map((x) => time(x.last_seen)) },
      { name: 'raised_url', type: FieldType.string, values: g.map((x) => x.raised_url ?? ''), config: hidden },
      { name: 'logged_url', type: FieldType.string, values: g.map((x) => x.logged_url ?? ''), config: hidden },
    ],
  });
}

const top = (i: Incident) => i.diagnosis?.hypotheses.find((h) => h.score > 0);

export function incidentsFrame(refId: string, r: ErrorsResponse): DataFrame {
  // the groups carry the code links and the history columns; an incident's cause is one of them
  const byId = new Map(r.groups.map((g) => [g.id, g]));
  const inc = (r.incidents ?? []).map((i) => ({ ...i, cause: { ...i.cause, ...byId.get(i.cause.id) } }));
  const commit = (i: Incident) => {
    const c = i.suspect_commits[0];
    return c ? `${c.sha.slice(0, 8)} ${c.author}: ${c.summary}` : '';
  };
  return createDataFrame({
    refId,
    meta: { preferredVisualisationType: 'table' },
    fields: [
      { name: 'Incident', type: FieldType.number, values: inc.map((_, n) => n + 1) },
      { name: 'Started', type: FieldType.time, values: inc.map((i) => time(i.started)) },
      { name: 'Events', type: FieldType.number, values: inc.map((i) => i.events) },
      { name: 'Groups', type: FieldType.number, values: inc.map((i) => 1 + i.effects.length) },
      { name: 'Cause', type: FieldType.string, values: inc.map((i) => i.cause.title_short || short(i.cause.title)) },
      {
        name: 'Root cause',
        type: FieldType.string,
        values: inc.map((i) => short(i.shared_root_cause?.cause ?? i.cause.root_cause_short ?? i.cause.root_cause)),
      },
      {
        name: 'Raised at',
        type: FieldType.string,
        values: inc.map((i) => i.cause.raised_name ?? base(i.cause.raised_at)),
        config: linkTo('raised_url', 'Open the code'),
      },
      { name: 'Most likely', type: FieldType.string, values: inc.map((i) => top(i)?.title ?? '') },
      { name: 'Evidence', type: FieldType.string, values: inc.map((i) => i.diagnosis?.strength ?? '') },
      { name: 'Check first', type: FieldType.string, values: inc.map((i) => top(i)?.next_check ?? '') },
      {
        name: 'Deploy',
        type: FieldType.string,
        values: inc.map((i) => (i.deploy ? `${i.deploy.ref} at ${i.deploy.at.slice(0, 16).replace('T', ' ')}` : '')),
      },
      { name: 'Suspect commit', type: FieldType.string, values: inc.map(commit) },
      { name: 'History', type: FieldType.string, values: inc.map((i) => i.cause.history_label ?? '') },
      { name: 'Fixed before by', type: FieldType.string, values: inc.map((i) => i.cause.fixed_before ?? '') },
      { name: 'raised_url', type: FieldType.string, values: inc.map((i) => i.cause.raised_url ?? ''), config: hidden },
    ],
  });
}

export function explanationsFrame(refId: string, r: ErrorsResponse): DataFrame {
  const rows = (r.incidents ?? []).flatMap((i, n) =>
    (i.diagnosis?.hypotheses ?? []).map((h) => ({ n: n + 1, i, h }))
  );
  const lines = (xs: string[]) => xs.map((x) => `• ${x}`).join('\n');
  return createDataFrame({
    refId,
    meta: { preferredVisualisationType: 'table' },
    fields: [
      { name: 'Incident', type: FieldType.number, values: rows.map((x) => x.n) },
      { name: 'Explanation', type: FieldType.string, values: rows.map((x) => x.h.title) },
      { name: 'Score', type: FieldType.number, values: rows.map((x) => x.h.score) },
      { name: 'For', type: FieldType.string, values: rows.map((x) => lines(x.h.supporting)) },
      { name: 'Against', type: FieldType.string, values: rows.map((x) => lines(x.h.against)) },
      { name: 'Not known', type: FieldType.string, values: rows.map((x) => lines(x.h.unknown)) },
      { name: 'Check next', type: FieldType.string, values: rows.map((x) => x.h.next_check ?? '') },
    ],
  });
}

/** First seen / gone / came back, as annotations (time, title, text, tags). */
export function historyFrame(refId: string, events: HistoryEvent[]): DataFrame {
  return createDataFrame({
    refId,
    fields: [
      { name: 'time', type: FieldType.time, values: events.map((e) => e.time) },
      { name: 'title', type: FieldType.string, values: events.map((e) => e.title) },
      { name: 'text', type: FieldType.string, values: events.map((e) => e.text) },
      { name: 'tags', type: FieldType.other, values: events.map((e) => e.tags) },
      { name: 'kind', type: FieldType.string, values: events.map((e) => e.kind) },
      { name: 'group', type: FieldType.string, values: events.map((e) => e.group) },
    ],
  });
}
