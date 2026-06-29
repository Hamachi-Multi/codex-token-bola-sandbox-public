export async function getJSON(path) {
  const res = await fetch(path);
  const text = await res.text();
  let parsed = {};
  try {
    parsed = text ? JSON.parse(text) : {};
  } catch {
    parsed = { error: text };
  }
  if (!res.ok) {
    const error = new Error(parsed.message || parsed.error || text || res.statusText);
    error.status = res.status;
    error.code = parsed.error || '';
    throw error;
  }
  return parsed;
}

export async function postJSON(path, body = null) {
  const init = { method: 'POST' };
  if (body !== null) {
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(body);
  }
  const res = await fetch(path, init);
  const text = await res.text();
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { error: text };
  }
  if (!res.ok) {
    const error = new Error(data.message || data.error || text || res.statusText);
    Object.assign(error, data);
    error.status = res.status;
    error.code = data.error || '';
    throw error;
  }
  return data;
}

export function isServiceBusyError(err) {
  return Number((err || {}).status) === 409 && String((err || {}).code || '') === 'analysis_or_cleanup_running';
}
