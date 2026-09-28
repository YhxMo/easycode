import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import { fetchCommands, fetchFiles } from "../../api";
import type { CommandInfo, FileEntry } from "../../api";
import type { StickToBottom } from "../../lib/useStickToBottom";
import { isEditing, type DraftStore, type EditDraft } from "./useDrafts";
import { applyMention, mentionToken } from "./mention";
import {
  activeCommandId,
  clampCommandIndex,
  filterCommands,
  moveCommandCursor,
} from "./commands";
import type { CommandMenuState } from "./CommandMenu";
import type { MentionAnswer, MentionState } from "./MentionMenu";
import { useVoiceInput } from "./useVoiceInput";

/** The last answer for a `@` query, tagged with the scope and query it answered. */
interface MentionReply {
  scope: string;
  query: string;
  answer: MentionAnswer;
}

export interface ComposerInputs {
  /** The conversation whose draft this composer edits. */
  key: string;
  drafts: DraftStore;
  /** Scope of the two menus: which conversation, which project. */
  sessionId: string | null;
  sessionRoot: string | null;
  draftRoot: string | null;
  secondary: string[];
  /** Changes whenever the registered projects do, so the menu re-reads them. */
  projectsSignature: string;
  /** Bumped when the extensions dialog installs or saves something. */
  extensionsRevision: number;
  send: () => void | Promise<void>;
  sendEdit: (draft: EditDraft, commandId: string | null) => void | Promise<void>;
  /** Leave edit mode; the composer's own Escape falls through to it. */
  cancelEdit: () => void;
  sticky: StickToBottom;
  fieldRef: React.RefObject<HTMLTextAreaElement | null>;
}

/**
 * The composer: its draft edits, its two menus, and whose keystroke goes where.
 *
 * The text and the caret live in the drafts store (one entry per conversation),
 * so this hook reads and writes them through it rather than holding a copy; what
 * it owns is everything about *presenting* that text — the `/` catalogue, the
 * `@` file listing, the menu cursors, and the IME boundary.
 */
export function useComposer({
  key,
  drafts,
  sessionId,
  sessionRoot,
  draftRoot,
  secondary,
  projectsSignature,
  extensionsRevision,
  send,
  sendEdit,
  cancelEdit,
  sticky,
  fieldRef,
}: ComposerInputs) {
  const draft = drafts.get(key);
  const input = draft.text;
  const caret = draft.caret;
  // Plain closures: the drafts store is rebuilt on every keystroke (its `get`
  // closes over the map), so an explicit memo here would never hold anyway —
  // the compiler memoizes around it instead.
  const setInput = (v: string) => drafts.update(key, (d) => ({ ...d, text: v }));
  const setCaret = (c: number) => drafts.update(key, (d) => ({ ...d, caret: c }));

  /** The edit this composer is holding, if any (the banner names its target). */
  const editingDraft = isEditing(draft) ? draft : null;

  const [commandAnswer, setCommandAnswer] = useState<CommandMenuState>({ status: "loading" });
  // Bumped by the menu's retry, and by a change in the registered projects.
  const [commandRetry, setCommandRetry] = useState(0);
  const [cmdOpen, setCmdOpen] = useState(false);
  const [cmdIndex, setCmdIndex] = useState(0);
  // `@` file references: the token under the caret and whether the user
  // dismissed the menu for this token.
  const [mentionOff, setMentionOff] = useState(false);
  const [mentionIndex, setMentionIndex] = useState(0);
  // A token without a matching answer is still being looked up, so "searching"
  // never has to be written into state and a completed-but-empty result stays
  // distinguishable from a failure.
  const [mentionAnswer, setMentionAnswer] = useState<MentionReply | null>(null);
  // Bumped by the menu's retry: the effect below is what owns the request.
  const [mentionRetry, setMentionRetry] = useState(0);

  // IME composition (Chinese/Japanese input) owns the field while it runs. The
  // ref guards the caret restore below — writing a selection into the field
  // mid-composition is what turns a pinyin buffer into stray latin text — and
  // the state gates everything derived from the token under the caret.
  const composingRef = useRef(false);
  const [composing, setComposing] = useState(false);
  /**
   * The field reported new text. While an IME is composing, the controlled
   * value still has to follow the field — React would otherwise write the stale
   * text back over the IME's buffer — but nothing else moves until the
   * candidate lands.
   */
  const handleChange = (value: string, nextCaret: number) => {
    if (composingRef.current) {
      setInput(value);
      return;
    }
    syncComposer(value, nextCaret);
  };

  const handleComposingChange = (next: boolean) => {
    composingRef.current = next;
    setComposing(next);
  };

  // Restore the caret the draft remembers, but never mid-composition: writing a
  // selection into the field while an IME owns it is what turns a pinyin buffer
  // into stray latin text.
  useEffect(() => {
    const field = fieldRef.current;
    if (!field || composingRef.current) return;
    const pos = Math.min(drafts.get(key).caret, field.value.length);
    if (field.selectionStart !== pos || field.selectionEnd !== pos) {
      field.setSelectionRange(pos, pos);
    }
  }, [key, drafts, fieldRef]);

  /** The field's new text and caret, once the user (not the IME) is done editing. */
  const syncComposer = (value: string, nextCaret: number) => {
    setInput(value);
    setCaret(nextCaret);
    setCmdOpen(value.startsWith("/"));
    setCmdIndex(0);
    setMentionIndex(0);
    setMentionOff(false);
  };

  const commandScope = sessionId ? `s:${sessionId}` : `d:${draftRoot ?? ""}`;
  // Newest request wins: a reply for a menu that has since changed scope must
  // not overwrite the current one, and the sequence is also what keeps a reply
  // that arrives after the view moved to another project.
  const commandsSeqRef = useRef(0);
  useEffect(() => {
    const seq = ++commandsSeqRef.current;
    fetchCommands(sessionId, sessionId ? sessionRoot : draftRoot)
      .then((r) => {
        if (seq !== commandsSeqRef.current) return;
        setCommandAnswer({
          status: "ready",
          scope: commandScope,
          commands: r.commands,
          errors: r.errors,
        });
      })
      .catch((e: unknown) => {
        if (seq !== commandsSeqRef.current) return;
        setCommandAnswer({
          status: "error",
          message: e instanceof Error ? e.message : String(e),
        });
      });
  }, [
    commandScope,
    commandRetry,
    projectsSignature,
    extensionsRevision,
    sessionId,
    sessionRoot,
    draftRoot,
  ]);
  // An answer describes the scope it was fetched for. Until this render's
  // scope has one, the menu is loading: it must never offer the previous
  // project's services in the moment before the effect runs.
  const commandState: CommandMenuState = useMemo(
    () =>
      commandAnswer.status === "ready" && commandAnswer.scope !== commandScope
        ? { status: "loading" }
        : commandAnswer,
    [commandAnswer, commandScope],
  );
  // A stable list per state: the keyboard handler below depends on it.
  const commands = useMemo(
    () => (commandState.status === "ready" ? commandState.commands : []),
    [commandState],
  );

  // `@` references: fetch the listing for the token under the caret, debounced
  // so typing does not fire a request per keystroke. A reply is used only while
  // it still matches the scope and the query it was fetched for.
  const mentionInfo = useMemo(
    () => (composing ? null : mentionToken(input, caret)),
    [input, caret, composing],
  );
  const mentionScope = JSON.stringify([sessionId, draftRoot, secondary]);
  const mentionQuery = mentionInfo?.query ?? null;
  // A bare `@` asks for a file name instead of listing the tree, so the menu
  // never opens with noise the reader did not ask for (hidden files stay
  // findable by typing).
  const mentionSearching = mentionQuery !== null && mentionQuery.trim() !== "";
  useEffect(() => {
    if (!mentionSearching) return;
    const draftRequest = sessionId === null ? { root: draftRoot, secondary } : undefined;
    const scope = mentionScope;
    const query = mentionQuery;
    let live = true;
    // Debounced so typing does not fire a request per keystroke.
    const timer = window.setTimeout(() => {
      fetchFiles(sessionId, draftRequest, query)
        .then((r) => {
          if (live) {
            setMentionAnswer({
              scope,
              query,
              answer: { status: "ready", files: r.files, total: r.total },
            });
          }
        })
        .catch((e: unknown) => {
          if (live) {
            setMentionAnswer({
              scope,
              query,
              answer: { status: "error", message: e instanceof Error ? e.message : String(e) },
            });
          }
        });
    }, 140);
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
  }, [mentionSearching, mentionQuery, mentionScope, sessionId, draftRoot, secondary, mentionRetry]);

  const mentionOpen = Boolean(mentionInfo) && !mentionOff && !cmdOpen;
  // A reply only counts while it still answers the token under the caret; a
  // token without one is either asking for a query or still being looked up.
  const mentionCurrent: MentionState | null = useMemo(() => {
    if (!mentionInfo) return null;
    if (!mentionSearching) {
      return { status: "prompt", scope: mentionScope, query: mentionInfo.query };
    }
    const answered = mentionAnswer;
    if (answered && answered.scope === mentionScope && answered.query === mentionInfo.query) {
      return { ...answered.answer, scope: answered.scope, query: answered.query };
    }
    return { status: "loading", scope: mentionScope, query: mentionInfo.query };
  }, [mentionInfo, mentionSearching, mentionAnswer, mentionScope]);
  const mentionMatches = useMemo(
    () => (mentionCurrent?.status === "ready" ? mentionCurrent.files : []),
    [mentionCurrent],
  );

  /**
   * Picking a menu entry drops its command into the composer, selection and
   * all, in one write: the text and what it means are stored together so no
   * other conversation can inherit one without the other.
   */
  const pickCommand = useCallback(
    (c: CommandInfo) => {
      const text = `/${c.name} `;
      drafts.update(key, (d) => ({
        ...d,
        text,
        caret: text.length,
        command: { id: c.id, name: c.name },
      }));
      setCmdOpen(false);
      setCmdIndex(0);
    },
    [drafts, key],
  );

  const pickMention = (file: FileEntry) => {
    const token = mentionInfo;
    if (!token) return;
    const next = applyMention(input, token, file.absolute_path);
    setInput(next.value);
    setCaret(next.caret);
    setMentionOff(true);
    window.requestAnimationFrame(() => {
      fieldRef.current?.setSelectionRange(next.caret, next.caret);
    });
  };

  /** Dictated text appends to the draft the dictation was started in. */
  const appendToDraft = useCallback(
    (target: string, text: string) =>
      drafts.update(target, (d) => ({ ...d, text: d.text ? `${d.text} ${text}` : text })),
    [drafts],
  );
  // Draft key whose dictation is running, so a late transcript lands in the
  // conversation it was spoken into.
  const voiceKeyRef = useRef(key);
  const voice = useVoiceInput((text) => appendToDraft(voiceKeyRef.current, text));
  const toggleVoice = useCallback(() => {
    // Remember where this dictation belongs: a transcript that arrives after
    // the user switched away must not append to the new conversation.
    if (!voice.listening) voiceKeyRef.current = key;
    voice.toggle();
  }, [voice, key]);

  /**
   * Send what the composer holds, whichever way it was asked for.
   *
   * One exit for both the Enter key and the send button: it decides between a
   * plain send and replacing the message being edited, so the two can never
   * disagree about what the draft means.
   */
  const submit = useCallback(() => {
    // Sending is an explicit return to the live edge: the reply to your own
    // message is never something you have to scroll back down for.
    sticky.stick();
    // The composer's text has been consumed, so the menu that helped write it
    // goes with it. A hand-typed `/mcp:demo 任务` whose query matches nothing
    // still sends on Enter — leaving its scrim over the conversation would
    // block every click until the user found Escape.
    setCmdOpen(false);
    // The same value the stream was handed: one source for every send, whether
    // it starts a turn or replaces one.
    if (editingDraft) void sendEdit(editingDraft, activeCommandId(draft.command, draft.text));
    else void send();
  }, [draft, editingDraft, send, sendEdit, sticky]);

  // Composer keys: command-menu navigation first, then Enter to send.
  const onKeyDown = (e: ReactKeyboardEvent<HTMLTextAreaElement>) => {
      // IME composition (Chinese/Japanese input): Enter confirms the candidate
      // text and must never send or pick commands.
      if (e.nativeEvent.isComposing || e.keyCode === 229) return;
      if (e.key === "Escape") {
        // Menus are the innermost context: one is dismissed before the edit is.
        if (mentionOpen) setMentionOff(true);
        else if (cmdOpen) setCmdOpen(false);
        else cancelEdit();
        return;
      }
      // While the file menu is up, Enter belongs to it: picking a reference is
      // the only way forward, so a query still loading, a failure or a blank
      // result can never turn into an accidental send. Escape closes the menu
      // and gives Enter back to the composer.
      if (mentionOpen) {
        if (e.key === "ArrowDown") {
          e.preventDefault();
          setMentionIndex((i) => Math.min(i + 1, Math.max(0, mentionMatches.length - 1)));
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          setMentionIndex((i) => Math.max(0, i - 1));
          return;
        }
        if (e.key === "Enter") {
          e.preventDefault();
          const file = mentionMatches[Math.min(mentionIndex, mentionMatches.length - 1)];
          if (file) pickMention(file);
          return;
        }
        if (e.key === "Tab" && mentionMatches.length > 0) {
          e.preventDefault();
          const file = mentionMatches[Math.min(mentionIndex, mentionMatches.length - 1)];
          if (file) pickMention(file);
          return;
        }
      }
      if (cmdOpen) {
        const filtered = filterCommands(commands, input);
        if (e.key === "ArrowDown") {
          e.preventDefault();
          setCmdIndex((i) => moveCommandCursor(filtered, i, +1));
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          setCmdIndex((i) => moveCommandCursor(filtered, i, -1));
          return;
        }
        if (e.key === "Enter" && filtered.length > 0) {
          // Same rule as the file menu while it is actually showing something.
          e.preventDefault();
          const pick = filtered[clampCommandIndex(filtered, cmdIndex)];
          if (pick) pickCommand(pick);
          return;
        }
      }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  const closeCmdMenu = useCallback(() => setCmdOpen(false), []);
  const retryCommands = useCallback(() => setCommandRetry((n) => n + 1), []);
  const dismissMention = useCallback(() => setMentionOff(true), []);
  const retryMention = useCallback(() => setMentionRetry((n) => n + 1), []);

  return {
    input,
    caret,
    setInput,
    setCaret,
    editingDraft,
    composing,
    handleChange,
    handleComposingChange,
    syncComposer,
    submit,
    onKeyDown,
    cmdOpen,
    cmdIndex,
    commandState,
    commands,
    pickCommand,
    closeCmdMenu,
    retryCommands,
    mentionOpen,
    mentionCurrent,
    mentionMatches,
    mentionIndex,
    mentionInfo,
    pickMention,
    dismissMention,
    retryMention,
    toggleVoice,
    voice,
  };
}
