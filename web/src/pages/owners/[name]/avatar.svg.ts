import type { APIRoute } from "astro";
import { person } from "../../../lib/robot";

export const prerender = false;

export const GET: APIRoute = ({ params }) => {
  const svg = person(params.name ?? "");
  return svg
    ? new Response(svg, {
        headers: { "content-type": "image/svg+xml", "cache-control": "public, max-age=86400, stale-while-revalidate=604800" },
      })
    : new Response(null, { status: 404 });
};
