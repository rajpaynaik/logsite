import React, { ChangeEvent } from 'react';
import { InlineField, Input, SecretInput, Stack, Text } from '@grafana/ui';
import { DataSourcePluginOptionsEditorProps } from '@grafana/data';

import { LogsiteOptions, LogsiteSecureOptions } from '../types';

interface Props extends DataSourcePluginOptionsEditorProps<LogsiteOptions, LogsiteSecureOptions> {}

export function ConfigEditor({ onOptionsChange, options }: Props) {
  const { jsonData, secureJsonFields, secureJsonData } = options;

  const onUrlChange = (event: ChangeEvent<HTMLInputElement>) =>
    onOptionsChange({ ...options, jsonData: { ...jsonData, url: event.target.value.trim().replace(/\/+$/, '') } });

  const onTokenChange = (event: ChangeEvent<HTMLInputElement>) =>
    onOptionsChange({ ...options, secureJsonData: { token: event.target.value } });

  const onResetToken = () =>
    onOptionsChange({
      ...options,
      secureJsonFields: { ...secureJsonFields, token: false },
      secureJsonData: { ...secureJsonData, token: '' },
    });

  return (
    <Stack direction="column" gap={2}>
      <Text color="secondary">
        logsite groups your production errors by cause and maps them to the line of code that raised them. This data
        source reads them from <code>logsite alert-server</code>, running next to your code (see the Docker image in the
        logsite repository).
      </Text>
      <InlineField
        label="URL"
        labelWidth={14}
        interactive
        tooltip="Where logsite alert-server listens, as Grafana's server reaches it"
      >
        <Input
          id="config-editor-url"
          onChange={onUrlChange}
          value={jsonData.url ?? ''}
          placeholder="http://logsite:8085"
          width={40}
        />
      </InlineField>
      <InlineField
        label="Token"
        labelWidth={14}
        interactive
        tooltip="The alert-server's --token (LOGSITE_ALERT_TOKEN). Kept on the Grafana server, never sent to the browser."
      >
        <SecretInput
          id="config-editor-token"
          isConfigured={Boolean(secureJsonFields.token)}
          value={secureJsonData?.token ?? ''}
          placeholder="the shared secret"
          width={40}
          onReset={onResetToken}
          onChange={onTokenChange}
        />
      </InlineField>
    </Stack>
  );
}
