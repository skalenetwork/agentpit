{
  // Carry the campaign tags of the link a visitor arrived by (ref, utm_*) on to
  // the site's own pages and into the console, where sign-in saves them with
  // the account. Read from the address only: no cookie, no storage.
  const keys = ["ref", "utm_source", "utm_medium", "utm_campaign"];
  const tag = /^(?:[A-Za-z0-9_.-]{1,40}|0x[0-9a-fA-F]{40})$/;
  const here = new URLSearchParams(location.search);
  const found = keys.filter((key) => tag.test(here.get(key) ?? ""));
  if (found.length) {
    for (const link of document.querySelectorAll('a[href^="/"]:not([href^="//"]), a[href^="https://app.agentpit.dev"]')) {
      const url = new URL(link.href);
      for (const key of found) if (!url.searchParams.has(key)) url.searchParams.set(key, here.get(key));
      link.href = url.href;
    }
  }
}
