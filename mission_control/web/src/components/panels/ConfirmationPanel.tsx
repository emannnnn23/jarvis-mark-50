/**
 * The approval JARVIS is waiting for, with the same content the terminal prints, and Approve / Decline.
 *
 * Safety rules, each one deliberate:
 * - Everything shown is React text; the review is ReviewLines, exact to the character.
 * - The answer carries the payload's digest, so the broker approves only what was shown here.
 * - When the backend says the browser may not approve (a secret was redacted, or something was cut
 *   short), only Decline is offered, with a banner sending the person to the terminal.
 * - Approve needs a deliberate act: it is never focused for you (the panel itself takes focus when it
 *   opens, so an Enter meant for the command bar lands on nothing), it is not in a form, it stays off
 *   for a moment after the panel opens so a click aimed at something else cannot land on it, and
 *   Escape never approves.
 * - After an answer is sent both buttons are off until the server's result or confirm.resolved arrives.
 * - The click handler checks answerGate again with the latest state before sending, so the disabled
 *   attribute is never the only guard, and the frame is built by buildAnswer (id, digest, strict boolean).
 * The panel closes when the reducer sees confirm.resolved for its id.
 */

import { useEffect, useRef, useState } from "react";
import type { Ref } from "react";

import { answerGate, answerResultMessage, buildAnswer, countdownText } from "../../store/commands";
import type { ConfirmAnswer } from "../../store/reducer";
import { useMission } from "../../store/store";
import type { ClientMessage, ConfirmPayload } from "../../types/events";
import { ReviewLines } from "./ReviewLines";

export const SECRET_BANNER =
  "This looks like it contains a secret or is too long to show in full: answer in the terminal";
/** How long Approve stays off after the panel opens. */
export const ARM_DELAY_MS = 600;

export interface ConfirmationPanelViewProps {
  payload: ConfirmPayload;
  answer: ConfirmAnswer | null;
  /** Epoch ms, for the countdown. */
  now: number;
  /** False for the first moments after the panel opens: Approve is off. */
  armed: boolean;
  connected: boolean;
  onAnswer: (approved: boolean) => void;
  panelRef?: Ref<HTMLElement>;
}

function statusLine(answer: ConfirmAnswer | null, expired: boolean, connected: boolean): { text: string; tone: string } | null {
  if (answer?.phase === "sending") return { text: "Sending your answer…", tone: "quiet" };
  if (answer?.phase === "answered" && answer.result !== null) {
    return { text: answerResultMessage(answer.result, answer.approved), tone: answer.result === "ok" ? "quiet" : "attention" };
  }
  if (expired) return { text: answerResultMessage("expired", false), tone: "attention" };
  if (!connected) return { text: "Not connected to JARVIS: answer in the terminal, or wait for the link", tone: "attention" };
  return null;
}

export function ConfirmationPanelView({ payload, answer, now, armed, connected, onAnswer, panelRef }: ConfirmationPanelViewProps) {
  const countdown = payload.expires_at === null ? null : countdownText(payload.expires_at, now);
  const expired = payload.expires_at !== null && countdown === null;
  // The same gate the click handler checks again before anything is sent.
  const gate = answerGate({ payload, answer, now, armed, connected });
  const declineOff = !gate.decline;
  const approveOff = !gate.approve;
  const status = statusLine(answer, expired, connected);
  return (
    <section
      ref={panelRef}
      className="panel confirm-panel"
      role="alertdialog"
      aria-labelledby="confirm-title"
      aria-describedby="confirm-operation"
      tabIndex={-1}
    >
      <header className="confirm-head">
        <h2 id="confirm-title">Confirmation required</h2>
        <span className="confirm-label">{`${payload.label}: ${payload.tool}`}</span>
        {countdown !== null && <span className="confirm-countdown">{`Expires in ${countdown}`}</span>}
      </header>
      {!payload.browser_approvable && <p className="confirm-banner" role="note">{SECRET_BANNER}</p>}
      <dl className="confirm-facts">
        <div className="confirm-fact">
          <dt>Operation</dt>
          <dd id="confirm-operation">{payload.operation}</dd>
        </div>
        {payload.affected_paths.map((path, index) => (
          <div key={index} className="confirm-fact">
            <dt>Affects</dt>
            <dd>{path}</dd>
          </div>
        ))}
        {payload.reason !== "" && (
          <div className="confirm-fact">
            <dt>Why asked</dt>
            <dd>{payload.reason}</dd>
          </div>
        )}
      </dl>
      {payload.hidden > 0 && (
        <p className="confirm-hidden">{`!! ${payload.hidden} invisible or text-direction character(s), shown as \\u{...}`}</p>
      )}
      {payload.lines.length > 0 && <ReviewLines lines={payload.lines} />}
      {payload.omitted > 0 && <p className="confirm-omitted">{`... ${payload.omitted} more lines not shown`}</p>}
      <footer className="confirm-actions">
        <p className={`confirm-status confirm-status-${status?.tone ?? "quiet"}`} role="status" aria-live="polite">
          {status?.text ?? ""}
        </p>
        <button type="button" className="button confirm-decline" disabled={declineOff} onClick={() => onAnswer(false)}>
          Decline
        </button>
        {payload.browser_approvable && (
          <button
            type="button"
            className={`button confirm-approve${armed ? "" : " confirm-arming"}`}
            disabled={approveOff}
            onClick={() => onAnswer(true)}
          >
            Approve
          </button>
        )}
      </footer>
    </section>
  );
}

/** Re-render every second while a countdown is shown. */
function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return undefined;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [active]);
  return now;
}

/** The open confirmation from the store, wired to the socket. Renders nothing when none is open. */
export function ConfirmationPanel({ send }: { send: (message: ClientMessage) => boolean }) {
  const payload = useMission((state) => state.pendingConfirmation);
  const answer = useMission((state) => state.confirmAnswer);
  const connected = useMission((state) => state.connection === "open");
  const dispatch = useMission((state) => state.dispatch);
  const panel = useRef<HTMLElement | null>(null);
  const [armedId, setArmedId] = useState<string | null>(null);
  const now = useNow(payload !== null && payload.expires_at !== null);
  const id = payload?.id ?? null;

  // A new request: take focus (never on Approve), and arm Approve after a moment. Focus goes back to
  // where it was when the panel closes, if that element is still on the page.
  useEffect(() => {
    if (id === null) return undefined;
    const before = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    panel.current?.focus();
    const timer = setTimeout(() => setArmedId(id), ARM_DELAY_MS);
    return () => {
      clearTimeout(timer);
      // Only when closing the panel dropped focus to the page; never pull it from where the person moved it.
      const lost = document.activeElement === null || document.activeElement === document.body;
      if (before !== null && before.isConnected && lost) before.focus();
    };
  }, [id]);

  if (payload === null) return null;
  const armed = armedId === payload.id;
  const onAnswer = (approved: boolean) => {
    // Defence in depth: re-check against the store as it is now, not as it was at the last render.
    const current = useMission.getState();
    if (current.pendingConfirmation?.id !== payload.id) return;
    const gate = answerGate({
      payload, answer: current.confirmAnswer, now: Date.now(), armed, connected: current.connection === "open",
    });
    if (!(approved === true ? gate.approve : gate.decline)) return;
    const message = buildAnswer(payload, approved);
    if (send(message)) dispatch({ type: "confirm.sent", id: payload.id, approved: message.approved });
  };
  return (
    <ConfirmationPanelView
      payload={payload}
      answer={answer}
      now={now}
      armed={armed}
      connected={connected}
      onAnswer={onAnswer}
      panelRef={panel}
    />
  );
}
