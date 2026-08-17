/* The balance timeline's slider.
 *
 * The chart is drawn entirely in the HTML — every coordinate is computed
 * server-side in accounts._series(). This file moves a marker along a line that
 * already exists, and nothing else. With JavaScript off the graph still draws,
 * the slider is simply inert, and the list underneath still answers the same
 * question row by row.
 *
 * The marker moves by setting SVG geometry *attributes* (cx, cy, x1, x2) rather
 * than a style. That is deliberate: the CSP has no `unsafe-inline`, and while
 * CSSOM writes are not what that blocks, geometry attributes are unambiguously
 * fine and need no one to reason about the difference later.
 */

(function () {
  "use strict";

  var root = document.getElementById("timeline");
  if (!root) return;

  var points;
  try {
    points = JSON.parse(root.dataset.points || "[]");
  } catch (e) {
    return;
  }
  if (!points.length) return;

  var slider = document.getElementById("timeline-slider");
  var dot = document.getElementById("timeline-dot");
  var rule = document.getElementById("timeline-rule");
  var outDate = document.getElementById("timeline-date");
  var outLabel = document.getElementById("timeline-label");
  var outDelta = document.getElementById("timeline-delta");
  var outBalance = document.getElementById("timeline-balance");
  var readout = document.getElementById("timeline-readout");

  if (!slider || !dot || !rule) return;

  /* Formatting money in the browser would be a second implementation of
     money.py, in a language with one number type. So every string the readout
     can ever show was rendered server-side and rides along on the point. */
  function show(i) {
    var p = points[i];
    if (!p) return;

    dot.setAttribute("cx", p.x);
    dot.setAttribute("cy", p.y);
    rule.setAttribute("x1", p.x);
    rule.setAttribute("x2", p.x);

    if (outDate) outDate.textContent = p.date;
    if (outLabel) outLabel.textContent = p.label;
    if (outBalance) outBalance.textContent = p.balance_text;
    if (outDelta) {
      outDelta.textContent = p.delta_text;
      outDelta.className = "amt amt--" + (p.delta > 0 ? "income" : "spend");
    }
    if (readout) readout.setAttribute("aria-label",
      p.date + ", " + p.label + ", balance " + p.balance_text);

    var rows = root.querySelectorAll("[data-entry]");
    for (var n = 0; n < rows.length; n++) {
      rows[n].classList.toggle(
        "is-current", rows[n].dataset.entry === String(p.id));
    }
  }

  slider.addEventListener("input", function () {
    show(parseInt(slider.value, 10));
  });

  // The newest entry is the one the page opens on, which is the same thing the
  // balance at the top of the screen is.
  slider.value = String(points.length - 1);
  slider.disabled = false;
  root.classList.add("timeline--live");
  show(points.length - 1);
})();
