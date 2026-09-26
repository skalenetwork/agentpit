import type { APIRoute } from "astro";
import { robot } from "../../../lib/robot";

export const prerender = false;

export const GET: APIRoute = ({ params }) => {
  const svg = robot(params.address ?? "");
  return svg
    ? new Response(svg, {
        headers: { "content-type": "image/svg+xml", "cache-control": "public, max-age=86400, stale-while-revalidate=604800" },
      })
    : new Response(null, { status: 404 });
};
