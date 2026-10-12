import { Avatar, Style } from "@dicebear/core";
import bottts from "@dicebear/styles/bottts-neutral.json" with { type: "json" };
import glyphs from "@dicebear/styles/glyphs.json" with { type: "json" };

const draw = (style: Style, shape: RegExp) => (id: string): string | undefined =>
  shape.test(id) ? new Avatar(style, { seed: id.toLowerCase() }).toString() : undefined;

export const robot = draw(new Style(bottts), /^0x[0-9a-fA-F]{40}$/);
export const person = draw(new Style(glyphs), /^\w{1,15}$/);
