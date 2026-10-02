export const cents = (price: number) => {
  const tenths = Math.round(price * 1000);
  return tenths > 0 && tenths < 100 ? `${(tenths / 10).toFixed(1)}¢` : `${Math.round(price * 100)}¢`;
};
export const count = (value: number) => value.toLocaleString("en-US");
export const dollars = (value: number) => `$${Math.round(value).toLocaleString("en-US")}`;
export const percent = (share: number) => `${Math.round(share * 100)}%`;
export const shortDay = (date: Date) => date.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
export const utcTime = (date: Date) => `${date.toISOString().slice(11, 16)} UTC`;
export const shortAddress = (address: string) => `${address.slice(0, 6)}…${address.slice(-4)}`;

const signed = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0, signDisplay: "exceptZero" });
export const signedDollars = (value: number) => signed.format(value).replace("-", "−");

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
