/** Options for {@link connect}. */
export interface ConnectOptions<T extends { id: string }> {
  /** Milliseconds before giving up. */
  timeout?: number
  retries: number
  onError(error: Error): void
}

/**
 * Open a connection.
 * @param url where to connect
 */
export declare function connect(url: string, options?: ConnectOptions<{ id: string }>): Promise<Connection>;
/** Overload taking a port. */
export declare function connect(port: number): Promise<Connection>;

export declare class Connection {
  /** Close it. */
  close(): void;
  readonly open: boolean;
  static from(url: string): Connection;
}

export declare namespace util {
  /** Join with a slash. */
  function join(...parts: string[]): string;
  const VERSION: string;
}

export type Handler = (event: string) => void

export declare const DEFAULT_TIMEOUT: number
export declare const enum Mode { Fast = 1, Slow = 2 }
