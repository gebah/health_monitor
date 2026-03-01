// Dropdown
(function () {
  const btn = document.getElementById("userMenuBtn");
  const menu = document.getElementById("userMenu");

  if (btn && menu) {
    const close = () => {
      menu.classList.remove("is-open");
      btn.setAttribute("aria-expanded", "false");
    };

    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const open = menu.classList.toggle("is-open");
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    });

    document.addEventListener("click", () => close());
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") close();
    });
  }
})();

// Dark mode toggle (localStorage)
// Dark mode toggle (localStorage) - default LIGHT
(function () {
  const toggle = document.getElementById("themeToggle");
  const root = document.documentElement;

  const apply = (theme) => {
    root.setAttribute("data-theme", theme);
    localStorage.setItem("hm_theme", theme);
  };

  // default: light (als er niks is opgeslagen)
  const saved = localStorage.getItem("hm_theme");
  if (saved === "dark" || saved === "light") {
    root.setAttribute("data-theme", saved);
  } else {
    apply("light");
  }

  if (toggle) {
    toggle.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const current = root.getAttribute("data-theme") || "light";
      apply(current === "dark" ? "light" : "dark");
    });
  }
})();