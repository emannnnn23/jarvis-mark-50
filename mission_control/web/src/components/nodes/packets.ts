/**
 * Data packets: small lights that run along a node's link when real work crosses it. Outbound packets
 * (core to node) mean JARVIS asked something of the subsystem; inbound ones (node to core) carry a
 * result back. Only events that move data trigger them: tool, llm, memory and voice in/out. voice.level
 * does not (it streams several times a second while armed); it drives the VOICE node's meter instead.
 *
 * The packets live in a fixed ring of 32 slots outside React, so a burst of events costs no renders and
 * no allocation: the scene reads the ring every frame and the oldest packet gives way to the newest.
 */

import type { ServerEvent, SubsystemId } from "../../types/events";

export const PACKET_LIMIT = 32;
/** Seconds a packet takes to cross its link. */
export const PACKET_TRAVEL_S = 0.9;
/** Seconds a link stays brightened after a packet arrives (travel + pulse stays inside the 1.5 s event window). */
export const PULSE_S = 0.55;

export type PacketDirection = "out" | "in";
export interface PacketSpec {
  node: SubsystemId;
  direction: PacketDirection;
  tone: "normal" | "attention";
}

const FAILED_TOOL = new Set(["error", "denied"]);

/** The packets one event sends, if any. */
export function packetsFor(event: ServerEvent): PacketSpec[] {
  switch (event.type) {
    case "llm.call":
      return [{ node: "llm", direction: event.phase === "start" ? "out" : "in", tone: event.ok === false ? "attention" : "normal" }];
    case "tool.execution":
      if (event.status === "awaiting_confirmation") return [];
      return [{
        node: "tools",
        direction: event.status === "requested" || event.status === "running" ? "out" : "in",
        tone: FAILED_TOOL.has(event.status) ? "attention" : "normal",
      }];
    case "memory.activity":
      return [{ node: "memory", direction: event.op === "store" ? "out" : "in", tone: "normal" }];
    case "voice.output":
      if (event.phase === "chunk") return [];
      return [{ node: "voice", direction: "out", tone: event.phase === "error" ? "attention" : "normal" }];
    case "voice.input":
      if (event.mode === "off" || event.phase === "stopped" || event.phase === "idle") return [];
      return [{ node: "voice", direction: "in", tone: "normal" }];
    default:
      return [];
  }
}

export interface PacketSlot extends PacketSpec {
  /** performance.now() / 1000 at launch; -Infinity for an empty slot. */
  startedAt: number;
}

function emptySlot(): PacketSlot {
  return { node: "llm", direction: "out", tone: "normal", startedAt: Number.NEGATIVE_INFINITY };
}

/** The ring the scene draws from. */
export const packetSlots: PacketSlot[] = Array.from({ length: PACKET_LIMIT }, emptySlot);
let nextSlot = 0;

export function launchPackets(specs: readonly PacketSpec[], now = performance.now() / 1000): void {
  for (const spec of specs) {
    packetSlots[nextSlot] = { ...spec, startedAt: now };
    nextSlot = (nextSlot + 1) % PACKET_LIMIT;
  }
}

/** Seconds since the newest packet on this node's link left, for the link pulse. */
export function lastPacketAge(node: SubsystemId, now: number): number {
  let newest = Number.NEGATIVE_INFINITY;
  for (const slot of packetSlots) if (slot.node === node && slot.startedAt > newest) newest = slot.startedAt;
  return now - newest;
}
