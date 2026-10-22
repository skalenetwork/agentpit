import { afterEach, expect, mock, spyOn, test } from "bun:test";

mock.module("astro:env/client", () => ({ PUBLIC_API_URL: "https://api.test" }));
const { GET } = await import("../pages/competitions/[slug]/og/[slot].png.ts");

const hour = () => new Date().toISOString().slice(0, 13);
const slot = (name: string) => GET({ params: { slug: "october-test", slot: name }, cache: { set: () => undefined } } as never) as Promise<Response>;
const api = (status: number) => spyOn(globalThis, "fetch").mockResolvedValue(new Response(status === 200 ? "png" : null, { status }));

afterEach(() => mock.restore());

test("an old hour moves to the current one", async () => {
  const fetched = api(200);
  const moved = await slot("2026-10-01T00");
  expect([moved.status, moved.headers.get("location"), fetched.mock.calls.length]).toEqual([302, `/competitions/october-test/og/${hour()}.png`, 0]);
});

test("the final slot serves the final card for 30 days and moves to the hour until there is one", async () => {
  const fetched = api(200);
  const final = await slot("final");
  expect([final.status, final.headers.get("cache-control"), fetched.mock.calls[0][0]]).toEqual([200, "public, max-age=2592000", "https://api.test/competitions/october-test/card.png?final=1"]);
  mock.restore();
  api(404);
  const early = await slot("final");
  expect([early.status, early.headers.get("location")]).toEqual([302, `/competitions/october-test/og/${hour()}.png`]);
});
