import cloudflare from "@astrojs/cloudflare";
import { cacheCloudflare } from "@astrojs/cloudflare/cache";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig, envField, fontProviders } from "astro/config";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

const hash = (script: string) =>
  `sha256-${createHash("sha256")
    .update(readFileSync(new URL(`./src/scripts/${script}`, import.meta.url)))
    .digest("base64")}` as const;

export default defineConfig({
  site: "https://agentpit.dev",
  env: { schema: { PUBLIC_API_URL: envField.string({ context: "client", access: "public", url: true, default: "https://api.agentpit.dev" }) } },
  adapter: cloudflare({ imageService: "compile", prerenderEnvironment: "node" }),
  session: false,
  cache: { provider: cacheCloudflare() },
  routeRules: { "/": { maxAge: 60, swr: 300 }, "/agents": { maxAge: 30, swr: 30 }, "/stats": { maxAge: 30, swr: 30 }, "/agents/[address]": { maxAge: 30, swr: 30 }, "/agents/[address]/avatar.svg": { maxAge: 86400, swr: 604800 }, "/markets": { maxAge: 30, swr: 30 }, "/markets/[tab]": { maxAge: 30, swr: 30 }, "/markets/sports/[...item]": { maxAge: 30, swr: 30 } },
  trailingSlash: "never",
  build: { format: "file" },
  devToolbar: { enabled: false },
  vite: {
    plugins: [tailwindcss()],
    build: { cssTarget: ["chrome123", "edge123", "firefox120", "safari17.5"] },
  },
  markdown: { syntaxHighlight: false },
  security: {
    csp: {
      algorithm: "SHA-256",
      scriptDirective: { hashes: [hash("copy.js"), hash("markets.js")] },
      directives: [
        "default-src 'self'",
        "img-src 'self' data: https://polymarket-upload.s3.us-east-2.amazonaws.com",
        "font-src 'self'",
        "connect-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
      ],
    },
  },
  fonts: [
    {
      provider: fontProviders.fontsource(),
      name: "Geist",
      cssVariable: "--font-geist",
      weights: ["400 500"],
      subsets: ["latin"],
      styles: ["normal"],
      fallbacks: ["ui-sans-serif", "system-ui", "sans-serif"],
    },
    {
      provider: fontProviders.fontsource(),
      name: "Geist Mono",
      cssVariable: "--font-geist-mono",
      weights: [400],
      subsets: ["latin"],
      styles: ["normal"],
      fallbacks: ["ui-monospace", "monospace"],
    },
  ],
});
