/* Edit-screen enhancement.
 *
 * The form works without this file. Every group it toggles is already hidden or
 * shown server-side from the stored row, which is the state that matters — you
 * open a transfer and the transfer fields are there, you open a spend and they
 * are not. This only keeps up when the type or the currency is changed while
 * the page is open, which the server cannot see until you save.
 *
 * Three jobs, all of them removals of things that do not apply:
 *
 *   - a transfer has no merchant and no in-person/online, because it has no
 *     counterparty. transactions._prepare() has always dropped both on one, so
 *     offering them was offering controls that quietly did nothing.
 *   - anything that is not a transfer has no "into" account and no arriving
 *     amount.
 *   - an entry in the household's own currency has no rate to it. The box is
 *     not empty there, it is meaningless.
 *
 * And one that is not cosmetic: changing the destination to an account in a
 * different currency clears the arriving amount. Left behind, that number is
 * about the currency it is no longer in — which is how 10.00 EGP once arrived
 * as 10.00 EUR. transactions._check_transfer_rate() refuses that now, which is
 * the real guard; this stops it being typed in the first place.
 */

(function () {
  "use strict";

  var form = document.getElementById("edit-form");
  if (!form) return;

  var qs = function (id) { return document.getElementById(id); };

  var direction = qs("direction");
  var currency = qs("currency");
  var counterAccount = qs("counter_account_id");
  var counterAmount = qs("counter_amount");
  var merchant = qs("merchant_id");

  var base = (document.body.dataset.baseCurrency || "EGP").toUpperCase();

  function isTransfer() {
    return direction && direction.value === "transfer";
  }

  /* Groups declare when they belong rather than the script holding a list of
     ids: adding a field later means putting it in the right box, not editing
     this file. */
  function toggle(id, shown) {
    var el = qs(id);
    if (el) el.hidden = !shown;
  }

  function toggleAll(attr, shown) {
    var els = form.querySelectorAll("[" + attr + "]");
    for (var i = 0; i < els.length; i++) els[i].hidden = !shown;
  }

  function syncDirection() {
    var transfer = isTransfer();
    toggle("transfer-fields", transfer);
    toggle("party-fields", !transfer);
    toggleAll("data-when-transfer", transfer);
    toggleAll("data-when-not-transfer", !transfer);

    /* Cleared as well as hidden. A hidden <select> still posts its value, and
       while the server drops it either way, leaving it set means switching back
       to a spend silently re-selects a merchant nobody chose on this screen. */
    if (transfer && merchant) merchant.value = "";
  }

  function syncCurrency() {
    if (!currency) return;
    // A rate to base values a foreign spend. A transfer has both legs in their
    // own account's currency and nothing reads one, so the pair goes together.
    toggle("fx-field", !isTransfer() && currency.value.toUpperCase() !== base);
    toggle("currency-field", !isTransfer());
  }

  var lastIntoCode = null;

  function syncCounter() {
    if (!counterAccount || !currency) return;

    var chosen = counterAccount.options[counterAccount.selectedIndex];
    var code = (chosen && chosen.dataset.currency) || null;

    // Same currency both ends: the server copies the amount across rather than
    // asking twice, so the box is not a question.
    toggle("counter-amount-field",
           isTransfer() && !!code && code !== currency.value.toUpperCase());

    if (counterAmount && lastIntoCode !== null && code !== lastIntoCode) {
      counterAmount.value = "";
    }
    lastIntoCode = code;
  }

  form.addEventListener("change", function (e) {
    if (e.target.id === "direction") { syncDirection(); syncCurrency(); syncCounter(); }
    if (e.target.id === "currency") { syncCurrency(); syncCounter(); }
    if (e.target.id === "counter_account_id") syncCounter();
  });

  syncDirection();
  syncCurrency();
  syncCounter();
})();
