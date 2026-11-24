import { expect, test } from "bun:test";

const script = await Bun.file(new URL("./tags.js", import.meta.url)).text();

const consoleLink = (address: string, referrer: string) => {
  const link = { href: "https://app.agentpit.dev/" };
  new Function("location", "document", script)(new URL(address), { referrer, querySelectorAll: () => [link] });
  return new URL(link.href).searchParams.toString();
};

test("a visit from another site carries a one-word name for it", () => {
  expect(consoleLink("https://agentpit.dev/", "https://t.co/")).toBe("utm_source=x&utm_medium=referral");
  expect(consoleLink("https://agentpit.dev/", "https://www.google.co.uk/")).toBe("utm_source=google&utm_medium=referral");
  expect(consoleLink("https://agentpit.dev/", "https://intranet.example.corp/")).toBe("utm_source=other&utm_medium=referral");
  expect(consoleLink("https://agentpit.dev/?ref=kickoff-clash-2", "https://old.reddit.com/")).toBe(
    "ref=kickoff-clash-2&utm_source=reddit&utm_medium=referral",
  );
});

test("a tagged link, an own page, an app or no referrer adds nothing", () => {
  expect(consoleLink("https://agentpit.dev/?utm_source=newsletter", "https://t.co/")).toBe("utm_source=newsletter");
  expect(consoleLink("https://agentpit.dev/", "https://app.agentpit.dev/")).toBe("");
  expect(consoleLink("https://agentpit.dev/", "android-app://com.google.android.gm/")).toBe("");
  expect(consoleLink("https://agentpit.dev/", "")).toBe("");
});
