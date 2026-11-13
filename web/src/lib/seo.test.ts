import { expect, test } from "bun:test";
import type { RaceHead } from "@agentpit/brand/api";
import { competition, crawlers, faq, graph, robots, sitemap } from "./seo";

const site = {
  url: "https://agentpit.dev/",
  name: "AgentPit",
  alternateName: ["Agent Pit"],
  description: "Paper trading.",
  logo: "https://agentpit.dev/apple-touch-icon.png",
  sameAs: ["https://github.com/skalenetwork/agentpit"],
  parent: { name: "SKALE", url: "https://skale.space" },
};

const race: RaceHead = {
  slug: "kickoff-clash-1",
  title: "Kickoff Clash 1",
  startsAt: 1_762_041_600,
  endsAt: 1_763_251_200,
  phase: "live",
  final: false,
  prizes: [
    { usd: 200, skl: 0 },
    { usd: 125, skl: 0 },
  ],
  entrants: 21,
  ranked: 12,
  line: "21 agents trade live Polymarket books for $325.",
};

test("a market title can never close the JSON-LD script tag", () => {
  const body = graph([faq([{ term: "</script><script>alert(1)</script>?", def: "x" }])]);
  expect(body).not.toContain("</script>");
  expect(JSON.parse(body)["@graph"][0].mainEntity[0].name).toBe("</script><script>alert(1)</script>?");
});

test("a competition is an online event with ISO dates and its prize pool", () => {
  const node = competition(site, race, "https://agentpit.dev/competitions/kickoff-clash-1", "https://agentpit.dev/og.png");
  expect(node.startDate).toBe("2025-11-02T00:00:00.000Z");
  expect(node.eventAttendanceMode).toBe("https://schema.org/OnlineEventAttendanceMode");
  expect(node.eventStatus).toBe("https://schema.org/EventScheduled");
  expect(node.description).toBe("21 agents trade live Polymarket books for $325. $325 in prizes.");
  expect(node).not.toHaveProperty("award");
  expect(competition(site, { ...race, phase: "ended", prizes: [] }, "u", "i").description).toBe(race.line);
});

test("robots welcomes every listed crawler and points at the sitemap", () => {
  const txt = robots("https://agentpit.dev/sitemap.xml");
  for (const agent of crawlers) expect(txt).toContain(`User-agent: ${agent}\n`);
  expect(txt).not.toContain("Disallow");
  expect(txt.trimEnd().endsWith("Sitemap: https://agentpit.dev/sitemap.xml")).toBe(true);
});

test("sitemap escapes locations and writes only the fields it has", () => {
  const xml = sitemap([{ loc: "https://agentpit.dev/?a=1&b=2" }, { loc: "https://agentpit.dev/about", lastmod: "2026-11-12", changefreq: "monthly", priority: 0.9 }]);
  expect(xml).toContain("<url><loc>https://agentpit.dev/?a=1&amp;b=2</loc></url>");
  expect(xml).toContain("<lastmod>2026-11-12</lastmod><changefreq>monthly</changefreq><priority>0.9</priority>");
});
