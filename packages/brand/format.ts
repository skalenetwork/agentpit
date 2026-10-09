export type Tone = "up" | "down" | "flat";

export const ACTIVE_SECONDS = 1800;

export const cents = (price: number) => {
  const tenths = Math.round(price * 1000);
  return tenths > 0 && tenths < 100 ? `${(tenths / 10).toFixed(1)}¢` : `${Math.round(price * 100)}¢`;
};
export const count = (value: number) => value.toLocaleString("en-US");
export const chance = (price: number | null) =>
  price === null ? "No book" : price > 0 && price < 0.005 ? "<1%" : price < 1 && price > 0.995 ? ">99%" : `${Math.round(price * 100)}%`;
export const dollars = (value: number) => `$${Math.round(value).toLocaleString("en-US")}`;
export const shortDay = (date: Date, timeZone = "UTC") => date.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone });
export const ET = "America/New_York";
const year = (at: number) => new Date(at * 1000).toLocaleDateString("en-US", { year: "numeric", timeZone: ET });
export const etDay = (at: number, now: number) =>
  new Date(at * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric", year: year(at) === year(now) ? undefined : "numeric", timeZone: ET });
export const zoneTime = (date: Date, timeZone: string) => date.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone, timeZoneName: "short" });
export const utcTime = (date: Date) => zoneTime(date, "UTC");
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

export const until = (seconds: number) => {
  const s = Math.ceil(seconds);
  if (s < 300) return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  const [d, h, m] = [Math.floor(s / 86_400), Math.floor((s % 86_400) / 3_600), Math.floor((s % 3_600) / 60)];
  return s < 3_600 ? `${m}m` : s < 86_400 ? (m ? `${h}h ${m}m` : `${h}h`) : h ? `${d}d ${h}h` : `${d}d`;
};
