/* Entry form enhancement.
 *
 * The form already works without this file: chips are radio inputs, merchant
 * defaults are applied server-side for blank fields, the search box carries the
 * new-merchant field itself, and the toast is rendered by the server.
 * Everything here only removes waiting.
 */

(function () {
  "use strict";

  var form = document.getElementById("entry-form");
  if (!form) return;

  var qs = function (id) { return document.getElementById(id); };

  var amount = qs("amount");
  var category = qs("category_id");
  var account = qs("account_id");
  var online = qs("is_online");
  var currency = qs("currency");
  var fxField = qs("fx-field");
  var fxInput = qs("fx_rate_to_base");
  var fxHint = qs("fx-hint");
  var fxAge = qs("fx-age");
  var transferFields = qs("transfer-fields");
  var counterAccount = qs("counter_account_id");
  var counterField = qs("counter-amount-field");
  /* One box does both jobs: it filters the list, and it *is* the new-merchant
     field the server reads when nothing matched. */
  var search = qs("merchant-search");
  var addBtn = qs("merchant-add");
  var none = qs("m-none");
  var list = qs("merchant-list");
  var base = (document.body.dataset.baseCurrency || "EGP").toUpperCase();

  /* ---- merchant selection fills the rest of the form ------------------ */

  /* The account box always names a real account, so "has the person chosen
     this themselves?" can no longer be answered by looking for an empty value
     the way it can for the category. It is tracked explicitly instead: a
     `change` event on a <select> only fires for a human, never for a script
     setting `.value`, so the flag below is set exactly when someone touches it.

     Without this, removing the "Auto" option would have quietly killed merchant
     defaults for the account — the box would always look chosen. */
  var accountDefault = (account && account.dataset.default) || "";

  function accountChosenByHand() {
    return account && account.dataset.touched === "1";
  }

  function applyMerchantDefaults(input) {
    if (!input) return;
    var c = input.dataset.category;
    var a = input.dataset.account;
    var o = input.dataset.online;

    // Only fill what the person has not already chosen — auto-fill must never
    // overwrite a deliberate selection.
    if (c && category && !category.value) category.value = c;
    if (a && account && !accountChosenByHand()) { account.value = a; syncCurrency(); }
    if (o && online) online.value = o === "1" ? "1" : "0";
  }

  /* Undo exactly what this merchant filled in, and nothing else. Comparing the
     value before clearing is what keeps a deliberate choice from being wiped by
     de-selecting the chip that happened to share it.

     The account reverts to the default the page was rendered with rather than
     to nothing, because nothing is no longer one of the options. */
  function clearMerchantDefaults(input) {
    if (!input) return;
    var c = input.dataset.category;
    var a = input.dataset.account;
    if (c && category && category.value === c) category.value = "";
    if (a && account && account.value === a && !accountChosenByHand()) {
      account.value = accountDefault;
      syncCurrency();
    }
  }

  function checkedMerchant() {
    return form.querySelector('input[name="merchant_id"]:checked');
  }

  /* ---- chips toggle both ways ----------------------------------------- */

  /* A radio group cannot be emptied by clicking one of its own buttons, so the
     label click is intercepted and the toggle handled here: tapping the chip
     that is already selected points the group at the empty "No merchant" option
     instead. Without this a mis-tap is unfixable without reloading the page. */
  form.addEventListener("click", function (e) {
    var label = e.target.closest ? e.target.closest("label.chip") : null;
    if (!label || !label.htmlFor) return;

    var input = document.getElementById(label.htmlFor);
    // The receipt toggle wears the same chip styling but is a plain checkbox
    // with its own meaning. Only the merchant radios are toggled by hand here.
    if (!input || input.disabled || input.name !== "merchant_id") return;

    e.preventDefault();

    if (input.checked && input !== none) {
      clearMerchantDefaults(input);
      if (none) none.checked = true;
      else input.checked = false;
    } else {
      input.checked = true;
      applyMerchantDefaults(input);
      if (search && input !== none) search.value = "";
    }
    refreshChips();
  });

  form.addEventListener("change", function (e) {
    if (e.target.name === "merchant_id") {
      applyMerchantDefaults(e.target);
      if (search) search.value = "";
    }
    if (e.target.name === "direction") syncDirection();
    if (e.target.id === "account_id") {
      // A human moved it. From here on merchant defaults leave it alone.
      account.dataset.touched = "1";
      syncCurrency();
      syncCounter();
    }
    if (e.target.id === "currency") syncFx(true);
    if (e.target.id === "counter_account_id") syncCounter();
  });

  /* ---- currency follows the account, rate appears only when needed ---- */

  function syncCurrency() {
    if (!account || !currency) return;
    var opt = account.options[account.selectedIndex];
    var code = opt && opt.dataset.currency;
    if (code) currency.value = code;
    syncFx(true);
  }

  /* The cached rate is a starting point, never a record. `replace` is true when
     the currency itself changed, because a rate typed for dollars is not a rate
     for euros — keeping it would be worse than an empty box. On first load it is
     false, so a value the server put there (or one typed before a failed save)
     survives. */
  function syncFx(replace) {
    if (!fxField || !currency) return;
    var foreign = currency.value.toUpperCase() !== base;
    fxField.hidden = !foreign;

    var opt = currency.options[currency.selectedIndex];
    var rate = opt && opt.dataset ? opt.dataset.rate : "";
    var age = opt && opt.dataset ? opt.dataset.rateAge : "";

    if (fxInput) {
      if (!foreign) fxInput.value = "";
      else if (rate && (replace || !fxInput.value)) fxInput.value = rate;
    }

    if (fxHint) {
      fxHint.hidden = !(foreign && rate);
      if (fxAge && age !== "" && age !== undefined) {
        fxAge.textContent =
          age === "0" ? "today" : age + " day" + (age === "1" ? "" : "s") + " ago";
      }
    }
  }

  /* ---- transfer fields appear only for transfers ---------------------- */

  function currentDirection() {
    var checked = form.querySelector('input[name="direction"]:checked');
    return checked ? checked.value : "spend";
  }

  function syncDirection() {
    var isTransfer = currentDirection() === "transfer";
    if (transferFields) transferFields.hidden = !isTransfer;
    if (isTransfer) {
      var more = qs("more");
      if (more) more.open = true;
    }
    refreshChips();
    syncCounter();
  }

  function syncCounter() {
    if (!counterField || !counterAccount || !currency) return;

    // A transfer is between two accounts, so the account money is leaving
    // cannot also be the one it arrives in.
    var fromId = account ? account.value : "";
    for (var i = 0; i < counterAccount.options.length; i++) {
      var opt = counterAccount.options[i];
      opt.disabled = opt.value !== "" && opt.value === fromId;
      if (opt.disabled && opt.selected) counterAccount.value = "";
    }

    var chosen = counterAccount.options[counterAccount.selectedIndex];
    var code = chosen && chosen.dataset.currency;
    // Asking for the arriving amount only makes sense across currencies; for a
    // same-currency transfer the server copies the amount across.
    var crossCurrency =
      currentDirection() === "transfer" && code && code !== currency.value.toUpperCase();
    counterField.hidden = !crossCurrency;
  }

  /* ---- one list per side of the form ---------------------------------- */

  function wantedKind() {
    return currentDirection() === "income" ? "income" : "spend";
  }

  function kindOk(el, kind) {
    var k = el.getAttribute("data-kind");
    return !k || k === "both" || k === kind;
  }

  /* Hidden here is the product of two filters — which side of the form we are
     on, and what has been typed into the search box — so both are applied in
     one pass rather than fighting each other across two handlers. */
  function refreshChips() {
    var isTransfer = currentDirection() === "transfer";
    var blocks = form.querySelectorAll("[data-merchant-block]");
    for (var b = 0; b < blocks.length; b++) blocks[b].hidden = isTransfer;
    if (isTransfer) return;

    var kind = wantedKind();
    var term = search ? search.value.trim().toLowerCase() : "";
    var labels = form.querySelectorAll("label.chip");

    for (var i = 0; i < labels.length; i++) {
      var label = labels[i];
      var input = label.htmlFor ? document.getElementById(label.htmlFor) : null;
      if (!input || input.name !== "merchant_id") continue;
      var show = kindOk(label, kind);

      // Typing filters the full list only. The pinned row is the fast path and
      // stays where the thumb left it.
      if (show && term && input !== none && label.parentNode === list) {
        show = (label.getAttribute("data-name") || "").indexOf(term) !== -1;
      }

      label.hidden = !show;
      if (input && input !== none) input.disabled = !show;
    }

    // Switching sides must not leave a merchant from the other list selected.
    var checked = checkedMerchant();
    if (checked && checked.disabled && none) none.checked = true;

    var swaps = document.querySelectorAll("[data-kind-label]");
    for (var s = 0; s < swaps.length; s++) {
      swaps[s].hidden = swaps[s].getAttribute("data-kind-label") !== kind;
    }

    if (addBtn) {
      var offer = term.length > 0 && !exactMatch(term, kind);
      addBtn.hidden = !offer;
      addBtn.textContent = offer ? 'Add "' + search.value.trim() + '"' : "Add";
    }
  }

  function exactMatch(term, kind) {
    var labels = form.querySelectorAll("label.chip");
    for (var i = 0; i < labels.length; i++) {
      if (!kindOk(labels[i], kind)) continue;
      var name = labels[i].getAttribute("data-name");
      if (name === null) name = labels[i].textContent.trim().toLowerCase();
      if (name === term) return true;
    }
    return false;
  }

  if (search) search.addEventListener("input", refreshChips);

  /* ---- inline add ------------------------------------------------------ */

  if (addBtn) {
    addBtn.addEventListener("click", function () {
      var name = search.value.trim();
      if (!name) return;
      addBtn.disabled = true;

      var kind = wantedKind();
      var body = new FormData();
      body.append("name", name);
      body.append("direction", currentDirection());
      body.append("_csrf", form.querySelector('input[name="_csrf"]').value);

      fetch("/entry/merchants", { method: "POST", body: body, credentials: "same-origin" })
        .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
        .then(function (res) {
          if (!res.ok) throw new Error(res.d.error || "Could not add that merchant.");
          var input = document.createElement("input");
          input.type = "radio";
          input.name = "merchant_id";
          input.id = "m-" + res.d.id;
          input.value = res.d.id;
          input.className = "chip__input";
          input.dataset.kind = res.d.kind || kind;
          input.checked = true;

          var label = document.createElement("label");
          label.className = "chip";
          label.htmlFor = input.id;
          label.dataset.kind = input.dataset.kind;
          label.dataset.name = res.d.name.toLowerCase();
          label.textContent = res.d.name;

          list.insertBefore(label, list.firstChild);
          list.insertBefore(input, label);
          search.value = "";
          addBtn.hidden = true;
          refreshChips();
        })
        .catch(function (err) {
          // Falling back rather than failing: the name stays in the box, which
          // is the field the server creates the merchant from on save.
          console.warn(err);
        })
        .then(function () { addBtn.disabled = false; });
    });
  }

  /* ---- keep the keypad on the amount ---------------------------------- */

  if (amount) {
    // Some mobile browsers ignore autofocus after a redirect; nudging it here
    // is what keeps "type amount" the literal first action after a save.
    if (!amount.value) {
      try { amount.focus({ preventScroll: true }); } catch (e) { amount.focus(); }
    }
    amount.addEventListener("input", function () {
      // Accept a comma as a decimal separator without fighting the keypad.
      var cleaned = amount.value.replace(/[^\d.,]/g, "");
      if (cleaned !== amount.value) amount.value = cleaned;
    });
  }

  /* ---- the camera --------------------------------------------------------
     Two jobs, both of them removals of waiting rather than of rules.

     The tick replacing the camera icon is the only confirmation that a photo
     was taken — the file input itself shows nothing on a phone. Without
     JavaScript the icon stays as it is, and the photo still attaches: the
     server is what reads the file, and the toast on the next screen says so.

     Unticking "no receipt" mirrors what the server does anyway when both
     arrive together. Doing it here means the contradiction never appears on
     screen, rather than being quietly resolved after the save. */

  var receipt = document.getElementById("receipt");
  var cameraButton = document.getElementById("camera-button");
  var receiptless = document.getElementById("receiptless");

  if (receipt && cameraButton) {
    receipt.addEventListener("change", function () {
      var picked = receipt.files && receipt.files.length > 0;
      cameraButton.classList.toggle("camera--has", picked);
      if (picked && receiptless) receiptless.checked = false;
    });
  }

  if (receiptless && receipt) {
    receiptless.addEventListener("change", function () {
      // The other direction. A photo already chosen is evidence; saying "no
      // receipt" now means the photo was a mistake, so it goes.
      if (receiptless.checked && receipt.files && receipt.files.length) {
        receipt.value = "";
        if (cameraButton) cameraButton.classList.remove("camera--has");
      }
    });
  }

  var checkedNow = checkedMerchant();
  if (checkedNow) applyMerchantDefaults(checkedNow);
  syncDirection();
  syncFx(false);
})();
