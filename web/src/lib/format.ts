export const cents = (price: number) => `${Math.round(price * 100)}¢`;
export const count = (value: number) => value.toLocaleString("en-US");
export const dollars = (value: number) => `$${Math.round(value).toLocaleString("en-US")}`;

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
