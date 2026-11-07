/**
 * The six node labels: name, status word and one real fact each, placed by the scene's projector.
 * Without a live link the statuses are not known any more, so they say so instead of showing old health.
 */

import { displayState } from "../../store/reducer";
import { useMission } from "../../store/store";
import { NODE_LAYOUT, STATUS_LOOK, nodeFact } from "./layout";
import { registerLabel } from "./labelRegistry";

export function NodeLabels() {
  const subsystems = useMission((state) => state.subsystems);
  const model = useMission((state) => state.session?.model ?? null);
  const stale = useMission((state) => displayState(state) === "offline");
  return (
    <div className={`node-labels${stale ? " node-labels-stale" : ""}`} aria-label="Subsystems">
      {NODE_LAYOUT.map(({ id, label }) => {
        const view = subsystems[id];
        const fact = nodeFact(id, view, model);
        return (
          <div key={id} ref={registerLabel(id)} className={`node-label node-${stale ? "unknown" : view.status}`}
            data-node={id}>
            <span className="node-name">{label}</span>
            <span className="node-status">{stale ? "NO LINK" : STATUS_LOOK[view.status].word}</span>
            {fact !== null && <span className="node-fact">{fact}</span>}
          </div>
        );
      })}
    </div>
  );
}
