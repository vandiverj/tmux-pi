/** Join strings. */
export function joinAll(parts: string[], sep = ","): string {
  const BODY_MARKER_JOIN = parts.join(sep);
  return BODY_MARKER_JOIN;
}

export { pad } from "./util.ts";

/** A counter. */
export class Counter<T extends { id: string }> {
  #hidden = 1;
  private secret = "BODY_MARKER_FIELD";
  count = 0;
  constructor(public readonly label: string) {
    this.count = BODY_MARKER_CTOR;
  }
  /** Add one. */
  increment(by: number = 1): number {
    return (this.count += by) + BODY_MARKER_METHOD;
  }
  private hide(): void {
    BODY_MARKER_PRIVATE;
  }
}

export const VERSION = "1.4.0";
export const double = (n: number): number => {
  return n * 2 + BODY_MARKER_ARROW;
};
export const config = { token: "BODY_MARKER_CONFIG" };

function notExported(): void {
  BODY_MARKER_HIDDEN;
}
