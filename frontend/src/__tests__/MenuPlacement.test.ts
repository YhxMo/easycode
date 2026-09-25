import { describe, expect, it } from "vitest";
import { placeMenu } from "../lib/menuPlacement";

// The model menu is fixed-positioned in viewport coordinates. jsdom computes no
// layout, so the geometry that decides whether the list is reachable is checked
// here as a pure function, and the browser check covers the rendering.
const VIEWPORT = { width: 1440, height: 900 };
const BOX = { gap: 8, margin: 12, width: 270, maxHeight: 460 };

describe("placeMenu", () => {
  it("上方空间更多时向上展开，底边贴住触发器", () => {
    const p = placeMenu({ ...BOX, anchor: { top: 800, bottom: 842, left: 900 }, viewport: VIEWPORT });
    expect(p.side).toBe("up");
    if (p.side !== "up") throw new Error("unreachable");
    expect(p.bottom).toBe(VIEWPORT.height - 800 + BOX.gap);
    // Room above is generous, so the design cap is what limits the height.
    expect(p.maxHeight).toBe(BOX.maxHeight);
  });

  it("下方空间更多时向下展开，顶边贴住触发器", () => {
    const p = placeMenu({ ...BOX, anchor: { top: 60, bottom: 102, left: 40 }, viewport: VIEWPORT });
    expect(p.side).toBe("down");
    if (p.side !== "down") throw new Error("unreachable");
    expect(p.top).toBe(102 + BOX.gap);
    expect(p.maxHeight).toBe(BOX.maxHeight);
  });

  it("空间不足时不再套用最小高度：菜单不会顶出视口", () => {
    // A short window with the trigger near the middle: neither side can hold
    // the design cap, and the old `Math.max(96, …)` floor used to win anyway.
    const p = placeMenu({
      ...BOX,
      anchor: { top: 100, bottom: 142, left: 40 },
      viewport: { width: 1440, height: 200 },
    });
    expect(p.side).toBe("up");
    if (p.side !== "up") throw new Error("unreachable");
    expect(p.maxHeight).toBe(100 - BOX.gap - BOX.margin);
    expect(p.maxHeight).toBeLessThan(96);
    // The panel's top edge stays inside the margin, so nothing is cropped.
    const top = 200 - p.bottom - p.maxHeight;
    expect(top).toBeGreaterThanOrEqual(BOX.margin - 0.001);
  });

  it("视口两侧都留出边距：窄窗口收窄宽度，靠近边缘时平移回视口内", () => {
    const p = placeMenu({ ...BOX, anchor: { top: 700, bottom: 740, left: 1400 }, viewport: VIEWPORT });
    expect(p.left + p.width).toBeLessThanOrEqual(VIEWPORT.width - BOX.margin);
    expect(p.left).toBeGreaterThanOrEqual(BOX.margin);

    const narrow = placeMenu({ ...BOX, anchor: { top: 700, bottom: 740, left: 0 }, viewport: { width: 240, height: 900 } });
    expect(narrow.width).toBe(240 - BOX.margin * 2);
    expect(narrow.left).toBe(BOX.margin);
  });
});
