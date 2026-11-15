/**
 * What one subsystem is doing, opened by selecting its node. Every value comes from a real event or the
 * backend snapshot; a fact JARVIS has not reported yet reads "Not reported yet" rather than a guess.
 * All backend text is rendered as React text nodes.
 */

import { useEffect, useState } from "react";
import type { ReactNode } from "react";

import { formatHms, formatMs } from "../../store/eventStream";
import { displayState, toolEntries } from "../../store/reducer";
import { useMission } from "../../store/store";
import { useUi } from "../../store/ui";
import type { SubsystemId, SubsystemView } from "../../types/events";
import { formatBytes, formatDuration, formatPercent } from "../panels/format";
import { NODE_LAYOUT, STATUS_LOOK, VOICE_PHASE } from "./layout";

const NOT_YET = "Not reported yet";
const LEVEL_FRESH_MS = 1500;

const str = (value: unknown): string | null => (typeof value === "string" && value ? value : null);
const num = (value: unknown): number | null => (typeof value === "number" && Number.isFinite(value) ? value : null);

const TOOL_STATUS_WORD: Record<string, string> = {
  requested: "requested",
  awaiting_confirmation: "waiting for approval",
  running: "running",
  success: "succeeded",
  error: "failed",
  denied: "denied",
  partial: "partly succeeded",
  unverified: "unverified",
};

const LLM_RESULT_WORD: Record<string, string> = { success: "Answered", error: "Failed", retry: "Retrying" };

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </>
  );
}

function LlmDetails({ view }: { view: SubsystemView }) {
  const session = useMission((state) => state.session);
  const duration = num(view.last_duration_ms);
  const lastStatus = str(view.last_status);
  const attempt = num(view.attempt);
  return (
    <dl className="facts facts-detail">
      <Fact label="Provider">{session?.provider === "groq" ? "Groq" : session?.provider ?? NOT_YET}</Fact>
      <Fact label="Model">{str(view.model) ?? session?.model ?? NOT_YET}</Fact>
      <Fact label="Now">{view.in_flight === true ? `Calling the model${attempt !== null && attempt > 1 ? `, attempt ${attempt}` : ""}` : "Waiting"}</Fact>
      <Fact label="Last call">{duration === null ? NOT_YET : formatMs(duration)}</Fact>
      <Fact label="Last result">{lastStatus === null ? NOT_YET : LLM_RESULT_WORD[lastStatus] ?? lastStatus}</Fact>
      {str(view.last_retry_reason) !== null && lastStatus === "retry" && <Fact label="Retry reason">{str(view.last_retry_reason)}</Fact>}
      {str(view.last_error_kind) !== null && <Fact label="Last error">{str(view.last_error_message) ?? str(view.last_error_kind)}</Fact>}
    </dl>
  );
}

function MemoryDetails({ view }: { view: SubsystemView }) {
  const memoryStatus = useMission((state) => state.session?.memory_status ?? null);
  const recalled = num(view.recall_count);
  const facts = num(view.facts);
  const op = str(view.last_op);
  return (
    <dl className="facts facts-detail">
      <Fact label="Last activity">{op === null ? NOT_YET : op === "store" ? "Saved" : op === "search" ? "Searched" : "Recalled"}</Fact>
      <Fact label="Recalled">{recalled === null ? NOT_YET : `${recalled} ${recalled === 1 ? "memory" : "memories"} last time`}</Fact>
      <Fact label="Facts stored">{facts === null ? NOT_YET : String(facts)}</Fact>
      {memoryStatus !== null && memoryStatus !== "ok" && (
        <Fact label="Store">{memoryStatus === "corrupted" ? "Damaged file, memory is read-only" : "Disabled"}</Fact>
      )}
    </dl>
  );
}

function ToolsDetails({ view }: { view: SubsystemView }) {
  const awaiting = useMission((state) => state.jarvis?.detail === "awaiting approval");
  const active = Array.isArray(view.active) ? (view.active as unknown[]).filter((name): name is string => typeof name === "string") : [];
  const activeStatus = (view.active_status ?? {}) as Record<string, string>;
  const recent = toolEntries(view.recent).slice().reverse();
  const current = active[active.length - 1];
  const currentStatus = current === undefined ? null : activeStatus[current] ?? (awaiting ? "awaiting_confirmation" : "running");
  return (
    <>
      <dl className="facts facts-detail">
        <Fact label="Active">{current === undefined ? "None" : current}</Fact>
        {currentStatus !== null && <Fact label="Status">{TOOL_STATUS_WORD[currentStatus] ?? currentStatus}</Fact>}
        {str(view.last_target) !== null && <Fact label="Target">{str(view.last_target)}</Fact>}
      </dl>
      <h3 className="detail-subhead">Recent</h3>
      {recent.length === 0 ? (
        <p className="panel-note">No tool has finished yet.</p>
      ) : (
        <ul className="tool-list">
          {recent.map((entry, index) => (
            <li key={entry.seq ?? `snapshot-${index}`} className={`tool-item tool-${entry.status}`}>
              <span className="tool-name">{entry.tool}</span>
              <span className="tool-result">
                {TOOL_STATUS_WORD[entry.status] ?? entry.status}
                {entry.duration_ms !== undefined ? `, ${formatMs(entry.duration_ms)}` : ""}
              </span>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

function SystemDetails({ view }: { view: SubsystemView }) {
  const metrics = useMission((state) => state.metrics);
  if (view.status === "unavailable") {
    return <p className="panel-note">System metrics are unavailable{str(view.reason) ? `: ${str(view.reason)}.` : "."}</p>;
  }
  if (metrics === null) return <p className="panel-note">Waiting for the first reading.</p>;
  return (
    <dl className="facts facts-detail">
      <Fact label="Operating system">{metrics.os}</Fact>
      <Fact label="CPU">{formatPercent(metrics.cpu_percent)}</Fact>
      <Fact label="Memory">{`${formatPercent(metrics.memory_percent)}, ${formatBytes(metrics.memory_used)} of ${formatBytes(metrics.memory_total)}`}</Fact>
      <Fact label="Disk">{`${formatPercent(metrics.disk_percent)}, ${formatBytes(metrics.disk_used)} of ${formatBytes(metrics.disk_total)}`}</Fact>
      {metrics.load_avg !== null && metrics.load_avg.length === 3 && (
        <Fact label="Load">{metrics.load_avg.map((value) => value.toFixed(2)).join("  ")}</Fact>
      )}
      <Fact label="Machine up">{formatDuration(metrics.uptime_s)}</Fact>
      <Fact label="JARVIS up">{formatDuration(metrics.process_uptime_s)}</Fact>
    </dl>
  );
}

/** The live microphone level, refreshed while voice.level arrives; empty when the mic is quiet. */
function MicLevel() {
  const level = useMission((state) => state.voiceLevel);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (level === null) return undefined;
    const timer = setTimeout(() => setNow(Date.now()), LEVEL_FRESH_MS);
    setNow(Date.now());
    return () => clearTimeout(timer);
  }, [level]);
  const fresh = level !== null && now - level.at < LEVEL_FRESH_MS;
  const percent = fresh ? Math.round(Math.min(1, level.rms * 2.5) * 100) : 0;
  return (
    <div className="metric">
      <div className="metric-row">
        <span className="metric-label">Microphone level</span>
        <span className="metric-value">{fresh ? (level.phase === "collecting" ? "Hearing you" : "Listening for the wake word") : "No signal"}</span>
      </div>
      <div className="meter meter-live" role="meter" aria-label="Microphone level" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}>
        <div className="meter-fill" style={{ width: `${percent}%` }} />
      </div>
    </div>
  );
}

function VoiceDetails({ view }: { view: SubsystemView }) {
  const session = useMission((state) => state.session);
  const phase = str(view.input_phase);
  const mode = str(view.mode);
  const mic = session === null ? NOT_YET : session.mic_available ? "Available" : "Not found";
  return (
    <>
      <dl className="facts facts-detail">
        <Fact label="Microphone">{mic}</Fact>
        {session !== null && <Fact label="Wake word">{session.wake_available ? "Available" : "Not installed"}</Fact>}
        <Fact label="Input">{phase === null ? (mode === "off" ? "Off" : NOT_YET) : VOICE_PHASE[phase] ?? phase}</Fact>
        <Fact label="Speaking">{view.speaking === true ? "Yes" : "No"}</Fact>
        {session !== null && <Fact label="Spoken replies">{session.voice_enabled ? "On" : "Off"}</Fact>}
      </dl>
      {view.status !== "unavailable" && <MicLevel />}
    </>
  );
}

function NetworkDetails({ view }: { view: SubsystemView }) {
  const groq = str(view.groq);
  const latency = num(view.latency_ms);
  const checked = num(view.checked_at);
  const source = str(view.source);
  return (
    <dl className="facts facts-detail">
      <Fact label="Groq">{groq === "up" ? "Reachable" : groq === "down" ? "Unreachable" : groq === null ? NOT_YET : "Unknown"}</Fact>
      <Fact label="Latency">{latency === null ? NOT_YET : `${Math.round(latency)} ms`}</Fact>
      <Fact label="Checked at">{checked === null ? NOT_YET : formatHms(checked)}</Fact>
      {source !== null && <Fact label="Checked by">{source === "probe" ? "Connection probe" : "A model call"}</Fact>}
    </dl>
  );
}

const DETAILS: Record<SubsystemId, (props: { view: SubsystemView }) => ReactNode> = {
  llm: LlmDetails,
  memory: MemoryDetails,
  tools: ToolsDetails,
  system: SystemDetails,
  voice: VoiceDetails,
  network: NetworkDetails,
};

export function NodeDetailPanel() {
  const selected = useUi((ui) => ui.selected);
  const select = useUi((ui) => ui.select);
  const view = useMission((state) => (selected === null ? null : state.subsystems[selected]));
  const stale = useMission((state) => displayState(state) === "offline");
  if (selected === null || view === null) return null;
  const placement = NODE_LAYOUT.find((node) => node.id === selected)!;
  const status = stale ? "unknown" : view.status;
  const Body = DETAILS[selected];
  return (
    <section id="node-detail" className={`panel node-detail node-${status}${stale ? " panel-stale" : ""}`}
      aria-labelledby="node-detail-title">
      <header className="panel-head">
        <h2 id="node-detail-title" className="node-detail-name">{placement.label}</h2>
        <span className="node-detail-status">{stale ? "NO LINK" : STATUS_LOOK[status].word}</span>
        <button type="button" className="icon-button" onClick={() => select(null)} aria-label={`Close ${placement.label} details`}>
          <span aria-hidden="true">×</span>
        </button>
      </header>
      <Body view={view} />
    </section>
  );
}
