import { Avatar, Style } from "@dicebear/core";
import definition from "@dicebear/styles/bottts-neutral.json" with { type: "json" };

const style = new Style(definition);

export const robot = (address: string): string | undefined =>
  /^0x[0-9a-fA-F]{40}$/.test(address) ? new Avatar(style, { seed: address.toLowerCase() }).toString() : undefined;
