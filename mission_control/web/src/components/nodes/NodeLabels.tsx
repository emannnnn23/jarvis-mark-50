/**
 * The six node labels: name, status word and the node's real facts, placed by the scene's projector.
 * Each label is a button that opens the node's detail panel, so every node can be reached by keyboard
 * as well as by clicking its marker (Tab to it, Enter or Space to open). Keyboard focus lights the
 * node's marker the way hovering does. Without a live link the statuses are not known any more, so
 * they say so instead of showing old health.
 */

import { displayState } from "../../store/reducer";
import { useMission } from "../../store/store";
import { useUi } from "../../store/ui";
import { NODE_LAYOUT, STATUS_LOOK, nodeFacts } from "./layout";
import { registerLabel } from "./labelRegistry";

export function NodeLabels() {
  const subsystems = useMission((state) => state.subsystems);
  const model = useMission((state) => state.session?.model ?? null);
  const stale = useMission((state) => displayState(state) === "offline");
  const selected = useUi((ui) => ui.selected);
  const hovered = useUi((ui) => ui.hovered);
  const select = useUi((ui) => ui.select);
  const hover = useUi((ui) => ui.hover);
  return (
    <div className={`node-labels${stale ? " node-labels-stale" : ""}`} role="group" aria-label="Subsystems">
      {NODE_LAYOUT.map(({ id, label }) => {
        const view = subsystems[id];
        const facts = nodeFacts(id, view, model);
        const status = stale ? "NO LINK" : STATUS_LOOK[view.status].word;
        return (
          <button key={id} type="button" ref={registerLabel(id)} data-node={id}
            className={`node-label node-${stale ? "unknown" : view.status}${hovered === id ? " node-label-hover" : ""}`}
            aria-pressed={selected === id} aria-controls="node-detail"
            onClick={() => select(selected === id ? null : id)}
            aria-label={`${label}: ${status}${facts.length > 0 ? `. ${facts.join(". ")}` : ""}. Show details`}
            onPointerEnter={() => hover(id)} onPointerLeave={() => hover(null)}
            onFocus={() => hover(id)} onBlur={() => hover(null)}>
            <span className="node-name">{label}</span>
            <span className="node-status">{status}</span>
            {facts.map((fact) => <span key={fact} className="node-fact">{fact}</span>)}
          </button>
        );
      })}
    </div>
  );
}
