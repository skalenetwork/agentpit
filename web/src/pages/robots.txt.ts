import type { APIRoute } from "astro";
import { robots } from "../lib/seo";

export const GET: APIRoute = ({ site }) =>
  new Response(robots(new URL("/sitemap.xml", site).href), {
    headers: { "content-type": "text/plain; charset=utf-8" },
  });
