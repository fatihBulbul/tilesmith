// Sprite preloader. Given a set of tile keys, fetches PNG images in parallel
// and returns a map from key -> HTMLImageElement.

import type { MapState } from "./types";

export class SpriteCache {
  private cache = new Map<string, HTMLImageElement>();

  /** Collect every unique key that needs to be drawn (including animation frames). */
  collectKeys(state: MapState): Set<string> {
    const keys = new Set<string>();
    for (const layer of state.layers) {
      for (const row of layer.data) {
        for (const k of row) if (k) keys.add(k);
      }
    }
    for (const og of state.object_groups) {
      for (const o of og.objects) keys.add(o.key);
    }
    // Expand animation frame keys
    for (const k of Array.from(keys)) {
      const asset = state.tiles[k];
      if (!asset?.animation) continue;
      for (const fr of asset.animation) keys.add(fr.key);
    }
    return keys;
  }

  async loadAll(keys: Iterable<string>, baseUrl = ""): Promise<void> {
    const tasks: Promise<void>[] = [];
    for (const key of keys) {
      if (this.cache.has(key)) continue;
      tasks.push(this.loadOne(key, baseUrl));
    }
    await Promise.all(tasks);
  }

  private loadOne(key: string, baseUrl: string): Promise<void> {
    return new Promise((resolve) => {
      const img = new Image();
      img.onload = () => {
        this.cache.set(key, img);
        resolve();
      };
      img.onerror = () => {
        // Silently skip; consumer will see `get()` return undefined.
        console.warn(`[sprites] failed to load ${key}`);
        resolve();
      };
      img.src = `${baseUrl}/sprite/${encodeURIComponent(key)}.png`;
    });
  }

  get(key: string): HTMLImageElement | undefined {
    return this.cache.get(key);
  }

  size(): number {
    return this.cache.size;
  }

  clear(): void {
    this.cache.clear();
  }
}
