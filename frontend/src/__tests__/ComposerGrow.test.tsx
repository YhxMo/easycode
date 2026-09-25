import { render } from "@testing-library/react";
import type { RefObject } from "react";
import { describe, expect, it } from "vitest";
import { ComposerBar } from "../components/layout/ComposerBar";

// jsdom has no layout, so the test controls the content height: the point here
// is the cap-and-shrink behaviour, not the metrics themselves.
function fieldProps(value: string, fieldRef: RefObject<HTMLTextAreaElement | null>, variant: "tall" | "docked" = "docked") {
  return {
    variant,
    value,
    placeholder: "p",
    ariaLabel: "field",
    busy: false,
    sendBlocked: false,
    permission: "ask",
    permissionDisabled: false,
    onPermission: () => {},
    onChange: () => {},
    onKeyDown: () => {},
    onSelectionChange: () => {},
    onSend: () => {},
    onStop: () => {},
    fieldRef,
  } as const;
}

function setup() {
  const ref: RefObject<HTMLTextAreaElement | null> = { current: null };
  const view = render(<ComposerBar {...fieldProps("", ref)} />);
  const setHeight = (height: number) => {
    Object.defineProperty(ref.current, "scrollHeight", { configurable: true, get: () => height });
  };
  const type = (value: string, variant: "tall" | "docked" = "docked") =>
    view.rerender(<ComposerBar {...fieldProps(value, ref, variant)} />);
  return { ref, setHeight, type, dom: () => ref.current! };
}

describe("ComposerBar · 输入框高度", () => {
  it("按内容增高，到上限后改为内部滚动", () => {
    const f = setup();
    f.setHeight(120);
    f.type("line\nline");
    expect(f.dom().style.height).toBe("120px");
    expect(f.dom().style.overflowY).toBe("hidden");

    f.setHeight(900);
    f.type("line\n".repeat(40));
    expect(f.dom().style.height).toBe("220px");
    expect(f.dom().style.overflowY).toBe("auto");
  });

  it("删空后回落到最小高度", () => {
    const f = setup();
    f.setHeight(300);
    f.type("text\nmore");
    expect(f.dom().style.height).toBe("220px");

    f.setHeight(40);
    f.type("");
    expect(f.dom().style.height).toBe("44px");
    expect(f.dom().style.overflowY).toBe("hidden");
  });

  it("新会话的居中输入框有更大的最小高度", () => {
    const f = setup();
    f.setHeight(10);
    f.type("", "tall");
    expect(f.dom().style.height).toBe("72px");
  });
});
