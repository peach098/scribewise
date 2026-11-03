// ScribeWise landing — ссылки, меню, язык. Страница работает и без этого скрипта.
(function () {
  // Куда ведут кнопки «Попасть в бету». Поменяйте здесь — обновятся все кнопки на обеих версиях.
  var BETA_URL = "https://t.me/the_scribewise_bot";
  // Ссылка на открытый репозиторий. Пока пусто — ссылки на GitHub скрыты.
  var REPO_URL = "https://github.com/peach098/scribewise";

  document.querySelectorAll("[data-beta-link]").forEach(function (a) {
    a.href = BETA_URL;
    a.target = "_blank";
    a.rel = "noopener";
  });

  document.querySelectorAll("[data-repo-link]").forEach(function (a) {
    if (!REPO_URL) return;
    a.href = REPO_URL;
    a.target = "_blank";
    a.rel = "noopener";
    a.hidden = false;
  });

  // Запоминаем выбранный язык для корневой страницы-редиректа.
  function remember(lang) {
    try { localStorage.setItem("scribewise-lang", lang); } catch (e) {}
  }
  remember(document.documentElement.lang);
  document.querySelectorAll("[data-lang]").forEach(function (a) {
    a.addEventListener("click", function () { remember(a.getAttribute("data-lang")); });
  });

  // Мобильное меню.
  var btn = document.querySelector(".menu-btn");
  var nav = document.getElementById("nav");
  if (btn && nav) {
    var setOpen = function (open) {
      nav.classList.toggle("open", open);
      btn.setAttribute("aria-expanded", String(open));
    };
    btn.addEventListener("click", function () { setOpen(!nav.classList.contains("open")); });
    nav.addEventListener("click", function (e) { if (e.target.closest("a")) setOpen(false); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") setOpen(false); });
  }

  var year = document.querySelector("[data-year]");
  if (year) year.textContent = String(new Date().getFullYear());
})();
