const REVERSE_MESSAGE_SCROLL_SELECTOR =
  '[class*="chat-anywhere-message-list-bubble-scroll"]' +
  '[class*="bubble-list-order-desc"]';

const LINE_HEIGHT_PX = 16;
const SCROLL_TOLERANCE_PX = 1;

function wheelDeltaInPixels(
  deltaY: number,
  deltaMode: number,
  clientHeight: number,
): number {
  if (deltaMode === 1) return deltaY * LINE_HEIGHT_PX;
  if (deltaMode === 2) return deltaY * clientHeight;
  return deltaY;
}

export function getNextReverseScrollTop(
  scrollTop: number,
  scrollHeight: number,
  clientHeight: number,
  deltaY: number,
  deltaMode: number,
): number {
  const maxScrollDistance = Math.max(scrollHeight - clientHeight, 0);
  const pixelDelta = wheelDeltaInPixels(deltaY, deltaMode, clientHeight);

  return Math.min(0, Math.max(-maxScrollDistance, scrollTop + pixelDelta));
}

function canConsumeVerticalWheel(
  element: HTMLElement,
  deltaY: number,
): boolean {
  const maxScrollTop = element.scrollHeight - element.clientHeight;
  if (maxScrollTop <= SCROLL_TOLERANCE_PX) return false;

  // Most ancestors inside a message cannot scroll. Check their inexpensive
  // geometry first so the wheel hot path does not force style resolution for
  // every nested Markdown node.
  const overflowY = window.getComputedStyle(element).overflowY;
  if (overflowY !== "auto" && overflowY !== "scroll") return false;

  if (deltaY < 0) return element.scrollTop > SCROLL_TOLERANCE_PX;
  return element.scrollTop < maxScrollTop - SCROLL_TOLERANCE_PX;
}

function hasScrollableChildBeforeScroller(
  target: Element,
  scroller: HTMLElement,
  deltaY: number,
): boolean {
  let current: HTMLElement | null =
    target instanceof HTMLElement ? target : target.parentElement;

  while (current && current !== scroller) {
    if (canConsumeVerticalWheel(current, deltaY)) return true;
    current = current.parentElement;
  }

  return false;
}

export function scrollReverseMessageList(
  root: HTMLElement,
  target: EventTarget | null,
  deltaY: number,
  deltaMode: number,
): boolean {
  if (!(target instanceof Element) || deltaY === 0) return false;

  const scroller = target.closest<HTMLElement>(REVERSE_MESSAGE_SCROLL_SELECTOR);
  if (!scroller || !root.contains(scroller)) return false;
  if (hasScrollableChildBeforeScroller(target, scroller, deltaY)) return false;

  const nextScrollTop = getNextReverseScrollTop(
    scroller.scrollTop,
    scroller.scrollHeight,
    scroller.clientHeight,
    deltaY,
    deltaMode,
  );
  if (nextScrollTop === scroller.scrollTop) return false;

  scroller.scrollTop = nextScrollTop;
  return true;
}

/** Keep a visible message anchored while the user explicitly pauses following.
 * The SDK can scroll after its message effect, so restore on DOM/size changes
 * and scrolling. Never patch the SDK or global DOM scrolling methods.
 */
export function installMessageScrollLock(root: HTMLElement): () => void {
  let scroller: HTMLElement | null = null;
  let anchor: HTMLElement | null = null;
  let anchorTop = 0;
  let restoreFrame = 0;
  let inputFrame = 0;
  let userScrolling = false;
  let disposed = false;
  let pointerDown = false;
  const resize = new ResizeObserver(scheduleRestore);

  const capture = () => {
    const next = root.querySelector<HTMLElement>(
      REVERSE_MESSAGE_SCROLL_SELECTOR,
    );
    if (scroller !== next) {
      scroller?.removeEventListener("scroll", scheduleRestore);
      resize.disconnect();
      if (next) {
        resize.observe(next);
        Array.from(next.children).forEach((child) => resize.observe(child));
      }
      scroller = next;
      scroller?.addEventListener("scroll", scheduleRestore, { passive: true });
    }
    if (!scroller) return;
    const bounds = scroller.getBoundingClientRect();
    anchor =
      Array.from(
        scroller.querySelectorAll<HTMLElement>(
          '[class*="-bubble-content-wrapper"]',
        ),
      )
        .filter((item) => {
          const rect = item.getBoundingClientRect();
          return rect.bottom > bounds.top && rect.top < bounds.bottom;
        })
        .sort(
          (a, b) =>
            a.getBoundingClientRect().top - b.getBoundingClientRect().top,
        )[0] ?? null;
    if (anchor) anchorTop = anchor.getBoundingClientRect().top;
  };

  const restore = () => {
    restoreFrame = 0;
    if (disposed || userScrolling) return;
    if (!scroller || !anchor || !scroller.contains(anchor)) {
      capture();
      return;
    }
    const delta = anchor.getBoundingClientRect().top - anchorTop;
    if (Math.abs(delta) > SCROLL_TOLERANCE_PX) scroller.scrollTop += delta;
  };

  function scheduleRestore() {
    if (!restoreFrame && !userScrolling) {
      restoreFrame = requestAnimationFrame(restore);
    }
  }

  const handleUserInput = () => {
    userScrolling = true;
    cancelAnimationFrame(inputFrame);
    // Host wheel handling runs before us; native touch/keyboard movement
    // completes before the frame. Capture the user's resulting reading point.
    inputFrame = requestAnimationFrame(() => {
      inputFrame = 0;
      capture();
      userScrolling = pointerDown;
    });
  };
  const pointerStart = () => {
    pointerDown = true;
    userScrolling = true;
  };
  const pointerEnd = () => {
    if (!pointerDown) return;
    pointerDown = false;
    handleUserInput();
  };
  capture();
  const mutations = new MutationObserver(scheduleRestore);
  mutations.observe(root, {
    childList: true,
    subtree: true,
    characterData: true,
  });
  root.addEventListener("wheel", handleUserInput, {
    capture: true,
    passive: true,
  });
  root.addEventListener("touchmove", handleUserInput, {
    capture: true,
    passive: true,
  });
  root.addEventListener("keydown", handleUserInput, true);
  root.addEventListener("pointerdown", pointerStart, true);
  window.addEventListener("pointerup", pointerEnd, true);
  window.addEventListener("pointercancel", pointerEnd, true);

  return () => {
    disposed = true;
    cancelAnimationFrame(restoreFrame);
    cancelAnimationFrame(inputFrame);
    mutations.disconnect();
    resize.disconnect();
    scroller?.removeEventListener("scroll", scheduleRestore);
    root.removeEventListener("wheel", handleUserInput, true);
    root.removeEventListener("touchmove", handleUserInput, true);
    root.removeEventListener("keydown", handleUserInput, true);
    root.removeEventListener("pointerdown", pointerStart, true);
    window.removeEventListener("pointerup", pointerEnd, true);
    window.removeEventListener("pointercancel", pointerEnd, true);
  };
}
