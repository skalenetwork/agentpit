import type { APIRoute } from "astro";
import { site } from "../../../content/site";

export const prerender = false;

export const GET: APIRoute = async ({ params, cache }) => {
  const address = params.address ?? "";
  if (!/^0x[0-9a-fA-F]{40}$/.test(address)) return new Response(null, { status: 404 });
  const card = await fetch(`${site.api}/agents/${address}/card.png`, { signal: AbortSignal.timeout(5000) }).catch(() => undefined);
  if (!card?.ok) return new Response(null, { status: card?.status === 404 ? 404 : 503, headers: { "cache-control": "no-store" } });
  cache.set({ maxAge: 604800, swr: 2592000 });
  return new Response(card.body, {
    headers: { "content-type": "image/png", "cache-control": "public, max-age=604800, stale-while-revalidate=2592000" },
  });
};
