import { readFileSync } from "node:fs";
import { Resvg } from "@resvg/resvg-js";
import satori from "satori";

const W = 1200;
const H = 630;
const INK = "#0F1A44";
const LOGO_H = 80;

const dataUri = (mime: string, data: Buffer) => `data:${mime};base64,${data.toString("base64")}`;

const base = dataUri("image/png", readFileSync("src/assets/og-base.png"));
const logo = dataUri(
  "image/svg+xml",
  Buffer.from(readFileSync("src/assets/logo.svg", "utf8").replace('fill="currentColor"', `fill="${INK}"`)),
);
const geist = readFileSync("node_modules/@fontsource/geist/files/geist-latin-500-normal.woff");

const el = (type: string, style: Record<string, string | number>, children?: unknown, src?: string) => ({
  type,
  props: { style, children, src, width: style.width, height: style.height },
});

export async function renderOg(title: string): Promise<Buffer> {
  const svg = await satori(
    el(
      "div",
      {
        width: W,
        height: H,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        backgroundImage: `url(${base})`,
        backgroundSize: `${W}px ${H}px`,
      },
      [
        el("img", { width: (LOGO_H * 857) / 156, height: LOGO_H, position: "relative", top: 9 }, undefined, logo),
        el("div", { width: 3, height: LOGO_H * 1.1, background: INK, margin: "0 40px" }),
        el("div", { fontSize: 84, lineHeight: 1, letterSpacing: "-0.03em", color: INK }, title),
      ],
    ) as never,
    { width: W, height: H, fonts: [{ name: "Geist", data: geist, weight: 500, style: "normal" }] },
  );
  return new Resvg(svg, { fitTo: { mode: "width", value: W } }).render().asPng();
}
