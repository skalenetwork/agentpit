{
  const article = document.currentScript.previousElementSibling;
  const nav = article.previousElementSibling;
  const header = document.querySelector("header");
  const links = [...nav.querySelectorAll("a")];
  const heads = links.map((link) => document.getElementById(link.hash.slice(1)));
  const spy = () => {
    const edges = [...heads.map((head) => head.getBoundingClientRect().top), article.getBoundingClientRect().bottom];
    links.forEach((link, i) => link.toggleAttribute("data-visible", edges[i] < innerHeight && edges[i + 1] > header.offsetHeight));
  };
  addEventListener("scroll", spy, { passive: true });
  addEventListener("resize", spy);
  spy();
}
