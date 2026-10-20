{
  const pad = (n) => String(n).padStart(2, "0");
  const tick = () => {
    const now = Date.now() / 1000;
    for (const time of document.querySelectorAll("time[data-countdown]")) {
      const s = Math.max(0, Math.ceil(time.dataset.countdown - now));
      const [d, h, m] = [Math.floor(s / 86_400), Math.floor((s % 86_400) / 3_600), Math.floor((s % 3_600) / 60)];
      time.textContent = `${d ? `${d}d ${pad(h)}` : h}:${pad(m)}:${pad(s % 60)}`;
    }
    setTimeout(tick, 1000 - (Date.now() % 1000));
  };
  if (!window.countdown) {
    window.countdown = true;
    tick();
  }
}
