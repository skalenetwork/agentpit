import { defineMiddleware } from "astro:middleware";

export const onRequest = defineMiddleware(async (_, next) => {
  const response = await next();
  response.headers.set("Cloudflare-CDN-Cache-Control", "no-store");
  return response;
});
