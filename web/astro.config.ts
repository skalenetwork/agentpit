import cloudflare from "@astrojs/cloudflare";
import { cacheCloudflare } from "@astrojs/cloudflare/cache";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig, fontProviders } from "astro/config";

export default defineConfig({
  site: process.env.SITE_URL ?? "https://agentpit.dev",
  adapter: cloudflare({ prerenderEnvironment: "node" }),
  session: false,
  cache: { provider: cacheCloudflare() },
  routeRules: { "/": { maxAge: 60, swr: 300 } },
  trailingSlash: "never",
  build: { format: "file" },
  devToolbar: { enabled: false },
  vite: { plugins: [tailwindcss()] },
  markdown: { syntaxHighlight: false },
  security: {
    csp: {
      algorithm: "SHA-256",
      directives: [
        "default-src 'self'",
        "img-src 'self' data:",
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
