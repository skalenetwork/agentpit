import type { APIRoute } from "astro";
import { site } from "../../../../content/site";

export const prerender = false;

export const GET: APIRoute = async ({ params, cache }) => {
  const slug = params.slug ?? "";
  if (!/^[a-z0-9-]{1,64}$/.test(slug)) return new Response(null, { status: 404 });
  const hour = new Date().toISOString().slice(0, 13);
  const current = new Response(null, { status: 302, headers: { location: `/competitions/${slug}/og/${hour}.png`, "cache-control": "no-store" } });
  const final = params.slot === "final";
  if (!final && params.slot !== hour) return current;
  const card = await fetch(`${site.api}/competitions/${slug}/card.png${final ? "?final=1" : ""}`, { signal: AbortSignal.timeout(5000) }).catch(() => undefined);
  if (final && card?.status === 404) return current;
  if (!card?.ok) return new Response(null, { status: card?.status === 404 ? 404 : 503, headers: { "cache-control": "no-store" } });
  const maxAge = final ? 2_592_000 : 86_400;
  cache.set({ maxAge });
  return new Response(card.body, { headers: { "content-type": "image/png", "cache-control": `public, max-age=${maxAge}` } });
};
