{
  const root = document.currentScript.parentElement;
  const list = root.querySelector(".list");
  const rows = [...list.querySelectorAll("button")];
  const detail = root.querySelector("[data-detail]");
  const hands = [...detail.querySelectorAll(".hand")];
  const still = matchMedia("(prefers-reduced-motion: reduce)");
  let at = 0;
  let timer;
  const swap = () => hands.forEach((hand, i) => (hand.hidden = i !== at));
  const pick = (i, timed) => {
    rows[at].classList.remove("timing");
    rows[i].classList.toggle("timing", timed);
    if (i === at) return;
    rows[at].setAttribute("aria-pressed", "false");
    rows[i].setAttribute("aria-pressed", "true");
    at = i;
    if (list.scrollWidth > list.clientWidth) list.scrollTo({ left: rows[i].offsetLeft - list.offsetLeft - 4, behavior: still.matches ? "auto" : "smooth" });
    clearTimeout(timer);
    if (still.matches) return swap();
    detail.classList.add("out");
    timer = setTimeout(() => {
      swap();
      detail.classList.remove("out");
    }, 200);
  };
  list.addEventListener("animationend", (event) => {
    if (event.target === rows[at]) pick((at + 1) % rows.length, true);
  });
  list.addEventListener("click", (event) => {
    const row = event.target.closest("button");
    if (row) pick(rows.indexOf(row), false);
  });
  if (navigator.clipboard) {
    for (const button of detail.querySelectorAll("[data-copy]")) button.hidden = false;
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
  }
}
