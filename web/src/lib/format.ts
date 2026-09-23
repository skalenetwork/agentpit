export const cents = (price: number) => `${Math.round(price * 100)}¢`;
export const shares = (size: number) => size.toLocaleString("en-US");
export const dollars = (value: number) => `$${Math.round(value).toLocaleString("en-US")}`;
