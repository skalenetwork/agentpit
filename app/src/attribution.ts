// The campaign tags of the link a person arrived by (agentpit.dev adds them to
// every link into the console), sent with the sign-in so the account records
// where the person came from. No cookie. Only the Google path keeps them, in
// the OAuth state in session storage, while the redirect is in flight.

const TAG = /^(?:[A-Za-z0-9_.-]{1,40}|0x[0-9a-fA-F]{40})$/;
const KEYS = ["ref", "utm_source", "utm_medium", "utm_campaign"] as const;

export type Attribution = Partial<Record<(typeof KEYS)[number], string>>;

const clean = (read: (key: string) => unknown): Attribution | undefined => {
  const tags: Attribution = {};
  for (const key of KEYS) {
    const value = read(key);
    if (typeof value === "string" && TAG.test(value)) tags[key] = value;
  }
  return Object.keys(tags).length ? tags : undefined;
};

export const readAttribution = (search: string) => {
  const params = new URLSearchParams(search);
  return clean((key) => params.get(key));
};

// Google sign-in leaves the console and comes back, so the tags ride in the
// OAuth state after the nonce: `<nonce>.<base64url(json)>`.
export const withAttribution = (nonce: string, tags: Attribution | undefined) => (tags ? `${nonce}.${btoa(JSON.stringify(tags)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")}` : nonce);

export const attributionOf = (state: string | null) => {
  const packed = state?.split(".")[1];
  if (!packed) return undefined;
  try {
    const tags: unknown = JSON.parse(atob(packed.replace(/-/g, "+").replace(/_/g, "/")));
    return tags && typeof tags === "object" ? clean((key) => (tags as Record<string, unknown>)[key]) : undefined;
  } catch {
    return undefined;
  }
};

// Read once when the console loads, before the router rewrites the address.
export const landing = typeof location === "undefined" ? undefined : readAttribution(location.search);
