"use strict";
// Minimal service worker: lets Android/desktop show system notifications for alerts and
// focuses the dashboard when one is tapped. It caches nothing.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));
self.addEventListener("notificationclick", event => {
  event.notification.close();
  event.waitUntil(self.clients.matchAll({type:"window", includeUncontrolled:true}).then(list => {
    const open = list.find(c => new URL(c.url).pathname === "/");
    return open ? open.focus() : self.clients.openWindow("/#events");
  }));
});
