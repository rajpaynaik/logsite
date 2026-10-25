import { DataFrame } from '@grafana/data';

import fixture from '../tests/fixtures/errors.json';
import { explanationsFrame, groupsFrame, historyFrame, incidentsFrame, summaryFrame } from './frames';
import { ErrorsResponse } from './types';

// real `logsite alert-server` output for a cascading failure (three services, one root cause)
const response = fixture as unknown as ErrorsResponse;

const column = (frame: DataFrame, name: string) => frame.fields.find((f) => f.name === name)?.values;

describe('summary', () => {
  it('counts errors, groups, new groups and incidents', () => {
    const frame = summaryFrame('A', response);
    expect(column(frame, 'Errors')).toEqual([180]);
    expect(column(frame, 'Groups')).toEqual([3]);
    expect(column(frame, 'New groups')).toEqual([3]);
    expect(column(frame, 'Incidents')).toEqual([1]);
  });
});

describe('error groups', () => {
  const frame = groupsFrame('A', response);

  it('has one row per group with the short names', () => {
    expect(frame.length).toBe(3);
    expect(column(frame, 'Error')).toContain('ReadTimeout: timed out');
    expect(column(frame, 'Raised at')).toContain('inventory_client.py:18');
  });

  it('links the code columns to hidden URL columns', () => {
    const raised = frame.fields.find((f) => f.name === 'Raised at')!;
    expect(raised.config.links?.[0].url).toBe('${__data.fields["raised_url"]}');
    expect(frame.fields.find((f) => f.name === 'raised_url')!.config.custom.hidden).toBe(true);
  });
});

describe('incidents', () => {
  const frame = incidentsFrame('A', response);

  it('puts the cause first, without packages or directories', () => {
    expect(frame.length).toBe(1);
    expect(column(frame, 'Groups')).toEqual([3]);
    expect(column(frame, 'Cause')![0]).toMatch(/^QueryTimeoutException: /);
    expect(column(frame, 'Root cause')![0]).toMatch(/^PSQLException/);
    expect(column(frame, 'Raised at')).toEqual(['StockRepository.java:54']);
  });

  it('shows the most likely explanation and what to check first', () => {
    expect(column(frame, 'Most likely')![0]).toMatch(/^A dependency failing/);
    expect(column(frame, 'Evidence')).toEqual(['moderate']);
    expect(column(frame, 'Check first')![0]).toMatch(/^check the latency/);
  });
});

describe('ranked explanations', () => {
  it('has one row per explanation with the evidence as bullets', () => {
    const frame = explanationsFrame('A', response);
    expect(frame.length).toBe(response.incidents![0].diagnosis!.hypotheses.length);
    expect(column(frame, 'Score')![0]).toBe(4);
    expect(column(frame, 'For')![0]).toMatch(/^• the root cause is PSQLException/);
  });
});

describe('history events', () => {
  it('makes annotation fields', () => {
    const frame = historyFrame('A', [
      {
        time: 1760000000000,
        kind: 'came back',
        title: 'came back: x',
        text: 'was gone',
        tags: ['logsite'],
        group: 'ab',
      },
    ]);
    expect(frame.fields.map((f) => f.name)).toEqual(['time', 'title', 'text', 'tags', 'kind', 'group']);
  });
});
