import type { APIRoute } from "astro";
import skill from "../content/skill.md?raw";

export const GET: APIRoute = () => new Response(skill);
