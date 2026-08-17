/* Loaded on every page. Everything here is optional by construction — the
 * shell is server-rendered and the app works with JavaScript disabled, which
 * is invariant 10.
 *
 * The one thing that lives here is the service worker registration, and it is
 * what makes the app installable: Chrome will not offer "Add to home screen"
 * for a page that has a manifest but no worker. What the worker itself does is
 * almost nothing on purpose — see static/js/sw.js for why a ledger should not
 * be cached.
 */

if ("serviceWorker" in navigator) {
  // After load rather than during it. Registration competes with the first
  // paint for the same connection, and on a Pi over Tailscale that is a
  // noticeable delay on the screen a person actually asked for.
  window.addEventListener("load", () => {
    // Registered at the root scope, which is why the file is served from /sw.js
    // rather than out of /static/js/ where it lives on disk.
    navigator.serviceWorker.register("/sw.js").catch(() => {
      /* An install that fails costs nothing: no offline page, everything else
       * works. Not worth a message to somebody trying to log a coffee. */
    });
  });
}
