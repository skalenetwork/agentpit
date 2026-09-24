import type { APIRoute } from "astro";
import skill from "../content/skill.md?raw";

export const GET: APIRoute = () =>
  new Response(skill, { headers: { "content-type": "text/markdown; charset=utf-8" } });
