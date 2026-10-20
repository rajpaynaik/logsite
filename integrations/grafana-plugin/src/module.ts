import { DataSourcePlugin } from '@grafana/data';

import { DataSource } from './datasource';
import { ConfigEditor } from './components/ConfigEditor';
import { QueryEditor } from './components/QueryEditor';
import { LogsiteOptions, LogsiteQuery } from './types';

export const plugin = new DataSourcePlugin<DataSource, LogsiteQuery, LogsiteOptions>(DataSource)
  .setConfigEditor(ConfigEditor)
  .setQueryEditor(QueryEditor);
