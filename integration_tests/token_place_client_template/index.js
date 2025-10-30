const fetch = require('node-fetch');
const CryptoJS = require('crypto-js');
const jsencryptModule = require('jsencrypt');

const JSEncryptCtor =
  typeof jsencryptModule === 'function'
    ? jsencryptModule
    : jsencryptModule?.default || jsencryptModule?.JSEncrypt;

if (!JSEncryptCtor) {
  throw new Error('Unable to resolve JSEncrypt constructor from jsencrypt module');
}

function stripTrailingSlashes(value) {
  if (typeof value !== 'string' || value.length === 0) {
    return '';
  }

  let end = value.length;
  while (end > 0 && value.charCodeAt(end - 1) === 47) {
    end -= 1;
  }

  return value.slice(0, end);
}

function stripLeadingSlashes(value) {
  if (typeof value !== 'string' || value.length === 0) {
    return '';
  }

  let start = 0;
  while (start < value.length && value.charCodeAt(start) === 47) {
    start += 1;
  }

  return value.slice(start);
}

function resolveUrl(baseUrl, path) {
  const trimmedBase = stripTrailingSlashes(baseUrl);
  const trimmedPath = stripLeadingSlashes(path);

  if (!trimmedBase) {
    return `/${trimmedPath}`;
  }

  if (!trimmedPath) {
    return `${trimmedBase}/`;
  }

  return `${trimmedBase}/${trimmedPath}`;
}

function generateClientKeys() {
  const crypt = new JSEncryptCtor({ default_key_size: '2048' });
  crypt.getKey();
  const privateKey = crypt.getPrivateKey();
  const publicKey = crypt.getPublicKey();

  if (!privateKey || !publicKey) {
    throw new Error('Failed to generate RSA keypair for token.place client');
  }

  return { privateKey, publicKey };
}

function buildEncryptedPayload(payload, state, model) {
  const { clientKeys, serverPublicKey } = state;
  if (!clientKeys || !serverPublicKey) {
    throw new Error('Client not initialised before encrypting payload');
  }

  const plaintext =
    typeof payload === 'string' ? payload : JSON.stringify(payload ?? {});

  const aesKey = CryptoJS.lib.WordArray.random(32);
  const iv = CryptoJS.lib.WordArray.random(16);
  const encrypted = CryptoJS.AES.encrypt(
    CryptoJS.enc.Utf8.parse(plaintext),
    aesKey,
    {
      iv,
      mode: CryptoJS.mode.CBC,
      padding: CryptoJS.pad.Pkcs7,
    },
  );

  const rsa = new JSEncryptCtor();
  rsa.setPublicKey(serverPublicKey);
  const encryptedKey = rsa.encrypt(CryptoJS.enc.Base64.stringify(aesKey));

  if (!encryptedKey) {
    throw new Error('Failed to encrypt AES session key with server public key');
  }

  return {
    ciphertext: CryptoJS.enc.Base64.stringify(encrypted.ciphertext),
    cipherkey: encryptedKey,
    iv: CryptoJS.enc.Base64.stringify(iv),
    client_public_key: clientKeys.publicKey,
    model,
  };
}

function decryptPayload(payload, state) {
  const { clientKeys } = state;
  if (!clientKeys) {
    throw new Error('Client keys missing while decrypting response payload');
  }

  const rsa = new JSEncryptCtor();
  rsa.setPrivateKey(clientKeys.privateKey);
  const decryptedKeyBase64 = rsa.decrypt(payload.cipherkey);

  if (!decryptedKeyBase64) {
    throw new Error('Failed to decrypt AES session key from response payload');
  }

  const aesKey = CryptoJS.enc.Base64.parse(decryptedKeyBase64);
  const iv = CryptoJS.enc.Base64.parse(payload.iv);
  const ciphertext = CryptoJS.enc.Base64.parse(payload.ciphertext);
  const cipherParams = CryptoJS.lib.CipherParams.create({ ciphertext });

  const decrypted = CryptoJS.AES.decrypt(cipherParams, aesKey, {
    iv,
    mode: CryptoJS.mode.CBC,
    padding: CryptoJS.pad.Pkcs7,
  });

  const plaintext = CryptoJS.enc.Utf8.stringify(decrypted);
  if (!plaintext) {
    throw new Error('Failed to decode decrypted ciphertext from response payload');
  }

  return plaintext;
}

function createTokenPlaceClient(config = {}) {
  if (!config.baseUrl) {
    throw new Error('createTokenPlaceClient requires a baseUrl option');
  }

  const state = {
    clientKeys: null,
    serverPublicKey: null,
  };

  const options = {
    baseUrl: stripTrailingSlashes(config.baseUrl),
    publicKeyPath: config.publicKeyPath ?? 'api/v1/public-key',
    chatCompletionsPath: config.chatCompletionsPath ?? 'api/v1/chat/completions',
    model: config.model ?? 'mock-llm',
    encryption: config.encryption !== false,
  };

  async function initialise() {
    state.clientKeys = generateClientKeys();

    const response = await fetch(
      resolveUrl(options.baseUrl, options.publicKeyPath),
    );
    if (!response.ok) {
      throw new Error(
        `Failed to fetch token.place public key (${response.status})`,
      );
    }

    const payload = await response.json();
    if (!payload || typeof payload.public_key !== 'string') {
      throw new Error('token.place public key response missing public_key field');
    }

    state.serverPublicKey = payload.public_key;
  }

  async function createChatCompletion(params) {
    if (!Array.isArray(params?.messages) || params.messages.length === 0) {
      throw new Error('createChatCompletion requires at least one chat message');
    }

    if (options.encryption && (!state.clientKeys || !state.serverPublicKey)) {
      throw new Error('Client must be initialised before encrypted chat completions');
    }

    const model = params.model ?? options.model;
    const body = options.encryption
      ? buildEncryptedPayload(
          {
            content: params.messages[params.messages.length - 1]?.content ?? '',
            messages: params.messages,
          },
          state,
          model,
        )
      : { model, messages: params.messages };

    const response = await fetch(
      resolveUrl(options.baseUrl, options.chatCompletionsPath),
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      },
    );

    if (!response.ok) {
      throw new Error(`token.place chat completion failed (${response.status})`);
    }

    const payload = await response.json();

    if (!options.encryption) {
      return payload;
    }

    const decrypted = decryptPayload(payload, state);
    let message;
    try {
      message = JSON.parse(decrypted);
    } catch (error) {
      throw new Error(
        `Failed to parse decrypted chat completion payload: ${error.message}`,
      );
    }

    return {
      id: payload.id ?? 'token-place-chat-completion',
      object: 'chat.completion',
      choices: [
        {
          index: 0,
          message,
          finish_reason: 'stop',
        },
      ],
    };
  }

  return {
    initialise,
    initialize: initialise,
    createChatCompletion,
    getClientPublicKey: () => state.clientKeys?.publicKey ?? null,
    isEncryptionEnabled: () => options.encryption,
  };
}

module.exports = { createTokenPlaceClient };
