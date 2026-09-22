import type { APIRoute, GetStaticPaths } from "astro";
import { ogCards } from "../../content/site";
import { renderOg } from "../../lib/og";

export const getStaticPaths = (() =>
  Object.entries(ogCards).map(([path, title]) => ({ params: { card: path.slice(1) }, props: { title } }))) satisfies GetStaticPaths;

export const GET: APIRoute<{ title: string }> = async ({ props }) =>
  new Response(new Uint8Array(await renderOg(props.title)), { headers: { "content-type": "image/png" } });
