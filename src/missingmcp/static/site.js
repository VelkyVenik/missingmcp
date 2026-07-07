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

// Signup / suggestion modals. CSP (default-src 'self') allows the same-origin
// fetch below; no inline scripts, so all behavior lives in this one file.
document.addEventListener("click", function (e) {
  var opener = e.target.closest("[data-modal]");
  if (opener) {
    var dlg = document.getElementById("modal-" + opener.dataset.modal);
    if (dlg && dlg.showModal) { dlg.showModal(); }
    return;
  }
  if (e.target.closest("[data-close]")) {
    var card = e.target.closest("dialog");
    if (card) { card.close(); }
    return;
  }
  // Click on the backdrop (the <dialog> element itself, outside the card) closes it.
  if (e.target.tagName === "DIALOG") { e.target.close(); }
});

document.addEventListener("submit", function (e) {
  var form = e.target.closest("form[data-endpoint]");
  if (!form) { return; }
  e.preventDefault();
  var msg = form.querySelector("[data-msg]");
  var btn = form.querySelector("button[type=submit]");
  if (btn) { btn.disabled = true; }
  fetch(form.dataset.endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(new FormData(form)),
  }).then(function (r) {
    return r.json().catch(function () { return { ok: r.ok }; });
  }).then(function (data) {
    if (data.ok) {
      // hide the inputs and show a thank-you; the visitor can just close the modal
      form.querySelectorAll("label, button[type=submit]").forEach(function (el) {
        el.style.display = "none";
      });
      if (msg) {
        msg.className = "modal-msg ok";
        msg.textContent = "Thanks! I’ll be in touch when there’s something new.";
      }
    } else {
      if (btn) { btn.disabled = false; }
      if (msg) {
        msg.className = "modal-msg err";
        msg.textContent = data.error === "invalid_email"
          ? "That email doesn’t look right — please check it."
          : data.error === "rate_limited"
          ? "Too many tries — wait a minute and try again."
          : "Something went wrong — please try again.";
      }
    }
  }).catch(function () {
    if (btn) { btn.disabled = false; }
    if (msg) {
      msg.className = "modal-msg err";
      msg.textContent = "Network error — please try again.";
    }
  });
});
