/**
 * The live event stream: what JARVIS did, newest at the bottom, one line each. It follows the newest
 * row until the person scrolls up to read; then it stays put and offers "Jump to latest". Pause freezes
 * the list and counts what arrives meanwhile; Clear empties it. A row expands to show the full event as
 * plain text (React text nodes inside <pre>, never HTML).
 */

import { memo, useCallback, useLayoutEffect, useRef, useState } from "react";

import { STREAM_CATEGORIES, formatHms, streamRows } from "../../store/eventStream";
import type { EventRow, StreamFilter } from "../../store/eventStream";
import { useMission } from "../../store/store";
import { useUi } from "../../store/ui";

/** Within this many px of the bottom counts as "at the newest row". */
const STICK_PX = 24;

const Row = memo(function Row({ row, open, onToggle }: { row: EventRow; open: boolean; onToggle: (seq: number) => void }) {
  const { event } = row;
  const detailId = `event-${event.seq}-detail`;
  return (
    <li className={`event-row event-${row.tone}`}>
      <button type="button" className="event-line" aria-expanded={open} aria-controls={detailId}
        onClick={() => onToggle(event.seq)}>
        <time className="event-time" dateTime={new Date(event.timestamp).toISOString()}>{formatHms(event.timestamp)}</time>
        <span className={`chip chip-${row.category}`}>{row.category}</span>
        <span className="event-status">{row.status}</span>
      </button>
      {open && <pre id={detailId} className="event-detail">{row.details}</pre>}
    </li>
  );
});

export function EventStreamPanel() {
  const log = useMission((state) => state.log);
  const view = useUi((ui) => ui.stream);
  const pause = useUi((ui) => ui.pauseStream);
  const resume = useUi((ui) => ui.resumeStream);
  const clear = useUi((ui) => ui.clearStream);
  const setFilter = useUi((ui) => ui.filterStream);
  const { rows, buffered } = streamRows(log, view);
  const [expanded, setExpanded] = useState<ReadonlySet<number>>(() => new Set());
  const [following, setFollowing] = useState(true);
  const list = useRef<HTMLOListElement>(null);

  const toggle = useCallback((seq: number) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (!next.delete(seq)) next.add(seq);
      return next;
    });
  }, []);

  const newest = rows.length ? rows[rows.length - 1]!.event.seq : 0;
  useLayoutEffect(() => {
    const element = list.current;
    if (element !== null && following) element.scrollTop = element.scrollHeight;
  }, [newest, rows.length, following]);

  const onScroll = () => {
    const element = list.current;
    if (element === null) return;
    const atBottom = element.scrollHeight - element.scrollTop - element.clientHeight <= STICK_PX;
    if (atBottom !== following) setFollowing(atBottom);
  };

  const jumpToLatest = () => {
    if (view.paused) resume();
    setFollowing(true);
  };

  return (
    <section className="panel stream-panel" aria-labelledby="stream-title">
      <header className="panel-head stream-head">
        <h2 id="stream-title">Events</h2>
        <div className="stream-controls">
          <label className="visually-hidden" htmlFor="stream-filter">Show</label>
          <select id="stream-filter" className="select" value={view.filter}
            onChange={(event) => setFilter(event.target.value as StreamFilter)}>
            <option value="all">All</option>
            {STREAM_CATEGORIES.map((category) => <option key={category} value={category}>{category}</option>)}
          </select>
          <button type="button" className="button button-small" aria-pressed={view.paused}
            onClick={view.paused ? resume : pause}>
            {view.paused ? "Resume" : "Pause"}
          </button>
          <button type="button" className="button button-small" onClick={clear} disabled={rows.length === 0 && buffered === 0}>
            Clear
          </button>
        </div>
      </header>
      {view.paused && (
        <p className="stream-paused" role="status">
          Paused{buffered > 0 ? `, ${buffered} new ${buffered === 1 ? "event" : "events"} waiting` : ""}
        </p>
      )}
      <ol ref={list} className="event-list" onScroll={onScroll} aria-live="off" aria-label="Event stream">
        {rows.map((row) => (
          <Row key={row.event.seq} row={row} open={expanded.has(row.event.seq)} onToggle={toggle} />
        ))}
      </ol>
      {rows.length === 0 && (
        <p className="panel-note stream-empty">
          {view.filter === "all" ? "Events appear here as JARVIS works." : `No ${view.filter} events yet.`}
        </p>
      )}
      {(!following || (view.paused && buffered > 0)) && (
        <button type="button" className="button button-small jump-latest" onClick={jumpToLatest}>
          Jump to latest
        </button>
      )}
    </section>
  );
}
