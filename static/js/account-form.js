/* Account form enhancement.
 *
 * Every rule this file expresses is enforced again in reference.py and, behind
 * that, by triggers in migration 003. With JavaScript off the whole form is
 * simply visible at once and the server explains what does not apply — which is
 * clumsy, but never wrong.
 */

(function () {
  "use strict";

  var form = document.getElementById("account-form");
  if (!form) return;

  var type = document.getElementById("type");
  var currency = document.getElementById("currency");
  var parent = document.getElementById("parent_account_id");
  var owner = document.getElementById("owner_id");
  var same = document.getElementById("same_limits");
  var localLimit = document.getElementById("credit_limit_local");
  var intlField = document.getElementById("intl-limit-field");
  var intlLimit = document.getElementById("credit_limit_intl");

  /* Sections declare the types they belong to rather than the script holding a
     list of ids: adding a field later means adding an attribute, not editing
     this file. */
  function syncType() {
    var current = type ? type.value : "";
    var sections = form.querySelectorAll("[data-when-type]");
    for (var i = 0; i < sections.length; i++) {
      var wanted = sections[i].getAttribute("data-when-type").split(/\s+/);
      sections[i].hidden = wanted.indexOf(current) === -1;
    }
    syncParentCurrency();
    syncSameLimits();
  }

  /* A linked account is the same money reached a different way, so its currency
     is not a separate choice. Showing it as fixed is more honest than letting
     someone pick a currency the server is going to overwrite. */
  function syncParentCurrency() {
    if (!currency || !type) return;
    var linked = type.value === "instapay" || type.value === "debit_card";
    var opt = parent && parent.options[parent.selectedIndex];
    var code = opt && opt.dataset ? opt.dataset.currency : "";
    if (linked && code) currency.value = code;
    currency.disabled = linked && !!code;
    // A disabled select submits nothing, so the value travels in a twin field.
    var mirror = document.getElementById("currency-mirror");
    if (currency.disabled) {
      if (!mirror) {
        mirror = document.createElement("input");
        mirror.type = "hidden";
        mirror.id = "currency-mirror";
        mirror.name = "currency";
        form.appendChild(mirror);
      }
      mirror.value = currency.value;
    } else if (mirror) {
      mirror.parentNode.removeChild(mirror);
    }
  }

  /* The tick is a shortcut, not a default: in Egypt the two limits usually
     differ, so ticking it copies the local number across and gets out of the
     way rather than hiding a field that was already filled in. */
  function syncSameLimits() {
    if (!same || !intlField) return;
    intlField.hidden = same.checked;
    if (same.checked && localLimit && intlLimit) intlLimit.value = localLimit.value;
  }

  /* A card on an account belongs to that account's owner. Only a default — it fires when the
     linked account changes, and whatever is chosen afterwards stands, because a
     joint account with a card each is a real arrangement. */
  function inheritOwner() {
    if (!owner || !parent || !type) return;
    if (type.value !== "instapay" && type.value !== "debit_card") return;
    var opt = parent.options[parent.selectedIndex];
    if (!opt || !opt.dataset) return;
    owner.value = opt.dataset.owner || "";
  }

  if (type) type.addEventListener("change", syncType);
  if (parent) parent.addEventListener("change", function () {
    syncParentCurrency();
    inheritOwner();
  });
  if (same) same.addEventListener("change", syncSameLimits);
  if (localLimit) localLimit.addEventListener("input", function () {
    if (same && same.checked && intlLimit) intlLimit.value = localLimit.value;
  });

  syncType();
})();
