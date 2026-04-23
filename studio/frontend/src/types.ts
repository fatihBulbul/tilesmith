// MapState protocol — mirror of tmx_state.build_map_state()

export interface AnimFrame {
  key: string;
  duration: number; // ms
}

export interface TileAsset {
  w: number;
  h: number;
  sprite_url: string;
  animation?: AnimFrame[];
}

export interface TileLayer {
  name: string;
  type: "tile";
  visible: boolean;
  opacity: number;
  data: (string | null)[][];
}

export interface MapObject {
  id: number;
  key: string;
  x: number;
  y: number; // Tiled bottom-left anchor
  w: number;
  h: number;
}

export interface ObjectGroup {
  name: string;
  visible: boolean;
  opacity: number;
  objects: MapObject[];
}

export interface MapState {
  tmx_path?: string;
  width: number;
  height: number;
  tile_w: number;
  tile_h: number;
  layers: TileLayer[];
  object_groups: ObjectGroup[];
  tiles: Record<string, TileAsset>;
  empty?: boolean;
}

// WS events
export interface PaintPatchEvent {
  type: "patch";
  op: "paint";
  layer: string;
  cells: { x: number; y: number; key: string | null }[];
  wang?: {
    wangset_uid: string;
    color: number;
    cells_touched: number;
    erase?: boolean;
  };
}

// Wang palette metadata (served by bridge GET /wang/sets)
export interface WangColor {
  color_index: number;
  name: string | null;
  color_hex: string | null;
}
export interface WangSet {
  pack_name: string;
  wangset_uid: string;
  tileset: string;
  name: string;
  type: "corner" | "edge" | "mixed" | string;
  color_count: number;
  tile_count: number;
  supported: boolean;  // false → corner resolver can't handle (edge/mixed)
  colors: WangColor[];
}

export interface ObjectPatchEvent {
  type: "patch";
  op: "object";
  group: string;
  patch: {
    op: "move" | "delete" | "set_key";
    id: number;
    x?: number;
    y?: number;
    key?: string;
  };
}

export interface Selection {
  layer: string;
  x0: number; y0: number;  // inclusive, min corner
  x1: number; y1: number;  // inclusive, max corner
}

export interface SelectionEvent {
  type: "selection";
  selection: Selection | null;
}

export type WsEvent =
  | { type: "map_loaded"; state: MapState }
  | { type: "pong" }
  | { type: "error"; message: string }
  | PaintPatchEvent
  | ObjectPatchEvent
  | SelectionEvent;

// Tool modes
export type ToolMode = "view" | "paint" | "erase" | "select" | "wang";

export interface WangSelection {
  wangset_uid: string;
  color: number;  // color_index
  color_hex?: string | null;
  color_name?: string | null;
}
