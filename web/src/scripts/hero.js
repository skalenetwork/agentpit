{
  const root = document.currentScript.parentElement;
  const list = root.querySelector(".list");
  const detail = root.querySelector("[data-detail]");
  const still = matchMedia("(prefers-reduced-motion: reduce)");
  const rows = () => [...list.querySelectorAll(".row")];
  const hands = () => [...detail.querySelectorAll(".hand")];
  const row = (slug) => rows().find((r) => r.dataset.slug === slug);
  let at = rows()[0].dataset.slug;
  let timed = true;
  let timer;
  const arm = () => {
    if (navigator.clipboard) for (const button of detail.querySelectorAll("[data-copy]")) button.hidden = false;
  };
  const swap = () => hands().forEach((hand) => (hand.hidden = hand.dataset.slug !== at));
  const mark = () => rows().forEach((r) => r.querySelector("button").setAttribute("aria-pressed", r.dataset.slug === at));
  const pick = (slug, timing) => {
    const next = row(slug);
    row(at)?.classList.remove("timing");
    void next.offsetWidth;
    next.classList.toggle("timing", timing);
    timed = timing;
    if (slug === at) return;
    at = slug;
    mark();
    if (list.scrollWidth > list.clientWidth) list.scrollTo({ left: next.offsetLeft - list.offsetLeft - 4, behavior: still.matches ? "auto" : "smooth" });
    clearTimeout(timer);
    if (still.matches) return swap();
    detail.classList.add("out");
    timer = setTimeout(() => {
      swap();
      detail.classList.remove("out");
    }, 200);
  };
  const refresh = async () => {
    const response = await fetch("/hero").catch(() => null);
    if (!response?.ok) return;
    const next = new DOMParser().parseFromString(await response.text(), "text/html");
    const fresh = [...next.querySelectorAll(".row")];
    if (!fresh.length) return;
    const was = new Map(rows().map((r) => [r.dataset.slug, { box: r.getBoundingClientRect(), at: Number(r.dataset.at) }]));
    const tail = next.querySelector(".list > a");
    list.querySelector(":scope > a")?.remove();
    for (const incoming of fresh) {
      const current = row(incoming.dataset.slug);
      if (current) {
        current.dataset.at = incoming.dataset.at;
        current.replaceChildren(...incoming.childNodes);
      } else incoming.classList.remove("timing");
      list.append(current ?? incoming);
    }
    if (tail) list.append(tail);
    const kept = new Set(fresh.map((r) => r.dataset.slug));
    for (const old of rows()) if (!kept.has(old.dataset.slug)) old.remove();
    for (const incoming of next.querySelectorAll(".hand")) {
      const current = hands().find((h) => h.dataset.slug === incoming.dataset.slug);
      if (current?.dataset.at === incoming.dataset.at) continue;
      incoming.hidden = incoming.dataset.slug !== at;
      if (current) current.replaceWith(incoming);
      else detail.prepend(incoming);
    }
    for (const old of hands()) if (!kept.has(old.dataset.slug)) old.remove();
    arm();
    if (!still.matches) {
      for (const r of rows()) {
        const old = was.get(r.dataset.slug)?.box;
        const box = r.getBoundingClientRect();
        if (old && (old.top !== box.top || old.left !== box.left)) r.animate([{ translate: `${old.left - box.left}px ${old.top - box.top}px` }, { translate: "0 0" }], { duration: 400, easing: "cubic-bezier(0.2, 0, 0, 1)" });
      }
    }
    const traded = rows().filter((r) => Number(r.dataset.at) > (was.get(r.dataset.slug)?.at ?? 0));
    if (!kept.has(at)) {
      at = rows()[0].dataset.slug;
      pick(at, true);
      swap();
    } else if (timed && traded.length) pick(traded[0].dataset.slug, true);
    mark();
  };
  list.addEventListener("animationend", (event) => {
    const all = rows();
    if (event.target === row(at)) pick(all[(all.indexOf(event.target) + 1) % all.length].dataset.slug, true);
  });
  list.addEventListener("click", (event) => {
    const clicked = event.target.closest(".row");
    if (clicked) pick(clicked.dataset.slug, false);
  });
  detail.addEventListener("click", (event) => {
    const button = event.target.closest("[data-copy]");
    if (!button) return;
    const say = button.parentElement.querySelector(".x-say");
    navigator.clipboard.writeText(say.textContent).then(
      () => {
        button.querySelector(".x-rest").hidden = true;
        button.querySelector(".x-done").hidden = false;
        setTimeout(() => {
          button.querySelector(".x-rest").hidden = false;
          button.querySelector(".x-done").hidden = true;
        }, 1500);
      },
      () => {
        say.hidden = false;
        getSelection().selectAllChildren(say);
      },
    );
  });
  let seen = false;
  new IntersectionObserver(([entry]) => (seen = entry.isIntersecting)).observe(root);
  setInterval(() => !document.hidden && seen && refresh(), 30_000);
  arm();
}
