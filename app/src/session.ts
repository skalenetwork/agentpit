const BASE = (import.meta.env.VITE_API_BASE_URL || "http://localhost:8000").replace(/\/+$/, "");
const ACCESS = "agentpit.access_token";
const REFRESH = "agentpit.refresh_token";

export interface Me {
  readonly user_id: string;
  readonly email: string | null;
  readonly created_at: number;
}

export interface Session {
  readonly access_token: string;
  readonly refresh_token: string | null;
}

export class ApiError extends Error {
  constructor(readonly status: number) {
    super(`Request failed: ${status}`);
  }
}

const changes = new EventTarget();
const emit = () => changes.dispatchEvent(new Event("change"));
addEventListener("storage", emit);

const store = (key: string, value: string | null) => (value ? localStorage.setItem(key, value) : localStorage.removeItem(key));
const set = (access: string | null, refresh: string | null) => {
  store(ACCESS, access);
  store(REFRESH, refresh);
  emit();
};

export const subscribe = (listener: () => void) => changes.addEventListener("change", listener);
export const signedIn = () => localStorage.getItem(ACCESS) !== null;
export const start = (session: Session) => set(session.access_token, session.refresh_token ?? localStorage.getItem(REFRESH));
export const end = () => set(null, null);

const send = (path: string, method: string, body: unknown, token: string | null) =>
  fetch(`${BASE}${path}`, {
    method,
    headers: { Accept: "application/json", ...(body !== undefined && { "Content-Type": "application/json" }), ...(token && { Authorization: `Bearer ${token}` }) },
    ...(body !== undefined && { body: JSON.stringify(body) }),
  });

const parse = async <T>(response: Response): Promise<T> => {
  if (!response.ok) throw new ApiError(response.status);
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
};

export const open = async <T>(path: string, method = "GET", body?: unknown) => parse<T>(await send(path, method, body, null));

let renewing: Promise<string | null> | undefined;

const renew = () => {
  const refresh = localStorage.getItem(REFRESH);
  if (!refresh) {
    end();
    return Promise.resolve(null);
  }
  renewing ??= open<Session>("/auth/refresh", "POST", { refresh_token: refresh })
    .then((session) => {
      if (localStorage.getItem(REFRESH) !== refresh) return null;
      start(session);
      return session.access_token;
    })
    .catch((error: unknown) => {
      if (error instanceof ApiError && error.status === 401) end();
      return null;
    })
    .finally(() => {
      renewing = undefined;
    });
  return renewing;
};

export const request = async <T>(path: string, method = "GET", body?: unknown) => {
  const token = localStorage.getItem(ACCESS);
  const response = await send(path, method, body, token);
  const fresh = response.status === 401 && token ? await renew() : null;
  if (!fresh) return parse<T>(response);
  const replay = await send(path, method, body, fresh);
  if (replay.status === 401) end();
  return parse<T>(replay);
};
