export class AuthError extends Error {
  constructor() {
    super("Unauthorized");
    this.name = "AuthError";
  }
}

export async function apiJson<T>(path: string, init?: RequestInit): Promise<T> {
  const method = init?.method ?? "GET";
  const headers: Record<string, string> = { ...(init?.headers as Record<string, string>) };
  if (method !== "GET" && method !== "HEAD" && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const r = await fetch(`/api${path}`, {
    credentials: "include",
    ...init,
    headers,
  });
  if (r.status === 401) {
    throw new AuthError();
  }
  if (!r.ok) {
    const t = await r.text();
    throw new Error(t || r.statusText);
  }
  const text = await r.text();
  return text ? (JSON.parse(text) as T) : (null as T);
}

export async function login(password: string): Promise<void> {
  await apiJson("/auth/login", { method: "POST", body: JSON.stringify({ password }) });
}

export async function logout(): Promise<void> {
  await apiJson("/auth/logout", { method: "POST", body: JSON.stringify({}) });
}

export async function authMe(): Promise<{ authenticated: boolean }> {
  return apiJson("/auth/me");
}
