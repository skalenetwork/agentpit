{
  const tabs = document.getElementById("x-tabs");
  const nearest = { block: "nearest", inline: "nearest" };
  document.fonts.ready.then(() => tabs.querySelector("[aria-current]")?.scrollIntoView({ block: "nearest", inline: "center" }));
  tabs.addEventListener("focusin", (event) => event.target.scrollIntoView(nearest));
  const status = document.getElementById("x-status");
  const form = document.getElementById("x-search");
  const input = form.elements.q;
  const still = matchMedia("(prefers-reduced-motion: reduce)");
  const timers = new WeakMap();
  const reveal = (root) => {
    if (navigator.clipboard) for (const button of root.querySelectorAll("button[data-copy]")) button.hidden = false;
  };
  const show = (button, done) => {
    const from = button.offsetWidth;
    button.querySelector(".x-rest").hidden = done;
    button.querySelector(".x-done").hidden = !done;
    if (!still.matches) {
      button.animate({ width: [`${from}px`, `${button.offsetWidth}px`] }, { duration: 200, easing: "cubic-bezier(0.2, 0, 0, 1)" });
    }
  };
  reveal(document);
  if (navigator.clipboard) {
    document.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-copy]");
      if (!button) return;
      const say = button.parentElement.querySelector(".x-say");
      navigator.clipboard.writeText(say.textContent).then(
        () => {
          show(button, true);
          status.textContent = "Copied";
          clearTimeout(timers.get(button));
          timers.set(
            button,
            setTimeout(() => {
              show(button, false);
              status.textContent = "";
            }, 1500),
          );
        },
        () => {
          say.hidden = false;
          getSelection().selectAllChildren(say);
        },
      );
    });
  }
  const until = (seconds) => {
    const s = Math.ceil(seconds);
    if (s < 300) return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    const [d, h, m] = [Math.floor(s / 86_400), Math.floor((s % 86_400) / 3_600), Math.floor((s % 3_600) / 60)];
    return s < 3_600 ? `${m}m` : s < 86_400 ? (m ? `${h}h ${m}m` : `${h}h`) : h ? `${d}d ${h}h` : `${d}d`;
  };
  setInterval(() => {
    const now = Date.now() / 1000;
    for (const time of document.querySelectorAll("time[data-at]")) {
      const left = time.dataset.at - now;
      if (left > 0) {
        const text = until(left);
        if (time.textContent !== text) time.textContent = text;
      } else if (time.dataset.next) {
        time.previousSibling.nodeValue = "Closes in ";
        time.dateTime = new Date(time.dataset.next * 1000).toISOString();
        time.dataset.at = time.dataset.next;
        delete time.dataset.next;
      } else {
        const card = time.closest("article");
        if (time.dataset.then === "Awaiting result") {
          card.classList.add("closed");
          card.querySelector("[data-copy]")?.remove();
        }
        time.parentElement.textContent = time.dataset.then;
      }
    }
  }, 1000);
  const away = new URL(form.action).pathname !== location.pathname;
  let wanted = input.value.trim();
  let pending;
  let timer;
  const search = async () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q === wanted) return;
    wanted = q;
    const url = new URL(form.action);
    if (q) url.searchParams.set("q", q);
    if (away) return location.assign(url);
    pending?.abort();
    pending = new AbortController();
    document.getElementById("x-results").ariaBusy = "true";
    try {
      const response = await fetch(url, { signal: pending.signal });
      if (!response.ok) throw new Error(String(response.status));
      const results = new DOMParser().parseFromString(await response.text(), "text/html").getElementById("x-results");
      document.getElementById("x-results").replaceWith(results);
      reveal(results);
      history.replaceState(null, "", url);
      status.textContent = results.dataset.status;
    } catch (error) {
      if (error.name !== "AbortError") location.assign(url);
    }
  };
  const soon = () => {
    if (away) return;
    clearTimeout(timer);
    timer = setTimeout(search, 250);
  };
  input.addEventListener("input", (event) => event.isComposing || soon());
  input.addEventListener("compositionend", soon);
  input.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !input.value) return;
    event.preventDefault();
    input.value = "";
    search();
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    search();
  });
  form.querySelector("[data-clear]").addEventListener("click", (event) => {
    event.preventDefault();
    input.value = "";
    input.focus();
    search();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey || event.target.closest("input, textarea, select, [contenteditable]")) return;
    event.preventDefault();
    input.focus();
  });
}
