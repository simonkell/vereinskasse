document.addEventListener("DOMContentLoaded", () => {
  const toggle = document.querySelector(".nav-toggle");
  const navigation = document.querySelector(".primary-navigation");
  const closeButton = document.querySelector(".nav-close");
  const backdrop = document.querySelector(".nav-backdrop");
  const settingsMenu = document.querySelector(".settings-menu");
  const settingsToggle = document.querySelector(".settings-toggle");
  if (!toggle || !navigation) return;

  const mobile = () => window.matchMedia("(max-width: 900px)").matches;

  const setMenu = (open) => {
    navigation.classList.toggle("is-open", open);
    backdrop?.classList.toggle("is-open", open);
    toggle.classList.toggle("is-open", open);
    document.body.classList.toggle("nav-open", open);
    toggle.setAttribute("aria-expanded", String(open));
    const label = toggle.querySelector(".visually-hidden");
    if (label) label.textContent = open ? "Menü schließen" : "Menü öffnen";
    if (open) closeButton?.focus();
  };

  const setSettings = (open) => {
    settingsMenu?.classList.toggle("is-open", open);
    settingsToggle?.setAttribute("aria-expanded", String(open));
  };

  toggle.addEventListener("click", () => {
    setMenu(toggle.getAttribute("aria-expanded") !== "true");
  });
  closeButton?.addEventListener("click", () => setMenu(false));
  backdrop?.addEventListener("click", () => setMenu(false));
  settingsToggle?.addEventListener("click", () => {
    if (!mobile()) setSettings(settingsToggle.getAttribute("aria-expanded") !== "true");
  });

  navigation.addEventListener("click", (event) => {
    if (event.target.closest("a") && mobile()) {
      setMenu(false);
    }
  });

  document.addEventListener("click", (event) => {
    if (!mobile() && settingsMenu && !settingsMenu.contains(event.target)) setSettings(false);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (settingsToggle?.getAttribute("aria-expanded") === "true") {
        setSettings(false);
        settingsToggle.focus();
      } else if (toggle.getAttribute("aria-expanded") === "true") {
        setMenu(false);
        toggle.focus();
      }
    }
  });

  window.addEventListener("resize", () => {
    setMenu(false);
    setSettings(false);
  });
});
