/**
 * The speech pulse as pure maths: the core brightens with each spoken word, using the word timings
 * edge-tts reports (voice.output "chunk", timing "exact"). The backend announces a chunk right before
 * the player starts it; the player takes about PLAYER_LATENCY_MS to make sound, so the words are laid
 * out from the moment the chunk arrived plus that latency. Without exact timings there is no timeline
 * and the core keeps its plain speaking breath.
 */

import type { SpeechChunk } from "../../store/reducer";

/** Time from the chunk event reaching the page to the first audio leaving the speakers. */
export const PLAYER_LATENCY_MS = 150;
/** A chunk whose event is older than this (a reconnect replay) is no longer playing: no pulse. */
export const STALE_CHUNK_MS = 3000;

export interface SpeechTimeline {
  /** performance.now() at which word offset 0 is heard. */
  start: number;
  words: readonly (readonly [number, number])[];
  /** ms after `start` when the last word ends: past it, the core goes back to its plain breath. */
  end: number;
}

/**
 * 0..1 at `t` ms into the chunk: a smooth bump across each word window, 1 at its middle and 0 at its
 * edges, and 0 between words. The square root makes a word's rise quick, the way a syllable starts.
 */
export function speechEnvelope(words: readonly (readonly [number, number])[], t: number): number {
  let level = 0;
  for (const [offset, duration] of words) {
    if (!(duration > 0) || t <= offset || t >= offset + duration) continue;
    level = Math.max(level, Math.sqrt(Math.sin((Math.PI * (t - offset)) / duration)));
  }
  return Math.min(1, level);
}

/**
 * The timeline for the chunk held in the store, given when this page received it (performance.now())
 * and the wall clock then. Null when there is nothing to pulse with.
 */
export function speechTimeline(
  chunk: SpeechChunk | null, receivedAt: number, nowEpochMs: number,
): SpeechTimeline | null {
  if (chunk === null || chunk.timing !== "exact" || chunk.words.length === 0) return null;
  if (nowEpochMs - chunk.timestamp > STALE_CHUNK_MS) return null;
  const end = chunk.words.reduce((latest, [offset, duration]) => Math.max(latest, offset + duration), 0);
  return { start: receivedAt + PLAYER_LATENCY_MS, words: chunk.words, end };
}

/** The envelope at performance.now() = `now`. */
export function pulseAt(timeline: SpeechTimeline | null, now: number): number {
  return timeline === null ? 0 : speechEnvelope(timeline.words, now - timeline.start);
}
