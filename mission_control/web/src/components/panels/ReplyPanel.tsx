/**
 * The whole latest reply (an assistant.message, or a browser slash command's output such as a /resume
 * run), opened from the command bar's one-line preview. It lives at the top of the right column, which
 * the camera already keeps clear of the node ring, so reading a long reply never hides a node.
 * Plain text in a height-capped, scrollable <pre>; never HTML.
 */

import { useMission } from "../../store/store";
import type { LatestReply } from "../../store/reducer";
import { useUi } from "../../store/ui";

export function ReplyPanelView({ reply, onClose }: { reply: LatestReply; onClose: () => void }) {
  return (
    <section className="panel reply-panel" aria-labelledby="reply-title">
      <header className="panel-head">
        <h2 id="reply-title">{reply.kind === "message" ? "JARVIS replied" : "Command output"}</h2>
        {reply.kind === "output" && <span className="panel-meta">{reply.command}</span>}
        <button type="button" className="button button-small" onClick={onClose}>Close</button>
      </header>
      <pre className="command-reply-body" tabIndex={0}>{reply.text}</pre>
    </section>
  );
}

export function ReplyPanel() {
  const open = useUi((ui) => ui.replyOpen);
  const close = useUi((ui) => ui.closeReply);
  const reply = useMission((state) => state.latestReply);
  if (!open || reply === null) return null;
  return <ReplyPanelView reply={reply} onClose={close} />;
}
