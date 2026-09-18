// JSON API wrapper. Every request is same-origin and cookie-authenticated.

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

let unauthorized = () => {};
export const onUnauthorized = (fn) => { unauthorized = fn; };
export const signalUnauthorized = () => unauthorized();

function detail(data) {
  if (!data) return "";
  if (typeof data.detail === "string") return data.detail;
  if (Array.isArray(data.detail) && data.detail[0]?.msg) return data.detail[0].msg;
  return "";
}

export async function api(path, { method = "GET", body, params, signal } = {}) {
  let url = path;
  if (params) {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      for (const v of Array.isArray(value) ? value : [value]) {
        if (v !== undefined && v !== null && v !== "" && v !== false) query.append(key, v);
      }
    }
    const qs = query.toString();
    if (qs) url += `?${qs}`;
  }
  let response;
  try {
    response = await fetch(url, {
      method,
      signal,
      credentials: "same-origin",
      headers: body !== undefined ? { "Content-Type": "application/json" } : {},
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (err) {
    if (err.name === "AbortError") throw err;
    throw new ApiError(0, "Can't reach the dashboard server");
  }
  const isJson = response.headers.get("content-type")?.includes("json");
  const data = isJson ? await response.json().catch(() => null) : null;
  if (response.status === 401 && !path.startsWith("/api/auth/")) {
    unauthorized();
    throw new ApiError(401, "Signed out");
  }
  if (!response.ok) throw new ApiError(response.status, detail(data) || response.statusText || "Request failed");
  return data;
}
