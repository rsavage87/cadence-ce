/* Cadence CE shell behavior: drawer and modal, toasts, theme, keyboard shortcuts. Data and state live on the server (HTMX). */
(function () {
  "use strict";
  const $ = (s) => document.querySelector(s);

  function openDrawer() {
    const d = $("#drawer");
    d.classList.add("open");
    d.setAttribute("aria-hidden", "false");
    $("#scrim").classList.add("show");
    const b = d.querySelector(".dr-b");
    if (b) b.scrollTop = 0;
  }
  function closeDrawer() {
    const d = $("#drawer");
    d.classList.remove("open");
    d.setAttribute("aria-hidden", "true");
    $("#scrim").classList.remove("show");
  }
  function openModal() {
    const m = $("#modal");
    m.classList.add("open");
    m.setAttribute("aria-hidden", "false");
    const f = m.querySelector("[autofocus]") || m.querySelector("input:not([type=hidden]),select,textarea");
    if (f) setTimeout(() => f.focus(), 30);
  }
  function closeModal() {
    const m = $("#modal");
    m.classList.remove("open");
    m.setAttribute("aria-hidden", "true");
    $("#modal-card").innerHTML = "";
  }
  let toastTimer;
  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("show"), 2800);
  }
  function themeNow() {
    return document.documentElement.getAttribute("data-theme") ||
      (window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  }
  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); toast("Link copied"); }
    catch (e) { toast("Copy failed. Select the link and copy it."); }
  }

  document.addEventListener("htmx:afterSwap", (e) => {
    const id = e.detail.target && e.detail.target.id;
    if (id === "drawer") openDrawer();
    if (id === "modal-card" && !$("#modal").classList.contains("open")) openModal();
    // A history restore (back button) fires afterSwap without a target.
    if (e.detail.target && e.detail.target.classList.contains("sugg")) e.detail.target.classList.toggle("show", e.detail.target.innerHTML.trim() !== "");
  });
  document.addEventListener("htmx:responseError", (e) => {
    const s = e.detail.xhr.status;
    toast(s === 403 ? "You don't have access to do that." : s === 404 ? "Not found. It may have been removed." : "Something went wrong. Try again.");
  });
  document.addEventListener("htmx:sendError", () => toast("Can't reach the server. Check your connection."));
  document.body.addEventListener("toast", (e) => toast(e.detail.value));
  document.body.addEventListener("modal-close", closeModal);
  document.body.addEventListener("drawer-close", closeDrawer);

  // Export and print links carry the list's current filters. The filters change through HTMX, which updates the address but
  // not these links, so rewrite them whenever the address changes: then a middle-click, "Open in new tab", or "Save link as"
  // gets the filters on screen too, not only a plain click.
  // Slice 22: links that open a page or a file of their own (prints, labels, CSVs, report PDFs) name the facility this page shows,
  // as staff emails do, so one followed from a tab left in another facility offers the switch (apps.web.decorators) instead of
  // answering with the record that has the same number in the facility the browser is in now.
  const FACILITY = document.body.dataset.facility || "";
  function withFacility(href) {
    if (!FACILITY || !href || !href.startsWith("/") || href.startsWith("//")) return href;
    const u = new URL(href, window.location.origin);
    u.searchParams.set("facility", FACILITY);
    return u.pathname + u.search + u.hash;
  }
  function tagLinks(root) {
    if (!FACILITY || !root.querySelectorAll) return;
    root.querySelectorAll('a[target="_blank"][href], a[download][href]').forEach((a) => {
      if (a.dataset.act === "with-filters") return;  // syncFilterLinks owns these
      const href = a.getAttribute("href"), tagged = withFacility(href);
      if (tagged !== href) a.setAttribute("href", tagged);
    });
  }
  tagLinks(document);
  document.addEventListener("htmx:load", (e) => tagLinks(e.target));  // drawers, modals, and lists swapped in later

  function syncFilterLinks() {
    document.querySelectorAll('[data-act="with-filters"]').forEach((a) => a.setAttribute("href", withFacility(a.dataset.base + window.location.search)));
  }
  ["htmx:pushedIntoHistory", "htmx:replacedInHistory", "htmx:historyRestore"].forEach((n) => document.addEventListener(n, syncFilterLinks));
  window.addEventListener("popstate", syncFilterLinks);
  syncFilterLinks();

  document.addEventListener("click", (e) => {
    const el = e.target;
    if (el === $("#scrim")) { closeDrawer(); return; }
    if (el === $("#modal")) { closeModal(); return; }
    const act = el.closest("[data-act]");
    if (!act) return;
    switch (act.dataset.act) {
      case "close-drawer": closeDrawer(); break;
      case "close-modal": closeModal(); break;
      case "copy": copyText(act.dataset.copy); break;
      case "print": window.print(); break;
      case "with-filters": syncFilterLinks(); break;  // belt and braces: the listeners above keep it current already
      case "theme": {
        const next = themeNow() === "dark" ? "light" : "dark";
        document.documentElement.setAttribute("data-theme", next);
        try { localStorage.setItem("cadence-theme", next); } catch (err) { /* private mode */ }
        break;
      }
    }
  });
  // Slice 24 review: a My work card's move (Start, Resume, Take) re-fetches the list, which replaces the button that had focus.
  // Keyboard and screen-reader users go back to that card (or the list's top if it left), not to the top of the page.
  let mwFocus = null;
  document.addEventListener("click", (e) => { const b = e.target.closest("[data-mw-card]"); if (b) mwFocus = b.dataset.mwCard; }, true);
  document.addEventListener("htmx:afterSettle", (e) => {
    if (!mwFocus || !(e.target.id === "my-work-body")) return;
    const card = document.getElementById(mwFocus) || e.target.querySelector(".mw-card");
    mwFocus = null;
    if (card && (document.activeElement === document.body || !document.activeElement)) card.focus({ preventScroll: true });
  });
  // Slice 22: the top bar's facility menu. Choosing a facility posts the switch; "All facilities" opens that page in this one.
  document.addEventListener("change", (e) => {
    const sel = e.target.closest('select[data-act="switch-facility"]');
    if (!sel) return;
    if (sel.value === "all") { window.location.href = sel.dataset.all; return; }
    sel.form.submit();
  });
  document.addEventListener("keydown", (e) => {
    const typing = /INPUT|TEXTAREA|SELECT/.test(e.target.tagName);
    if (e.key === "Escape") {
      if ($("#modal").classList.contains("open")) closeModal(); else closeDrawer();
      const menu = $(".user-menu[open]");
      if (menu) menu.removeAttribute("open");
      return;
    }
    if (e.key === "/" && !typing) { e.preventDefault(); $("#gsearch").focus(); }
  });
  // Rows open drawers via hx-get; the link inside is for keyboard users and cmd/ctrl-click to a new tab.
  // Only a click-triggered container (a row or card) owns its links; a wrapper that re-fetches on an event does not.
  document.addEventListener("click", (e) => {
    const a = e.target.closest("[hx-get] a[href]");
    if (!a || a.hasAttribute("hx-get")) return;
    const trigger = a.closest("[hx-get]").getAttribute("hx-trigger") || "click";
    if (!/\bclick\b/.test(trigger)) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) { e.stopPropagation(); return; }
    e.preventDefault();
  }, true);
  document.addEventListener("click", (e) => {
    const menu = $(".user-menu[open]");
    if (menu && !menu.contains(e.target)) menu.removeAttribute("open");
  });
})();
