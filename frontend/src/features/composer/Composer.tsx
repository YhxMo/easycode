import type { ReactNode, RefObject } from "react";
import { COMMAND_LIST_ID, CommandMenu } from "./CommandMenu";
import { ComposerBar } from "./ComposerBar";
import { MENTION_LIST_ID, MENTION_OPTION_PREFIX, MentionMenu } from "./MentionMenu";
import type { useComposer } from "./useComposer";

interface Props {
  /** `tall` is the centred first-run card; `docked` floats over the message stream. */
  variant: "tall" | "docked";
  /** The composer state for the draft on screen. */
  composer: ReturnType<typeof useComposer>;
  fieldRef: RefObject<HTMLTextAreaElement | null>;
  busy: boolean;
  sendBlocked: boolean;
  permission: string;
  onPermission: (mode: string) => void;
  onCancelEdit: () => void;
  onStop: () => void;
  /** The model picker, kept beside the field it applies to. */
  model: ReactNode;
}

/**
 * The input field with its menus, wired to the composer's state.
 *
 * One composer, two placements: the centred first-run card, or docked over the
 * message stream. Only one of them is mounted at a time, and both read the same
 * state, so what was typed survives the stage changing under it.
 */
export function Composer({
  variant,
  composer,
  fieldRef,
  busy,
  sendBlocked,
  permission,
  onPermission,
  onCancelEdit,
  onStop,
  model,
}: Props) {
  const { mentionOpen, mentionMatches, mentionIndex } = composer;
  return (
    <ComposerBar
      variant={variant}
      value={composer.input}
      placeholder="向 Easy code 提问，使用 / 运行命令…"
      ariaLabel="给 Easy code 发送消息"
      busy={busy}
      sendBlocked={sendBlocked}
      permission={permission}
      permissionDisabled={busy || sendBlocked}
      onPermission={onPermission}
      onChange={composer.handleChange}
      onKeyDown={composer.onKeyDown}
      onSelectionChange={composer.setCaret}
      onComposingChange={composer.handleComposingChange}
      onCompositionEnd={(v, nextCaret) => composer.syncComposer(v, nextCaret)}
      fieldRef={fieldRef}
      menuOpen={mentionOpen || composer.cmdOpen}
      menuId={mentionOpen ? MENTION_LIST_ID : COMMAND_LIST_ID}
      activeOptionId={
        mentionOpen && mentionMatches.length
          ? `${MENTION_OPTION_PREFIX}${Math.min(mentionIndex, mentionMatches.length - 1)}`
          : undefined
      }
      onSend={composer.submit}
      editing={
        composer.editingDraft
          ? { text: composer.editingDraft.text, onCancel: onCancelEdit }
          : undefined
      }
      onStop={onStop}
      hint="Enter 发送 · Shift + Enter 换行"
      model={model}
      voice={composer.voice}
      menu={
        mentionOpen ? (
          <MentionMenu
            state={composer.mentionCurrent}
            query={composer.mentionInfo?.query ?? ""}
            index={mentionIndex}
            onPick={composer.pickMention}
            onClose={composer.dismissMention}
            onRetry={composer.retryMention}
          />
        ) : (
          <CommandMenu
            state={composer.commandState}
            open={composer.cmdOpen}
            query={composer.input}
            index={composer.cmdIndex}
            onPick={composer.pickCommand}
            onRetry={composer.retryCommands}
            onClose={composer.closeCmdMenu}
          />
        )
      }
    />
  );
}
