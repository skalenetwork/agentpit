import { Avatar, Style } from "@dicebear/core";
import glyphs from "@dicebear/styles/glyphs.json";

const style = new Style(glyphs);

export default ({ seed, size }: { seed: string; size: number }) => (
  <img src={new Avatar(style, { seed }).toDataUri()} width={size} height={size} alt="" className="shrink-0 rounded-control bg-surface" />
);
