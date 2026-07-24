export interface Stage {
  key: string;
  label: string;
  agent: string;
  model: string;
}

export interface TaskDTO {
  id: string;
  title: string;
  description: string;
  status: string; // backlog | planning | execute | review | done
  session_id: string | null;
  agent: string;
  model: string;
  branch: string;
  status_kind: string | null; // error | ready | waiting | running | stale | null
  health: number | null;
  health_color: string | null;
  context_remaining: number | null;
  context_color: string | null;
  tokens: number | null;
  top_error: string | null;
  stale: boolean;
  attention: boolean;
  preview: string | null;
}

export interface Aggregate {
  running: number;
  waiting: number;
  ready: number;
  error: number;
  exec_active: number;
}

export interface State {
  type: string;
  project: string;
  base_branch: string;
  stages: Stage[];
  tasks: TaskDTO[];
  aggregate: Aggregate;
}
