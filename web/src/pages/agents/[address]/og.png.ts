import type { APIRoute } from "astro";

export const prerender = false;

export const GET: APIRoute = ({ params }) => {
  const address = params.address ?? "";
  if (!/^0x[0-9a-fA-F]{40}$/.test(address)) return new Response(null, { status: 404 });
  return new Response(null, { status: 302, headers: { location: `/agents/${address}/og/${new Date().toISOString().slice(0, 13)}.png`, "cache-control": "no-store" } });
};
