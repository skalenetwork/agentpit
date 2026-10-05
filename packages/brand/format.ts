export type Tone = "up" | "down" | "flat";

export const ACTIVE_SECONDS = 1800;

export const cents = (price: number) => {
  const tenths = Math.round(price * 1000);
  return tenths > 0 && tenths < 100 ? `${(tenths / 10).toFixed(1)}¢` : `${Math.round(price * 100)}¢`;
};
export const count = (value: number) => value.toLocaleString("en-US");
export const dollars = (value: number) => `$${Math.round(value).toLocaleString("en-US")}`;
export const shortDay = (date: Date) => date.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
export const utcTime = (date: Date) => `${date.toISOString().slice(11, 16)} UTC`;
export const shortAddress = (address: string) => `${address.slice(0, 6)}…${address.slice(-4)}`;

export const tone = (value: number): Tone => {
  const hundredths = Math.round(value * 100);
  return hundredths > 0 ? "up" : hundredths < 0 ? "down" : "flat";
};

export const rankMove = (change: number | null): { readonly dir: "up" | "down"; readonly by: number } | null => (change ? { dir: change > 0 ? "up" : "down", by: Math.abs(change) } : null);

const signed = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0, signDisplay: "exceptZero" });
export const signedDollars = (value: number) => signed.format(value).replace("-", "−");

const compact = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: 1 });
export const compactDollars = (value: number) => compact.format(value).replace("-", "−");

const points = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: "exceptZero" });
export const pct = (value: number) => `${points.format(value).replace("-", "−")}%`;

const relative = new Intl.RelativeTimeFormat("en-US", { style: "narrow" });
const units = [
  ["day", 86_400],
  ["hour", 3_600],
  ["minute", 60],
] as const;
export const ago = (at: number, now: number) => {
  const elapsed = Math.max(0, now - at);
  const unit = units.find(([, size]) => elapsed >= size);
  return unit ? relative.format(-Math.floor(elapsed / unit[1]), unit[0]) : "now";
};
