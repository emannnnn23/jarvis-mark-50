/**
 * The task JARVIS is working through: its goal, the plan's steps with their status, why it is paused,
 * and the latest progress line. A plan written after JARVIS read web or file content is flagged, in the
 * same words the terminal uses, because its steps may carry someone else's instructions.
 * Hidden when there is no task.
 */

import { useMission } from "../../store/store";

const TASK_STATUS_WORD: Record<string, string> = {
  active: "Active",
  paused: "Paused",
  done: "Done",
  abandoned: "Abandoned",
};

const STEP_STATUS_WORD: Record<string, string> = {
  pending: "Pending",
  in_progress: "In progress",
  done: "Done",
  skipped: "Skipped",
  failed: "Failed",
};

export function TaskPanel() {
  const task = useMission((state) => state.task);
  const progress = useMission((state) => state.taskProgress);
  if (task === null) return null;
  const finished = task.steps.filter((step) => step.status === "done" || step.status === "skipped").length;
  return (
    <section className={`panel task-panel task-${task.status}`} aria-labelledby="task-title">
      <header className="panel-head">
        <h2 id="task-title">Task</h2>
        <span className="panel-meta">
          {TASK_STATUS_WORD[task.status] ?? task.status}
          {task.steps.length > 0 ? `, ${finished} of ${task.steps.length} steps` : ""}
        </span>
      </header>
      <p className="task-goal">{task.goal}</p>
      {task.untrusted_plan && (
        <p className="task-warning" role="note">
          This plan was written after reading web or file content. Check it before continuing.
        </p>
      )}
      {task.status === "paused" && task.paused_reason && <p className="panel-note">Paused: {task.paused_reason}</p>}
      {task.steps.length > 0 && (
        <ol className="task-steps">
          {task.steps.map((step) => (
            <li key={step.n} className={`task-step step-${step.status}`}>
              <span className="step-mark" aria-hidden="true" />
              <span className="step-text">{step.text}</span>
              <span className="visually-hidden">{`, ${STEP_STATUS_WORD[step.status] ?? step.status}`}</span>
            </li>
          ))}
        </ol>
      )}
      {progress !== null && <p className="task-progress">{progress.message}</p>}
    </section>
  );
}
