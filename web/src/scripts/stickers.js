{
  const pull = (d) => Math.sign(d) * 36 * (1 - Math.exp(-Math.abs(d) / 90));
  const calm = matchMedia("(prefers-reduced-motion: reduce)");
  const swallow = (event) => {
    event.preventDefault();
    event.stopPropagation();
  };
  let held = null;

  const release = () => {
    if (!held) return;
    const { el, moved } = held;
    held = null;
    el.classList.remove("held");
    el.style.transition = `transform ${calm.matches ? "200ms ease" : "560ms cubic-bezier(0.34, 1.56, 0.64, 1)"}, scale 420ms cubic-bezier(0.22, 1, 0.36, 1)`;
    el.style.transform = "";
    setTimeout(() => held?.el !== el && (el.style.transition = ""), 560);
    if (moved) {
      addEventListener("click", swallow, { capture: true, once: true });
      setTimeout(() => removeEventListener("click", swallow, { capture: true }));
    }
  };

  if (!window.stickers) {
    window.stickers = true;
    addEventListener("pointerdown", (event) => {
      const el = event.button === 0 && event.target.closest?.(".sticker");
      if (!el) return;
      event.preventDefault();
      el.setPointerCapture(event.pointerId);
      el.style.transition = "scale 420ms cubic-bezier(0.22, 1, 0.36, 1)";
      el.classList.add("held");
      held = { el, x: event.clientX, y: event.clientY, moved: false };
    });
    addEventListener("pointermove", (event) => {
      if (!held) return;
      const dx = pull(event.clientX - held.x);
      const dy = pull(event.clientY - held.y);
      held.moved ||= Math.hypot(dx, dy) > 2;
      held.el.style.transform = `translate(${dx}px, ${dy}px) rotate(${dx / 3}deg)`;
    });
    addEventListener("pointerup", release);
    addEventListener("pointercancel", release);
    addEventListener("dragstart", (event) => held && event.preventDefault());
  }
}
