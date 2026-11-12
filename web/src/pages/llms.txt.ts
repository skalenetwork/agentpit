import type { APIRoute } from "astro";
import { llms } from "../content/llms";

export const GET: APIRoute = () => new Response(llms, { headers: { "content-type": "text/plain; charset=utf-8" } });
