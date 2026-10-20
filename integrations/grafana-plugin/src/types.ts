import { DataSourceJsonData } from '@grafana/data';
import { DataQuery } from '@grafana/schema';

/** What a panel shows: each is one table built from logsite alert-server's JSON. */
export type QueryType = 'summary' | 'groups' | 'incidents' | 'explanations' | 'history';

export interface LogsiteQuery extends DataQuery {
  queryType?: QueryType;
  /** `app=$app, namespace=prod`: narrows the Loki selector, like an alert's labels do */
  labels?: string;
}

export const DEFAULT_QUERY: Partial<LogsiteQuery> = {
  queryType: 'groups',
};

export const QUERY_TYPES: Array<{ value: QueryType; label: string; description: string }> = [
  { value: 'summary', label: 'Summary', description: 'Errors, groups, new groups and incidents: one row, for stat panels' },
  { value: 'groups', label: 'Error groups', description: 'Production errors grouped by cause, new and spiking first' },
  { value: 'incidents', label: 'Incidents', description: 'Linked groups with the cause first, the deploy and the suspect commit' },
  {
    value: 'explanations',
    label: 'Ranked explanations',
    description: 'Each incident’s explanations with the evidence for and against, and what to check next',
  },
  { value: 'history', label: 'History events', description: 'First seen, gone and came back: for annotations' },
];

/** Options configured for each data source instance. `url` is the logsite alert-server. */
export interface LogsiteOptions extends DataSourceJsonData {
  url?: string;
}

/** Sent to Grafana's proxy only, never to the browser. */
export interface LogsiteSecureOptions {
  token?: string;
}

// --- logsite alert-server JSON (the fields used here) -------------------------------------

export interface Commit {
  sha: string;
  author: string;
  date: string;
  summary: string;
}

export interface Group {
  id: string;
  title: string;
  title_short?: string;
  type?: string | null;
  root_cause?: string | null;
  root_cause_short?: string | null;
  count: number;
  previous: number;
  trend?: string | null;
  trend_label?: string;
  new: boolean;
  severe?: boolean;
  first_seen?: string | null;
  last_seen?: string | null;
  raised_at?: string | null;
  raised_name?: string | null;
  raised_url?: string | null;
  raised_code?: string | null;
  logged_at?: string | null;
  logged_name?: string | null;
  logged_url?: string | null;
  services?: Record<string, number>;
  example_trace?: string | null;
  history_label?: string | null;
  first_seen_ever?: string | null;
  fixed_before?: string | null;
}

export interface Hypothesis {
  cause: string;
  title: string;
  score: number;
  supporting: string[];
  against: string[];
  unknown: string[];
  next_check?: string | null;
}

export interface Incident {
  started?: string | null;
  last_seen?: string | null;
  events: number;
  new: boolean;
  cause: Group;
  effects: Group[];
  deploy?: { ref: string; at: string } | null;
  suspect_commits: Commit[];
  diagnosis?: { strength: string; hypotheses: Hypothesis[]; unknown: string[] } | null;
  shared_root_cause?: { cause: string; entry_points: string[] } | null;
  impact?: Record<string, unknown> | null;
}

export interface ErrorsResponse {
  window: { start: string; end: string };
  events: number;
  previous_events: number;
  groups_total: number;
  groups: Group[];
  incidents?: Incident[];
}

export interface HistoryEvent {
  time: number;
  kind: string;
  title: string;
  text: string;
  tags: string[];
  group: string;
}
