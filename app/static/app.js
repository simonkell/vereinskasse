document.addEventListener("DOMContentLoaded", () => {
  const toggle = document.querySelector(".nav-toggle");
  const navigation = document.querySelector(".primary-navigation");
  if (!toggle || !navigation) return;

  const setMenu = (open) => {
    navigation.classList.toggle("is-open", open);
    toggle.classList.toggle("is-open", open);
    toggle.setAttribute("aria-expanded", String(open));
    const label = toggle.querySelector(".visually-hidden");
    if (label) label.textContent = open ? "Menü schließen" : "Menü öffnen";
  };

  toggle.addEventListener("click", () => {
    setMenu(toggle.getAttribute("aria-expanded") !== "true");
  });

  navigation.addEventListener("click", (event) => {
    if (event.target.closest("a") && window.matchMedia("(max-width: 900px)").matches) {
      setMenu(false);
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      setMenu(false);
      toggle.focus();
    }
  });

  window.addEventListener("resize", () => {
    if (!window.matchMedia("(max-width: 900px)").matches) setMenu(false);
  });
});
