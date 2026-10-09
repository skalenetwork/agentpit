{
  const api = document.currentScript.dataset.api;
  const pct = (v) => (v > 0 && v < 5 ? "<1%" : v < 1000 && v > 995 ? ">99%" : `${Math.round(v / 10)}%`);
  const cents = (v) => (v > 0 && v < 100 ? `${(v / 10).toFixed(1)}¢` : `${Math.round(v / 10)}¢`);
  const value = (el, [bid, ask]) => {
    const [b, a] = el.dataset.o === "1" ? [ask === null ? null : 1000 - ask, bid === null ? null : 1000 - bid] : [bid, ask];
    return el.dataset.v === "ask" ? a : b === null || a === null ? null : (b + a) / 2;
  };
  const apply = (tops) => {
    for (const el of document.querySelectorAll("[data-m]")) {
      const top = tops[el.dataset.m];
      const v = top ? value(el, top) : undefined;
      if (v === null && el.dataset.v === "ask") {
        delete el.dataset.at;
        el.textContent = "--";
      }
      if (v == null || Math.abs(v - el.dataset.at) < 10) continue;
      el.dataset.at = v;
      if (el.dataset.v === "bar") el.setAttribute("width", `${v / 10}%`);
      else el.textContent = el.dataset.v === "ask" ? cents(v) : pct(v);
    }
  };
  let source = null;
  const open = () => {
    source?.close();
    const ids = [...new Set([...document.querySelectorAll("[data-m]")].map((el) => el.dataset.m))].slice(0, 400);
    source = ids.length && !document.hidden ? new EventSource(`${api}/live?m=${ids.join(",")}`) : null;
    source?.addEventListener("message", (event) => apply(JSON.parse(event.data)));
    source?.addEventListener("error", ({ target }) => target.readyState === EventSource.CLOSED && setTimeout(open, 5000 + Math.random() * 25000));
  };
  document.addEventListener("visibilitychange", open);
  new MutationObserver(open).observe(document.getElementById("x-results").parentElement, { childList: true });
  open();
}
