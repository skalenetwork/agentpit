{
  // Carry the campaign tags of the link a visitor arrived by (ref, utm_*) on to
  // the site's own pages and into the console, where sign-in saves them with
  // the account. A visit from another site with no utm_source gets a one-word
  // name for that site instead, never its address. Read from the address and
  // the referrer only: no cookie, no storage.
  const keys = ["ref", "utm_source", "utm_medium", "utm_campaign"];
  const tag = /^(?:[A-Za-z0-9_.-]{1,40}|0x[0-9a-fA-F]{40})$/;
  const sites = { x: /^t\.co$|(^|\.)(x|twitter)\.com$/, hn: /^news\.ycombinator\.com$/, reddit: /(^|\.)reddit\.com$/, linkedin: /(^|\.)linkedin\.com$|^lnkd\.in$/, github: /(^|\.)github\.com$/, google: /(^|\.)google\.[a-z.]+$/, bing: /(^|\.)bing\.com$/, duckduckgo: /(^|\.)duckduckgo\.com$/, chatgpt: /(^|\.)chatgpt\.com$/, perplexity: /(^|\.)perplexity\.ai$/ };
  const here = new URLSearchParams(location.search);
  const from = /^https?:/.test(document.referrer) ? new URL(document.referrer).hostname : "";
  if (from && from !== location.hostname && !/(^|\.)agentpit\.dev$/.test(from) && !here.has("utm_source")) {
    here.set("utm_source", Object.entries(sites).find(([, host]) => host.test(from))?.[0] ?? "other");
    if (!here.has("utm_medium")) here.set("utm_medium", "referral");
  }
  const found = keys.filter((key) => tag.test(here.get(key) ?? ""));
  if (found.length) {
    for (const link of document.querySelectorAll('a[href^="/"]:not([href^="//"]), a[href^="https://app.agentpit.dev"]')) {
      const url = new URL(link.href);
      for (const key of found) if (!url.searchParams.has(key)) url.searchParams.set(key, here.get(key));
      link.href = url.href;
    }
  }
}
