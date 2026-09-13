export const parseBeijingDate = (value: string): Date => {
  const normalized = value.trim().replace(' ', 'T');
  return new Date(/^\d{4}-\d{2}-\d{2}$/.test(normalized)
    ? `${normalized}T00:00:00+08:00`
    : /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(normalized) && !/(Z|[+-]\d{2}:?\d{2})$/i.test(normalized)
      ? `${normalized}+08:00` : normalized);
};

export const formatDateTime = (value?: string): string => {
  if (!value) return '—';
  const date = parseBeijingDate(value);
  if (Number.isNaN(date.getTime())) return value;

  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
};

export const formatDate = (value?: string): string => {
  if (!value) return '—';
  const date = parseBeijingDate(value);
  if (Number.isNaN(date.getTime())) return value;

  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(date);
};

export const toDateInputValue = (date: Date): string => {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(date);
};

/**
 * Returns the date N days ago as YYYY-MM-DD in Asia/Shanghai timezone.
 * Consistent with getTodayInShanghai() so both ends of the date range
 * are expressed in the same timezone as the backend.
 */
export const getRecentStartDate = (days: number): string => {
  const date = new Date();
  date.setUTCDate(date.getUTCDate() - days);
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(date);
};

/**
 * Returns today's date as YYYY-MM-DD in Asia/Shanghai timezone.
 * Use this instead of browser-local date to stay consistent with the backend,
 * which stores and filters timestamps in server local time (Asia/Shanghai).
 */
export const getTodayInShanghai = (): string =>
  new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(new Date());

export const formatReportType = (value?: string): string => {
  if (!value) return '—';
  if (value === 'simple') return '普通';
  if (value === 'detailed') return '标准';
  return value;
};
