Content scripts live here. `block-overlay.js` runs at `document_start` on every
http(s) page and only acts on a decision handed to it by the service worker
(which got it from the local agent). It never touches the network itself.
