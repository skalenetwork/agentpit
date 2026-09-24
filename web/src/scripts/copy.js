{
  const prompt = document.currentScript.parentElement;
  const text = prompt.querySelector("p");
  const button = prompt.querySelector("button");
  const still = matchMedia("(prefers-reduced-motion: reduce)");
  let timer;
  const label = (value) => {
    const from = button.offsetWidth;
    button.textContent = value;
    if (!still.matches) {
      button.animate({ width: [`${from}px`, `${button.offsetWidth}px`] }, { duration: 200, easing: "cubic-bezier(0.2, 0, 0, 1)" });
    }
  };
  if (navigator.clipboard) {
    button.parentElement.hidden = false;
    button.addEventListener("click", () =>
      navigator.clipboard.writeText(text.textContent).then(
        () => {
          label("Copied");
          clearTimeout(timer);
          timer = setTimeout(() => label("Copy"), 1500);
        },
        () => getSelection().selectAllChildren(text),
      ),
    );
  }
}
