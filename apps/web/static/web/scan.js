/* Scan tag's camera (slice 18): the Scan tag modal and page (apps/web/views_scan.py, web/_scan_form.html). Plain JavaScript, no
   libraries.

   A handheld barcode scanner needs none of this: it types the code into the field and presses Enter (a keyboard wedge). Where the
   browser can read codes from a camera (BarcodeDetector, navigator.mediaDevices.getUserMedia, a secure context, and at least one of
   FORMATS), the form's camera block shows "Use camera": it starts the rear camera in the block's <video>, reads a frame about every
   200 ms, and on the first code read fills the field and submits the form (the modal's through HTMX, the page's as a plain GET).
   The camera stops on success, on Stop camera, when the block leaves the page (the modal closes or its content is swapped), and
   when the page is hidden. Anywhere else the block keeps its one line saying the browser cannot read codes with the camera (the
   markup's default, so it also shows without JavaScript). Nothing here may throw on a browser without these APIs.

   Set up on htmx:afterSwap and htmx:load (the modal swapped in, or a page restored from history) and on DOMContentLoaded (the page
   opened directly).
   The field keeps the last code selected, so the next scan replaces it. */
(function () {
  "use strict";
  const FORMATS = ["qr_code", "code_128", "code_39", "data_matrix", "ean_13", "upc_a", "itf"];
  const EVERY_MS = 200;
  const ready = new WeakSet();  // blocks already set up (htmx:load and DOMContentLoaded can both reach the page's)
  let session = null;  // the camera running now, at most one: {block, stream, timer, observer, busy}

  function part(block, name) { return block.querySelector("[data-scan-" + name + "]"); }
  function show(el, on) { if (el) el.hidden = !on; }
  function say(block, text) { const s = part(block, "status"); if (s) s.textContent = text; }

  function cameraPossible() {
    try {
      return "BarcodeDetector" in window && window.isSecureContext === true && !!navigator.mediaDevices &&
        typeof navigator.mediaDevices.getUserMedia === "function";
    } catch (e) {
      return false;
    }
  }

  async function formats() {
    try {
      const offered = await window.BarcodeDetector.getSupportedFormats();
      return FORMATS.filter((f) => offered.indexOf(f) !== -1);
    } catch (e) {
      return [];
    }
  }

  function stop() {
    const s = session;
    if (!s) return;
    session = null;
    clearInterval(s.timer);
    if (s.observer) s.observer.disconnect();
    try { s.stream.getTracks().forEach((t) => t.stop()); } catch (e) { /* already ended */ }
    const video = part(s.block, "video");
    if (video) {
      try { video.pause(); } catch (e) { /* not playing */ }
      video.srcObject = null;
      show(video, false);
    }
    show(part(s.block, "start"), true);
    show(part(s.block, "stop"), false);
  }

  function found(block, code) {
    stop();
    const form = block.closest("form");
    const field = form && form.querySelector("[data-scan-field]");
    if (!field) return;
    field.value = code;
    say(block, "");
    if (typeof form.requestSubmit === "function") form.requestSubmit();  // a submit event, which HTMX takes in the modal
    else form.submit();
  }

  async function start(block) {
    if (block.dataset.scanStarting || (session && session.block === block)) return;
    stop();
    block.dataset.scanStarting = "1";
    say(block, "");
    let detector, stream;
    try {
      detector = new window.BarcodeDetector({ formats: await formats() });
      stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" } }, audio: false });
    } catch (e) {
      say(block, "The camera did not start. Allow this site to use the camera, or use a handheld scanner or type the tag.");
      return;
    } finally {
      delete block.dataset.scanStarting;
    }
    if (!block.isConnected || document.hidden) {  // the modal closed, or the page was left, while the browser asked
      stream.getTracks().forEach((t) => t.stop());
      return;
    }
    const video = part(block, "video");
    const s = { block: block, stream: stream, timer: 0, observer: null, busy: false };
    session = s;
    video.srcObject = stream;
    show(video, true);
    show(part(block, "start"), false);
    show(part(block, "stop"), true);
    say(block, "Point the camera at the label's code.");
    try { await video.play(); } catch (e) { /* muted and inline, so it plays; if not, the frames are still read */ }
    s.observer = new MutationObserver(() => { if (!block.isConnected) stop(); });
    s.observer.observe(document.body, { childList: true, subtree: true });
    s.timer = setInterval(async () => {
      if (session !== s) return;
      if (!block.isConnected) { stop(); return; }
      if (s.busy || video.readyState < 2) return;  // still reading the last frame, or no frame yet
      s.busy = true;
      try {
        const codes = await detector.detect(video);
        const code = codes.map((c) => (c.rawValue || "").trim()).find((v) => v);
        if (code && session === s) found(block, code);
      } catch (e) {
        /* a frame that could not be read: try the next */
      } finally {
        s.busy = false;
      }
    }, EVERY_MS);
  }

  function setUp(block) {
    if (ready.has(block)) return;
    ready.add(block);
    // A page restored from history brings back whatever the block showed when it was saved: start from the markup's state.
    show(part(block, "video"), false);
    show(part(block, "stop"), false);
    show(part(block, "start"), false);
    say(block, "");
    if (!cameraPossible()) return;  // the "cannot read codes" line stays
    show(part(block, "none"), false);
    formats().then((list) => {
      show(part(block, "start"), list.length > 0);
      show(part(block, "none"), list.length === 0);
    });
  }

  function init(root) {
    if (!root || !root.querySelectorAll) return;
    const blocks = Array.from(root.querySelectorAll("[data-scan-camera]"));
    if (root.matches && root.matches("[data-scan-camera]")) blocks.push(root);
    blocks.forEach(setUp);
    const field = root.matches && root.matches("[data-scan-field]") ? root : root.querySelector("[data-scan-field]");
    if (field) setTimeout(() => { field.focus(); field.select(); }, 40);  // after the shell's own focus on the modal's field
  }

  document.addEventListener("click", (e) => {
    const el = e.target instanceof Element ? e.target : null;
    const button = el && el.closest("[data-scan-start], [data-scan-stop]");
    const block = button && button.closest("[data-scan-camera]");
    if (!block) return;
    if (button.hasAttribute("data-scan-start")) start(block); else stop();
  });
  document.addEventListener("htmx:load", (e) => init(e.target));
  // Also right after the swap, before the settle that fires htmx:load: the block then never shows its "cannot" line for a moment.
  document.addEventListener("htmx:afterSwap", (e) => init(e.detail && e.detail.target));
  document.addEventListener("htmx:beforeSwap", (e) => {
    const target = e.detail && e.detail.target;
    if (session && target && target.contains(session.block)) stop();
  });
  document.addEventListener("modal-close", stop);
  document.addEventListener("visibilitychange", () => { if (document.hidden) stop(); });
  window.addEventListener("pagehide", stop);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => init(document));
  else init(document);
})();
