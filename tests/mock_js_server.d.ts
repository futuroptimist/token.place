export interface MockServer {
  baseUrl: string;
  publicKey: string;
  stop(): Promise<void>;
}

export function startMockServer(): Promise<MockServer>;
