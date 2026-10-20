import React, { ChangeEvent } from 'react';
import { Combobox, InlineField, Input, Stack } from '@grafana/ui';
import { QueryEditorProps } from '@grafana/data';

import { DataSource } from '../datasource';
import { LogsiteOptions, LogsiteQuery, QUERY_TYPES, QueryType } from '../types';

type Props = QueryEditorProps<DataSource, LogsiteQuery, LogsiteOptions>;

export function QueryEditor({ query, onChange, onRunQuery }: Props) {
  const type = query.queryType ?? 'groups';

  const onTypeChange = (option: { value?: QueryType } | null) => {
    onChange({ ...query, queryType: option?.value ?? 'groups' });
    onRunQuery();
  };

  const onLabelsChange = (event: ChangeEvent<HTMLInputElement>) => onChange({ ...query, labels: event.target.value });

  return (
    <Stack gap={0} wrap="wrap">
      <InlineField label="Show" labelWidth={10}>
        <Combobox
          id="query-editor-type"
          options={QUERY_TYPES}
          value={type}
          onChange={onTypeChange}
          width={30}
        />
      </InlineField>
      {type !== 'history' && (
        <InlineField
          label="Labels"
          labelWidth={10}
          grow
          tooltip="Narrow the Loki selector, e.g. app=$app, namespace=prod. Empty: everything logsite.toml selects."
        >
          <Input
            id="query-editor-labels"
            value={query.labels ?? ''}
            onChange={onLabelsChange}
            onBlur={onRunQuery}
            placeholder="app=$app"
          />
        </InlineField>
      )}
    </Stack>
  );
}
