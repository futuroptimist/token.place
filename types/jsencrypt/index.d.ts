declare module 'jsencrypt' {
  export interface JSEncryptOptions {
    default_key_size?: string | number;
  }

  export default class JSEncrypt {
    constructor(options?: JSEncryptOptions);
    getKey(callback?: () => void): string | undefined;
    getPublicKey(): string;
    getPrivateKey(): string;
    setPublicKey(key: string): void;
    setPrivateKey(key: string): void;
    encrypt(value: string): string | false;
    decrypt(value: string): string | false;
  }
}
