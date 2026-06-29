export const fmt = new Intl.NumberFormat();
export const money = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
export const views = new Set(['overview', 'turns', 'tools', 'subagents', 'cleanup']);
export const DEFAULT_TURN_PAGE_SIZE = 25;
export const CLEANUP_AFFECTED_FILE_PAGE_SIZE = 25;
export const TURN_SORT_LABELS = { date: 'Date', session: 'Session', prompt: 'Prompt', credits: 'Cost Units', raw: 'Total Tokens' };
export const TURN_SORT_KEYS = new Set(Object.keys(TURN_SORT_LABELS));
export const ROLLUP_SORT_DEFAULTS = {
  projects: { key: 'credits', dir: 'desc' },
  tools: { key: 'output_tokens', dir: 'desc' },
  subagents: { key: 'child_credits', dir: 'desc' },
};
export const ROLLUP_SORT_KEYS = {
  projects: new Set(['session', 'credits', 'raw', 'turns']),
  tools: new Set(['tool_name', 'calls', 'output_tokens', 'share']),
  subagents: new Set(['confidence', 'rows', 'child_credits', 'child_raw']),
};
export const CLEANUP_RETENTION_MODES = ['1', '7', '14', '30', '90', 'all', 'custom'];
export const SETTINGS_KEY = 'codex-token-usage-dashboard-settings';

export const state = {
  selected: null,
  selectedSession: null,
  selectedTool: null,
  selectedSubagentConfidence: null,
  themeMode: 'light',
  themeModeExplicit: false,
  view: 'overview',
  turnPage: 1,
  turnPageSize: DEFAULT_TURN_PAGE_SIZE,
  listPages: { projects: 1, tools: 1 },
  listSorts: {
    projects: { key: 'credits', dir: 'desc' },
    tools: { key: 'output_tokens', dir: 'desc' },
    subagents: { key: 'child_credits', dir: 'desc' },
  },
  listRows: { sessions: [], tools: [], subagents: [] },
  sessionOptions: [],
  sessionOptionsError: false,
  sessionFilterOpen: false,
  sessionActiveIndex: 0,
  turnSortKey: 'date',
  turnSortDir: 'desc',
  requestSeq: 0,
  sessionSeq: 0,
  detailSeq: 0,
  toolSeq: 0,
  subagentSeq: 0,
  modalSeq: 0,
  cleanupSeq: 0,
  cleanupRetentionAvailable: false,
  modalTrigger: null,
  cleanupModalTrigger: null,
  detailData: null,
  promptExpanded: false,
  toolSummaryExpanded: false,
  pendingViewFocus: false,
  pendingSession: '',
  cleanupRetentionMode: '7',
  cleanupSelectedFile: '',
  cleanupDetailPage: 1,
  cleanupDetailKey: '',
  cleanupDetailRow: null,
  appliedDaysMode: '7',
  rollupCache: { overview: null, tools: null, subagents: null },
};
