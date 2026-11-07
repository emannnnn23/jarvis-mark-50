/**
 * The machine JARVIS runs on, from real psutil samples (every 2 s while this page is connected).
 * When psutil is missing the panel says so instead of showing zeros; while the link is down it keeps the
 * last reading and says how old it is.
 */

import { useEffect, useState } from "react";

import { useMission } from "../../store/store";
import type { SystemMetricsEvent } from "../../types/events";
import { formatBytes, formatClock, formatDuration, formatPercent } from "./format";

const HISTORY = 60; // two minutes of 2 s samples
const HIGH = 90; // percent at which a meter turns amber

function Meter({ label, percent, detail }: { label: string; percent: number; detail?: string }) {
  const clamped = Math.max(0, Math.min(100, percent));
  return (
    <div className="metric">
      <div className="metric-row">
        <span className="metric-label">{label}</span>
        <span className="metric-value">{formatPercent(percent)}</span>
      </div>
      <div className={`meter${clamped >= HIGH ? " meter-high" : ""}`} role="meter" aria-label={label}
        aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(clamped)}>
        <div className="meter-fill" style={{ width: `${clamped}%` }} />
      </div>
      {detail !== undefined && <div className="metric-detail">{detail}</div>}
    </div>
  );
}

function CpuTrace({ samples }: { samples: number[] }) {
  if (samples.length < 2) return null;
  const width = 200;
  const height = 30;
  const points = samples
    .map((value, index) => {
      const x = width - (samples.length - 1 - index) * (width / (HISTORY - 1));
      const y = height - (Math.max(0, Math.min(100, value)) / 100) * (height - 2) - 1;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const low = Math.round(Math.min(...samples));
  const high = Math.round(Math.max(...samples));
  return (
    <svg className="cpu-trace" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img"
      aria-label={`CPU over the last ${Math.round((samples.length * 2) / 60 * 10) / 10} minutes, ${low}% to ${high}%`}>
      <title>{`CPU, last ${samples.length * 2} s: ${low}% to ${high}%`}</title>
      <line className="cpu-trace-mid" x1="0" x2={width} y1={height / 2} y2={height / 2} />
      <polyline className="cpu-trace-line" points={points} />
    </svg>
  );
}

function useCpuHistory(metrics: SystemMetricsEvent | null): number[] {
  const [history, setHistory] = useState<{ seq: number; values: number[] }>({ seq: 0, values: [] });
  useEffect(() => {
    if (metrics === null) return;
    setHistory((current) =>
      metrics.seq === current.seq ? current
        : { seq: metrics.seq, values: [...current.values, metrics.cpu_percent].slice(-HISTORY) });
  }, [metrics]);
  return history.values;
}

export function SystemMonitorPanel() {
  const metrics = useMission((state) => state.metrics);
  const system = useMission((state) => state.subsystems.system);
  const live = useMission((state) => state.connection === "open");
  const cpu = useCpuHistory(metrics);

  let body;
  if (system.status === "unavailable") {
    body = (
      <p className="panel-note">
        System metrics are unavailable{typeof system.reason === "string" ? `: ${system.reason}.` : "."}
      </p>
    );
  } else if (metrics === null) {
    body = <p className="panel-note">Waiting for the first reading.</p>;
  } else {
    const load = metrics.load_avg;
    body = (
      <>
        <Meter label="CPU" percent={metrics.cpu_percent} />
        <CpuTrace samples={cpu} />
        <Meter label="Memory" percent={metrics.memory_percent}
          detail={`${formatBytes(metrics.memory_used)} of ${formatBytes(metrics.memory_total)}`} />
        <Meter label="Disk" percent={metrics.disk_percent}
          detail={`${formatBytes(metrics.disk_used)} of ${formatBytes(metrics.disk_total)}`} />
        <dl className="facts">
          {load !== null && load.length === 3 && (
            <>
              <dt>Load</dt>
              <dd>{load.map((value) => value.toFixed(2)).join("  ")}</dd>
            </>
          )}
          <dt>Machine up</dt>
          <dd>{formatDuration(metrics.uptime_s)}</dd>
          <dt>JARVIS up</dt>
          <dd>{formatDuration(metrics.process_uptime_s)}</dd>
        </dl>
      </>
    );
  }

  return (
    <section className={`panel system-panel${live ? "" : " panel-stale"}`} aria-labelledby="system-panel-title">
      <header className="panel-head">
        <h2 id="system-panel-title">System</h2>
        {metrics !== null && <span className="panel-meta">{metrics.os}</span>}
      </header>
      {body}
      {!live && metrics !== null && <p className="panel-note">Last reading at {formatClock(metrics.timestamp)}.</p>}
    </section>
  );
}
