import type { APIRoute } from "astro";
import { indexable } from "../content/site";

export const GET: APIRoute = ({ site }) =>
  new Response(
    indexable
      ? `User-agent: *\nAllow: /\n\nSitemap: ${new URL("/sitemap.xml", site).href}\n`
      : "User-agent: *\nDisallow: /\n",
    { headers: { "content-type": "text/plain; charset=utf-8" } },
  );
