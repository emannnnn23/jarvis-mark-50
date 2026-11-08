/**
 * The command bar, docked at the bottom centre: type a message or a slash command for JARVIS, as if at
 * the terminal prompt. It runs through the same loop, policy and tools (mission_control/bridge.py).
 *
 * - One line only: Enter sends command.submit with a fresh crypto.randomUUID(); a multi-line paste is
 *   refused with a hint instead of being flattened, because the server would refuse it anyway.
 * - The chip follows this page's latest command: queued, running, done, or why it was refused.
 * - Cancel works like Ctrl+C while JARVIS is working or waiting for an approval.
 * - Up and Down walk through what this page sent (kept in memory only).
 * - Above the bar, the latest reply (assistant.message, or a slash command's output) as plain text in a
 *   height-capped, scrollable area. The reply and any notice are hidden while an approval is open: the
 *   approval takes that space.
 */

import { useEffect, useRef, useState } from "react";
import type { ChangeEvent, ClipboardEvent, KeyboardEvent, Ref } from "react";

import { browseHistory, cancelAvailable, checkLine, commandChip, pastedProblem, rememberLine } from "../../store/commands";
import type { Chip } from "../../store/commands";
import { displayState } from "../../store/reducer";
import type { LatestReply } from "../../store/reducer";
import { useMission } from "../../store/store";
import type { ClientMessage } from "../../types/events";

export interface CommandBarViewProps {
  value: string;
  connected: boolean;
  chip: Chip | null;
  /** A message from the page itself (a refused paste, a line too long, no link); beats the chip's. */
  notice: string | null;
  reply: LatestReply | null;
  replyHidden: boolean;
  /** An approval is open over this space: show nothing above the bar. */
  aboveHidden?: boolean;
  cancelEnabled: boolean;
  onChange: (event: ChangeEvent<HTMLInputElement>) => void;
  onKeyDown: (event: KeyboardEvent<HTMLInputElement>) => void;
  onPaste: (event: ClipboardEvent<HTMLInputElement>) => void;
  onSubmit: () => void;
  onCancel: () => void;
  onToggleReply: () => void;
  inputRef?: Ref<HTMLInputElement>;
}

export function CommandBarView(props: CommandBarViewProps) {
  const { value, connected, chip, notice, reply, replyHidden, cancelEnabled } = props;
  const message = notice ?? chip?.message ?? null;
  return (
    <div className="command-bar">
      <div className="command-above" hidden={props.aboveHidden === true}>
        {reply !== null && (
          <section className="command-reply" aria-label="Latest reply from JARVIS" data-hidden={replyHidden}>
            <header className="command-reply-head">
              <span className={reply.kind === "message" ? "command-reply-from" : "command-reply-command"}>
                {reply.kind === "message" ? "JARVIS" : reply.command}
              </span>
              <button type="button" className="icon-button command-reply-toggle" onClick={props.onToggleReply}
                aria-expanded={!replyHidden}>
                {replyHidden ? "Show reply" : "Hide"}
              </button>
            </header>
            {!replyHidden && <pre className="command-reply-body" tabIndex={0}>{reply.text}</pre>}
          </section>
        )}
        {message !== null && (
          <p className={`command-notice${notice !== null || chip?.tone === "attention" ? " command-notice-attention" : ""}`}
            role="status">
            {message}
          </p>
        )}
      </div>
      <div className={`command-row${connected ? "" : " command-row-offline"}`}>
        <span className="command-prompt" aria-hidden="true">›</span>
        <input
          ref={props.inputRef}
          className="command-input"
          type="text"
          value={value}
          onChange={props.onChange}
          onKeyDown={props.onKeyDown}
          onPaste={props.onPaste}
          placeholder={connected ? "Ask JARVIS, or type a command like /status" : "Waiting for the link to JARVIS"}
          aria-label="Message or command for JARVIS"
          autoComplete="off"
          spellCheck={false}
          enterKeyHint="send"
        />
        {chip !== null && <span className={`command-chip command-chip-${chip.tone}`}>{chip.label}</span>}
        <button type="button" className="button command-send" disabled={!connected || value.trim() === ""}
          onClick={props.onSubmit}>
          Send
        </button>
        <button type="button" className="button command-cancel" disabled={!cancelEnabled} onClick={props.onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}

const NOT_SENT = "Not connected to JARVIS: nothing was sent";

export function CommandBar({ send }: { send: (message: ClientMessage) => boolean }) {
  const dispatch = useMission((state) => state.dispatch);
  const connected = useMission((state) => state.connection === "open");
  const shown = useMission(displayState);
  const confirmationOpen = useMission((state) => state.pendingConfirmation !== null);
  const latest = useMission((state) => state.commands[state.commands.length - 1] ?? null);
  const reply = useMission((state) => state.latestReply);

  const [value, setValue] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [replyHidden, setReplyHidden] = useState(false);
  const history = useRef<string[]>([]);
  const browsing = useRef<{ index: number | null; draft: string }>({ index: null, draft: "" });

  // A new reply is shown even if the person hid the last one.
  const replySeq = reply?.seq ?? null;
  useEffect(() => setReplyHidden(false), [replySeq]);

  const submit = () => {
    const line = checkLine(value);
    if (!line.ok) {
      setNotice(line.message);
      return;
    }
    const id = crypto.randomUUID();
    if (!send({ type: "command.submit", id, text: line.text })) {
      setNotice(NOT_SENT);
      return;
    }
    dispatch({ type: "command.sent", id, text: line.text, at: Date.now() });
    history.current = rememberLine(history.current, line.text);
    browsing.current = { index: null, draft: "" };
    setNotice(null);
    setValue("");
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing) return;
    if (event.key === "Enter") {
      event.preventDefault();
      submit();
    } else if (event.key === "ArrowUp" || event.key === "ArrowDown") {
      event.preventDefault();
      const { index, draft } = browsing.current;
      const step = browseHistory(history.current, index, event.key === "ArrowUp" ? "up" : "down",
        index === null ? value : draft);
      browsing.current = { index: step.index, draft: index === null ? value : draft };
      setValue(step.text);
    }
  };

  const onPaste = (event: ClipboardEvent<HTMLInputElement>) => {
    const problem = pastedProblem(event.clipboardData.getData("text"));
    if (problem !== null) {
      event.preventDefault();
      setNotice(problem);
    }
  };

  const onChange = (event: ChangeEvent<HTMLInputElement>) => {
    setValue(event.target.value);
    browsing.current = { index: null, draft: "" };
    if (notice !== null) setNotice(null);
  };

  return (
    <CommandBarView
      value={value}
      connected={connected}
      chip={latest === null ? null : commandChip(latest)}
      notice={notice}
      reply={reply}
      replyHidden={replyHidden}
      aboveHidden={confirmationOpen}
      cancelEnabled={cancelAvailable(shown, confirmationOpen, connected)}
      onChange={onChange}
      onKeyDown={onKeyDown}
      onPaste={onPaste}
      onSubmit={submit}
      onCancel={() => send({ type: "request.cancel" })}
      onToggleReply={() => setReplyHidden((hidden) => !hidden)}
    />
  );
}
