// Dictation belongs to the conversation it was started in: the composer
// switches draft keys when the user moves to another tab, while a transcript
// may arrive after that.
import { act, renderHook } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, expect, test, vi } from "vitest";

vi.mock("../api", () => ({
  fetchCommands: vi.fn(() => new Promise(() => {})),
  fetchFiles: vi.fn(() => new Promise(() => {})),
}));

import { useComposer } from "../features/composer/useComposer";
import { useDrafts } from "../features/composer/useDrafts";

class FakeRecognition {
  static last: FakeRecognition | null = null;
  lang = "";
  continuous = false;
  interimResults = false;
  onresult: ((event: unknown) => void) | null = null;
  onend: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor() {
    FakeRecognition.last = this;
  }
  start() {}
  stop() {}
  abort() {}
}

function transcript(text: string) {
  return {
    resultIndex: 0,
    results: [Object.assign([{ transcript: text }], { isFinal: true })],
  };
}

/** The composer for one conversation, plus the drafts store it edits. */
function renderComposer(initialKey: string) {
  const sticky = { unread: false, stick: () => {}, reveal: () => {}, forget: () => {} };
  return renderHook(
    ({ key }) => {
      const drafts = useDrafts();
      const fieldRef = useRef<HTMLTextAreaElement>(null);
      const composer = useComposer({
        key,
        drafts,
        sessionId: key === "draft" ? null : key,
        sessionRoot: null,
        draftRoot: null,
        secondary: [],
        projectsSignature: "",
        extensionsRevision: 0,
        send: async () => {},
        sendEdit: async () => {},
        cancelEdit: () => {},
        sticky,
        fieldRef,
      });
      return { composer, drafts };
    },
    { initialProps: { key: initialKey } },
  );
}

afterEach(() => {
  delete (window as unknown as { SpeechRecognition?: unknown }).SpeechRecognition;
});

test("听写结果落进开始听写时的那份草稿，不是挂载时的那份", () => {
  (window as unknown as { SpeechRecognition: unknown }).SpeechRecognition = FakeRecognition;
  const { result, rerender } = renderComposer("draft");
  rerender({ key: "sess-b" });
  act(() => result.current.composer.voice.toggle());
  act(() => FakeRecognition.last?.onresult?.(transcript("你好")));
  expect(result.current.drafts.get("sess-b").text).toBe("你好");
  expect(result.current.drafts.get("draft").text).toBe("");
});

test("听写中途切走，迟到的转写仍落在开始听写的那份草稿", () => {
  (window as unknown as { SpeechRecognition: unknown }).SpeechRecognition = FakeRecognition;
  const { result, rerender } = renderComposer("sess-b");
  act(() => result.current.composer.voice.toggle());
  rerender({ key: "sess-c" });
  act(() => FakeRecognition.last?.onresult?.(transcript("你好")));
  expect(result.current.drafts.get("sess-b").text).toBe("你好");
  expect(result.current.drafts.get("sess-c").text).toBe("");
});
