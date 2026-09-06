/**
 * Radial Quick-Entry Dial (Task U47)
 * Implements a hand-rolled press-drag-release gesture for a radial dial menu.
 */
document.addEventListener("DOMContentLoaded", function () {
  const container = document.getElementById("radial-container");
  if (!container) return;

  const svg = container.querySelector(".radial-dial");
  if (!svg) return;

  const wedges = Array.from(container.querySelectorAll(".radial-wedge"));
  const centerText = container.querySelector(".radial-center-text");
  const initialCenterText = centerText ? centerText.textContent : "Drag to select";

  let isDragging = false;
  let activeIndex = -1;

  function getCenter() {
    const rect = svg.getBoundingClientRect();
    return {
      x: rect.left + rect.width / 2,
      y: rect.top + rect.height / 2
    };
  }

  function getWedgeIndexFromPointer(e) {
    const center = getCenter();
    let clientX = e.clientX;
    let clientY = e.clientY;

    if (clientX === undefined && e.touches && e.touches.length > 0) {
      clientX = e.touches[0].clientX;
      clientY = e.touches[0].clientY;
    }

    if (clientX === undefined || clientY === undefined) return activeIndex;

    const dx = clientX - center.x;
    const dy = clientY - center.y;

    // Angle in degrees from 12 o'clock (-90 deg):
    const rad = Math.atan2(dy, dx);
    const deg = rad * (180 / Math.PI);
    const angle = (deg + 90 + 360) % 360;

    const count = wedges.length; // 14
    if (count === 0) return -1;
    const wedgeAngle = 360 / count;
    const index = Math.floor(angle / wedgeAngle) % count;
    return index;
  }

  function highlightWedge(index) {
    wedges.forEach((w, i) => {
      if (i === index) {
        w.classList.add("active");
        const title = w.getAttribute("data-title") || w.getAttribute("aria-label");
        if (centerText && title) {
          centerText.textContent = title;
        }
      } else {
        w.classList.remove("active");
      }
    });
    activeIndex = index;
  }

  function clearHighlight() {
    wedges.forEach((w) => w.classList.remove("active"));
    activeIndex = -1;
    if (centerText) centerText.textContent = initialCenterText;
  }

  function triggerWedge(index) {
    if (index < 0 || index >= wedges.length) return;
    const wedge = wedges[index];
    const hxGet = wedge.getAttribute("hx-get") || wedge.getAttribute("data-hx-get");
    const href = wedge.getAttribute("href") || wedge.getAttribute("xlink:href");
    const postUrl = wedge.getAttribute("data-post");

    if (postUrl) {
      if (typeof window.htmx !== "undefined") {
        window.htmx.ajax("POST", postUrl, { target: "#toast", swap: "innerHTML" });
      } else {
        const form = document.querySelector(`form[action="${postUrl}"]`);
        if (form) form.submit();
      }
    } else if (hxGet) {
      if (typeof window.htmx !== "undefined") {
        window.htmx.ajax("GET", hxGet, { select: "#panel", target: "#panel", swap: "outerHTML" });
      } else if (href) {
        window.location.href = href;
      }
    } else if (href) {
      window.location.href = href;
    }
  }

  function onPointerDown(e) {
    isDragging = true;
    if (svg.setPointerCapture && e.pointerId !== undefined) {
      try {
        svg.setPointerCapture(e.pointerId);
      } catch (_) {}
    }
    const idx = getWedgeIndexFromPointer(e);
    highlightWedge(idx);
    if (e.cancelable) e.preventDefault();
  }

  function onPointerMove(e) {
    if (!isDragging) return;
    const idx = getWedgeIndexFromPointer(e);
    highlightWedge(idx);
    if (e.cancelable) e.preventDefault();
  }

  function onPointerUp(e) {
    if (!isDragging) return;
    isDragging = false;
    const idx = activeIndex >= 0 ? activeIndex : getWedgeIndexFromPointer(e);
    clearHighlight();
    triggerWedge(idx);
    if (e.cancelable) e.preventDefault();
  }

  function onPointerCancel() {
    isDragging = false;
    clearHighlight();
  }

  if (window.PointerEvent) {
    svg.addEventListener("pointerdown", onPointerDown);
    svg.addEventListener("pointermove", onPointerMove);
    svg.addEventListener("pointerup", onPointerUp);
    svg.addEventListener("pointercancel", onPointerCancel);
  }

  // Fallback / standard touch listeners
  svg.addEventListener("touchstart", onPointerDown, { passive: false });
  svg.addEventListener("touchmove", onPointerMove, { passive: false });
  svg.addEventListener("touchend", onPointerUp, { passive: false });
  svg.addEventListener("touchcancel", onPointerCancel, { passive: false });
});
