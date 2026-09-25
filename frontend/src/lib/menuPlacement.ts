/**
 * Viewport placement for a fixed-position popover anchored to a trigger.
 *
 * Pure geometry: the caller measures the trigger and passes the size limits, so
 * the same numbers can be checked in a unit test without a real layout engine.
 */

/** The measured trigger, in viewport coordinates. */
export interface AnchorRect {
  top: number;
  bottom: number;
  left: number;
}

/** Where the menu goes: the anchored edge plus the size it may take. */
export type MenuPlacement = { left: number; width: number; maxHeight: number } & (
  | { side: "down"; top: number }
  | { side: "up"; bottom: number }
);

export function placeMenu({
  anchor,
  viewport,
  gap,
  margin,
  width,
  maxHeight,
}: {
  anchor: AnchorRect;
  viewport: { width: number; height: number };
  /** Clearance between the trigger and the menu. */
  gap: number;
  /** Clearance kept between the menu and every window edge. */
  margin: number;
  /** Preferred width; narrowed when the window cannot hold it. */
  width: number;
  /** Design cap; the menu still never grows past the room that is left. */
  maxHeight: number;
}): MenuPlacement {
  // Opens toward the roomier side, so the list gets the most usable height.
  // A tie keeps the historical direction: upward, away from the composer.
  const roomAbove = anchor.top - gap - margin;
  const roomBelow = viewport.height - anchor.bottom - gap - margin;
  const side = roomBelow > roomAbove ? "down" : "up";
  const room = Math.max(0, side === "down" ? roomBelow : roomAbove);

  const fitted = Math.min(width, viewport.width - margin * 2);
  const left = Math.min(Math.max(anchor.left, margin), viewport.width - margin - fitted);
  const height = Math.min(maxHeight, room);

  return side === "down"
    ? { side, left, width: fitted, maxHeight: height, top: anchor.bottom + gap }
    : { side, left, width: fitted, maxHeight: height, bottom: viewport.height - anchor.top + gap };
}
