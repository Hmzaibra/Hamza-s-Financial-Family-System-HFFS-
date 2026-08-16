/* Edit-screen enhancement.
 *
 * The edit form works without this file, the same way the entry form does. Two
 * things here, both of them removals of confusion rather than of rules:
 *
 *   - a transfer has no merchant, so the merchant field goes away when the type
 *     is switched to one. The server has always nulled it (transactions._prepare
 *     never reads a merchant on a transfer), and the template already hides it
 *     server-side for a transfer being opened — this only covers the case of
 *     changing the type while the page is open.
 *
 *   - changing the destination account to one in a different currency clears the
 *     arriving amount. Left behind, that number is about the currency it is no
 *     longer in, and it is exactly how 10.00 EGP once arrived as 10.00 EUR.
 *     transactions._check_transfer_rate() refuses that now, which is the real
 *     guard; this stops it being typed in the first place.
 */

(function () {
  "use strict";

  var form = document.getElementById("edit-form");
  if (!form) return;

  var qs = function (id) { return document.getElementById(id); };

  var direction = qs("direction");
  var merchantField = qs("merchant-field");
  var counterField = qs("counter-amount-field");
  var counterAccount = qs("counter_account_id");
  var counterAmount = qs("counter_amount");
  var currency = qs("currency");

  function isTransfer() {
    return direction && direction.value === "transfer";
  }

  function syncMerchant() {
    if (!merchantField) return;
    merchantField.hidden = isTransfer();
    /* Cleared as well as hidden. A hidden <select> still posts its value, and
       while the server drops it either way, leaving it set means switching back
       to a spend silently re-selects a merchant nobody chose on this screen. */
    var picker = qs("merchant_id");
    if (picker && isTransfer()) picker.value = "";
  }

  var lastIntoCode = null;

  function syncCounter() {
    if (!counterAccount || !currency) return;

    var chosen = counterAccount.options[counterAccount.selectedIndex];
    var code = (chosen && chosen.dataset.currency) || null;

    if (counterField) {
      counterField.hidden =
        !isTransfer() || !code || code === currency.value.toUpperCase();
    }

    if (counterAmount && lastIntoCode !== null && code !== lastIntoCode) {
      counterAmount.value = "";
    }
    lastIntoCode = code;
  }

  form.addEventListener("change", function (e) {
    if (e.target.id === "direction") { syncMerchant(); syncCounter(); }
    if (e.target.id === "counter_account_id" || e.target.id === "currency") syncCounter();
  });

  syncMerchant();
  syncCounter();
})();
