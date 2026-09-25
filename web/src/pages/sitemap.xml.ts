import type { APIRoute } from "astro";

export const GET: APIRoute = ({ site }) => {
  const urls = ["/", "/start", "/agents"].map((path) => `<url><loc>${new URL(path, site).href}</loc></url>`).join("");
  return new Response(
    `<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">${urls}</urlset>`,
    { headers: { "content-type": "application/xml; charset=utf-8" } },
  );
};
