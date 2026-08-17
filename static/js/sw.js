/* The service worker exists to make the app installable and to fail politely.
 * It does not cache the app.
 *
 * That is a deliberate decision and the reason is the same one that runs
 * through the rest of this project: a ledger that shows a stale number is
 * worse than one that shows none. A cached balance is a number that was true
 * at some point, presented with the same confidence as a true one, and there
 * is no way for a person holding a phone to tell which they are looking at.
 * Every page here is a query against a database that another member of the
 * household is also writing to.
 *
 * So exactly two things are cached, both of which cannot go stale because
 * neither says anything about money:
 *
 *   * the offline page, which says the server is not reachable
 *   * the stylesheet and the icon that page needs to not look broken
 *
 * Everything else goes to the network. When a *navigation* fails — the Pi is
 * off, Tailscale is asleep, the phone is on a plane — the offline page is
 * shown instead of the browser's dinosaur, because "can't reach home" is a
 * different sentence from "no internet" and only one of them is true.
 *
 * A POST that fails is deliberately not caught. Background Sync would let a
 * spend logged in a tunnel be replayed on landing, and it is the wrong feature
 * for this app: the FX rate, the account balance and the card's limit are all
 * checked at save time on the server, so a queued entry is one that has not
 * been checked yet and might be refused hours later, in front of nobody. The
 * form keeps what was typed; retry is a person pressing Save again.
 */

const VERSION = "hffs-v1";
const SHELL = [
  "/offline",
  "/static/css/app.css",
  "/static/img/icon-192.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(VERSION)
      .then((cache) => cache.addAll(SHELL))
      // Skip the usual wait-for-every-tab-to-close dance. There is nothing to
      // be careful about: no cached app to go out of step with a new one.
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(names.filter((n) => n !== VERSION).map((n) => caches.delete(n))),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;

  // Only navigations. A failed image or stylesheet should stay failed rather
  // than resolve to an HTML page, which is how a broken cache produces a
  // stylesheet full of "<!doctype html>".
  if (request.mode !== "navigate" || request.method !== "GET") return;

  event.respondWith(
    fetch(request).catch(async () => {
      const cache = await caches.open(VERSION);
      const page = await cache.match("/offline");
      return (
        page ||
        new Response("Can't reach the expenses server.", {
          status: 503,
          headers: { "Content-Type": "text/plain; charset=utf-8" },
        })
      );
    }),
  );
});
