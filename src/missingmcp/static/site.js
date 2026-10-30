// Copy-to-clipboard for [data-copy] buttons. CSP (default-src 'self') allows
// no inline scripts, so all page behavior lives in this one same-origin file.
document.addEventListener("click", function (e) {
  var btn = e.target.closest("[data-copy]");
  if (!btn) return;
  navigator.clipboard.writeText(btn.dataset.copy).then(function () {
    btn.classList.add("copied");
    setTimeout(function () { btn.classList.remove("copied"); }, 1500);
  }, function () {});
});

// Double-submit guard for regular (non-fetch) forms. The OAuth sign-in forms
// POST one-time CSRF tokens, so a double-click would burn the token on the
// first request and fail the second.
document.addEventListener("submit", function (e) {
  var form = e.target;
  if (form.dataset.busy) { e.preventDefault(); return; }
  form.dataset.busy = "1";
  // Garmin sign-in can take up to a minute; say so, or people resubmit and
  // start a second, overlapping sign-in (failing-logins map).
  var btn = form.querySelector("button[data-busy-label]");
  if (btn) {
    btn.dataset.idleLabel = btn.textContent;
    btn.textContent = btn.dataset.busyLabel;
    btn.disabled = true;
  }
});

// Demo videos ([data-autoplay]): muted autoplay only while on screen, and never
// for visitors who asked for reduced motion — they get the poster and the
// controls. The controls let anyone pause it (WCAG 2.2.2).
(function () {
  var videos = document.querySelectorAll("video[data-autoplay]");
  if (!videos.length) return;
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  var play = function (v) { var p = v.play(); if (p && p.catch) { p.catch(function () {}); } };
  if (!("IntersectionObserver" in window)) { videos.forEach(play); return; }
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (e.isIntersecting) {
        if (!e.target.dataset.userPaused) { play(e.target); }
      } else if (!e.target.paused) {
        e.target.dataset.autoPaused = "1";
        e.target.pause();
      }
    });
  }, { threshold: 0.4 });
  videos.forEach(function (v) {
    // a pause the visitor made sticks; one we made while scrolled away doesn't
    v.addEventListener("pause", function () {
      if (v.dataset.autoPaused) { delete v.dataset.autoPaused; } else { v.dataset.userPaused = "1"; }
    });
    v.addEventListener("play", function () { delete v.dataset.userPaused; });
    io.observe(v);
  });
})();

// Back/forward cache restores the page as it was left — busy, button
// disabled — so a user going back to fix a typo couldn't submit again.
window.addEventListener("pageshow", function () {
  document.querySelectorAll("form[data-busy]").forEach(function (form) {
    delete form.dataset.busy;
    var btn = form.querySelector("button[data-busy-label]");
    if (btn) {
      btn.disabled = false;
      if (btn.dataset.idleLabel) { btn.textContent = btn.dataset.idleLabel; }
    }
  });
});

// Car sign-in: show only the picked brand's notes and fields. Hidden fieldsets
// are disabled, so their inputs aren't submitted (the server validates per
// brand; without JS every field shows). The pick survives an error re-render
// via sessionStorage, best effort.
(function () {
  var picker = document.querySelector("[data-brand-picker]");
  if (!picker) { return; }
  function show() {
    document.querySelectorAll("[data-brand]").forEach(function (el) {
      var on = el.dataset.brand === picker.value;
      el.hidden = !on;
      if (el.tagName === "FIELDSET") { el.disabled = !on; }
    });
    try { sessionStorage.setItem("car-brand", picker.value); } catch (e) {}
  }
  try {
    var saved = sessionStorage.getItem("car-brand");
    if (saved && picker.querySelector('option[value="' + saved + '"]')) { picker.value = saved; }
  } catch (e) {}
  picker.addEventListener("change", show);
  show();
})();
