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
