import { useCallback, useEffect, useState, type RefObject } from "react";

const ACTIONS = [
  { id: "explain", label: "解释", instruction: "解释这段内容：" },
  { id: "improve", label: "改进", instruction: "改进这段内容：" },
  { id: "shorten", label: "缩短", instruction: "缩短这段内容：" },
];

interface Props {
  /** Only selections inside this element raise the bar. */
  containerRef: RefObject<HTMLElement | null>;
  onPick: (quoted: string, instruction: string) => void;
}

/** Selection bar: quote part of a reply and send it back with an instruction. */
export function SelectionActions({ containerRef, onPick }: Props) {
  const [target, setTarget] = useState<{ text: string; top: number; left: number } | null>(null);

  const clear = useCallback(() => setTarget(null), []);

  useEffect(() => {
    const onMouseUp = () => {
      const selection = window.getSelection();
      const text = selection?.toString().trim() ?? "";
      const container = containerRef.current;
      if (!text || !selection || !selection.rangeCount || !container) {
        setTarget(null);
        return;
      }
      const range = selection.getRangeAt(0);
      if (!container.contains(range.commonAncestorContainer)) {
        setTarget(null);
        return;
      }
      const rect = range.getBoundingClientRect();
      setTarget({ text, top: rect.top - 44, left: rect.left + rect.width / 2 });
    };
    const onScroll = () => setTarget(null);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setTarget(null);
    };
    document.addEventListener("mouseup", onMouseUp);
    document.addEventListener("mousedown", clear);
    document.addEventListener("scroll", onScroll, true);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mouseup", onMouseUp);
      document.removeEventListener("mousedown", clear);
      document.removeEventListener("scroll", onScroll, true);
      document.removeEventListener("keydown", onKey);
    };
  }, [containerRef, clear]);

  if (!target) return null;
  return (
    <div className="selection-bar" style={{ top: target.top, left: target.left }} role="toolbar">
      {ACTIONS.map((action) => (
        <button
          key={action.id}
          type="button"
          onClick={() => {
            onPick(target.text, action.instruction);
            setTarget(null);
          }}
        >
          {action.label}
        </button>
      ))}
    </div>
  );
}
