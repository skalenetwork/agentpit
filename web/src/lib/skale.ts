export const marks = ["divider", "pill", "stack"] as const;
export type Mark = (typeof marks)[number];
export const feet = ["strip", "banner", "facts", "column", "sticker", "line"] as const;
export type Foot = (typeof feet)[number];
export const places = ["center", "left", "right", "split", "stack", "below"] as const;
export type Place = (typeof places)[number];
