import type { APIRoute } from "astro";

export const GET: APIRoute = ({ site }) =>
  new Response(
    site?.host === "agentpit.dev"
      ? `User-agent: *\nAllow: /\n\nSitemap: ${new URL("/sitemap.xml", site).href}\n`
      : "User-agent: *\nDisallow: /\n",
    { headers: { "content-type": "text/plain; charset=utf-8" } },
  );
