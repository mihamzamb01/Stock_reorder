export function fmtMs(ms) {
  if (ms == null) return "-";
  if (ms >= 60000) return `${(ms / 60000).toFixed(1)} min`;
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`;
  return `${ms}ms`;
}

export function fmtInt(n) {
  return (n ?? 0).toLocaleString();
}

export function fmtUsd(usd) {
  const n = Number(usd) || 0;
  return `$${n.toFixed(2)}`;
}

/** Fetch the executions table from the dashboard API. */
export async function fetchExecutions() {
  const res = await fetch("/api/executions");
  if (!res.ok) throw new Error(`executions ${res.status}`);
  return res.json();
}

/** Fetch the aggregated run summary. */
export async function fetchSummary() {
  const res = await fetch("/api/summary");
  if (!res.ok) throw new Error(`summary ${res.status}`);
  return res.json();
}

/** Fetch the full trace of one run by correlation ID. */
export async function fetchTrace(corrId) {
  const res = await fetch(`/api/traces?correlation_id=${encodeURIComponent(corrId)}`);
  if (!res.ok) throw new Error(`trace ${res.status}`);
  return res.json();
}
