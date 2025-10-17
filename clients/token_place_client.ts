import JSEncrypt from 'jsencrypt';
import CryptoJS from 'crypto-js';

type ChatRole = 'system' | 'user' | 'assistant' | string;

export interface ChatMessage {
  role: ChatRole;
  content: string;
}

export interface TokenPlaceClientConfig {
  baseUrl: string;
  publicKeyPath?: string;
  chatCompletionsPath?: string;
  model?: string;
  encryption?: boolean;
}

export interface CreateChatCompletionParams {
  messages: ChatMessage[];
  model?: string;
}

export interface ChatCompletionChoice {
  index: number;
  message: ChatMessage;
  finish_reason: 'stop' | 'length' | 'tool_calls' | string;
}

export interface ChatCompletionResponse {
  id: string;
  object: 'chat.completion';
  choices: ChatCompletionChoice[];
}

interface EncryptedPayload {
  ciphertext: string;
  cipherkey: string;
  iv: string;
  client_public_key: string;
  model?: string;
  [key: string]: string | undefined;
}

interface KeyPair {
  publicKey: string;
  privateKey: string;
}

export class TokenPlaceClient {
  private readonly baseUrl: string;
  private readonly publicKeyPath: string;
  private readonly chatCompletionsPath: string;
  private readonly defaultModel: string;
  private readonly encryptionEnabled: boolean;

  private clientKeys: KeyPair | null = null;
  private serverPublicKey: string | null = null;

  constructor(config: TokenPlaceClientConfig) {
    if (!config.baseUrl) {
      throw new Error('TokenPlaceClient requires a baseUrl');
    }

    this.baseUrl = config.baseUrl.replace(/\/?$/, '');
    this.publicKeyPath = config.publicKeyPath ?? 'api/v1/public-key';
    this.chatCompletionsPath = config.chatCompletionsPath ?? 'api/v1/chat/completions';
    this.defaultModel = config.model ?? 'mock-llm';
    this.encryptionEnabled = config.encryption !== false;
  }

  async initialize(): Promise<void> {
    this.clientKeys = this.generateClientKeys();

    const response = await fetch(this.resolveUrl(this.publicKeyPath));
    if (!response.ok) {
      throw new Error(`Failed to fetch server public key (${response.status})`);
    }

    const payload = await response.json() as { public_key?: string };
    if (!payload.public_key) {
      throw new Error('Server response missing public_key field');
    }

    this.serverPublicKey = payload.public_key;
  }

  async createChatCompletion(params: CreateChatCompletionParams): Promise<ChatCompletionResponse> {
    if (!this.clientKeys || !this.serverPublicKey) {
      throw new Error('TokenPlaceClient not initialized. Call initialize() first.');
    }

    const model = params.model ?? this.defaultModel;

    let body: Record<string, unknown>;
    if (this.encryptionEnabled) {
      const lastMessage = params.messages[params.messages.length - 1];
      const requestPayload = {
        content: lastMessage?.content ?? '',
        messages: params.messages,
      };

      body = this.buildEncryptedPayload(requestPayload, model);
    } else {
      body = { model, messages: params.messages };
    }

    const response = await fetch(this.resolveUrl(this.chatCompletionsPath), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });

    if (!response.ok) {
      throw new Error(`Chat completion request failed (${response.status})`);
    }

    const payload = await response.json();

    if (!this.encryptionEnabled) {
      return payload as ChatCompletionResponse;
    }

    const decrypted = this.decryptPayload(payload as EncryptedPayload);
    let message: ChatMessage;
    try {
      message = JSON.parse(decrypted) as ChatMessage;
    } catch (error) {
      throw new Error(`Failed to parse decrypted payload: ${(error as Error).message}`);
    }

    return {
      id: 'mock-chat-completion',
      object: 'chat.completion',
      choices: [
        {
          index: 0,
          message,
          finish_reason: 'stop'
        }
      ]
    };
  }

  private buildEncryptedPayload(payload: unknown, model: string): EncryptedPayload {
    if (!this.clientKeys) {
      throw new Error('Client keys are not generated');
    }

    if (!this.serverPublicKey) {
      throw new Error('Server public key not available');
    }

    const plaintext = typeof payload === 'string' ? payload : JSON.stringify(payload);

    const aesKey = CryptoJS.lib.WordArray.random(32);
    const iv = CryptoJS.lib.WordArray.random(16);

    const encrypted = CryptoJS.AES.encrypt(
      CryptoJS.enc.Utf8.parse(plaintext),
      aesKey,
      {
        iv,
        mode: CryptoJS.mode.CBC,
        padding: CryptoJS.pad.Pkcs7
      }
    );

    const rsa = new JSEncrypt();
    rsa.setPublicKey(this.serverPublicKey);
    const encryptedKey = rsa.encrypt(CryptoJS.enc.Base64.stringify(aesKey));

    if (!encryptedKey) {
      throw new Error('Failed to RSA encrypt AES session key');
    }

    return {
      ciphertext: CryptoJS.enc.Base64.stringify(encrypted.ciphertext),
      cipherkey: encryptedKey,
      iv: CryptoJS.enc.Base64.stringify(iv),
      client_public_key: this.clientKeys.publicKey,
      model
    };
  }

  private decryptPayload(payload: EncryptedPayload): string {
    if (!this.clientKeys) {
      throw new Error('Client keys are not generated');
    }

    const rsa = new JSEncrypt();
    rsa.setPrivateKey(this.clientKeys.privateKey);
    const decryptedKeyBase64 = rsa.decrypt(payload.cipherkey);

    if (!decryptedKeyBase64) {
      throw new Error('Failed to decrypt AES session key');
    }

    const aesKey = CryptoJS.enc.Base64.parse(decryptedKeyBase64);
    const iv = CryptoJS.enc.Base64.parse(payload.iv);
    const ciphertext = CryptoJS.enc.Base64.parse(payload.ciphertext);

    const cipherParams = CryptoJS.lib.CipherParams.create({ ciphertext });

    const decrypted = CryptoJS.AES.decrypt(
      cipherParams,
      aesKey,
      {
        iv,
        mode: CryptoJS.mode.CBC,
        padding: CryptoJS.pad.Pkcs7
      }
    );

    const plaintext = CryptoJS.enc.Utf8.stringify(decrypted);
    if (!plaintext) {
      throw new Error('Failed to recover plaintext from encrypted payload');
    }

    return plaintext;
  }

  private generateClientKeys(): KeyPair {
    const crypt = new JSEncrypt({ default_key_size: '2048' });
    crypt.getKey();

    const privateKey = crypt.getPrivateKey();
    const publicKey = crypt.getPublicKey();

    if (!privateKey || !publicKey) {
      throw new Error('Failed to generate client RSA keypair');
    }

    return {
      privateKey,
      publicKey
    };
  }

  private resolveUrl(path: string): string {
    const trimmedBase = this.baseUrl.replace(/\/+$/, '');
    const trimmedPath = path.replace(/^\/+/, '');
    return `${trimmedBase}/${trimmedPath}`;
  }
}
