async function generateKeys() {
    const keyPair = await window.crypto.subtle.generateKey(
      {
        name: "RSA-OAEP",
        modulusLength: 2048,
        publicExponent: new Uint8Array([1, 0, 1]),
        hash: "SHA-256"
      },
      true,
      ["encrypt", "decrypt"]
    );
  
    const publicKey = await window.crypto.subtle.exportKey("spki", keyPair.publicKey);
    const privateKey = await window.crypto.subtle.exportKey("pkcs8", keyPair.privateKey);
  
    const pemPublicKey = arrayBufferToPem(publicKey, "PUBLIC KEY");
    const pemPrivateKey = arrayBufferToPem(privateKey, "PRIVATE KEY");
  
    return { pemPrivateKey, pemPublicKey };
  }
  
  async function encrypt(plaintext, publicKeyPem) {
    const publicKey = await importPublicKey(publicKeyPem);
  
    const key = await window.crypto.subtle.generateKey(
      {
        name: "AES-CBC",
        length: 256
      },
      true,
      ["encrypt", "decrypt"]
    );
  
    const iv = window.crypto.getRandomValues(new Uint8Array(16));
  
    const ciphertext = await window.crypto.subtle.encrypt(
      {
        name: "AES-CBC",
        iv: iv
      },
      key,
      plaintext
    );
  
    const encryptedKey = await window.crypto.subtle.encrypt(
      {
        name: "RSA-OAEP"
      },
      publicKey,
      await window.crypto.subtle.exportKey("raw", key)
    );
  
    return {
      iv: arrayBufferToBase64(iv),
      ciphertext: arrayBufferToBase64(ciphertext),
      encryptedKey: arrayBufferToBase64(encryptedKey)
    };
  }
  
  async function decrypt(ciphertext, encryptedKey, iv, privateKeyPem) {
    const privateKey = await importPrivateKey(privateKeyPem);
  
    const decryptedKey = await window.crypto.subtle.decrypt(
      {
        name: "RSA-OAEP"
      },
      privateKey,
      base64ToArrayBuffer(encryptedKey)
    );
  
    const key = await window.crypto.subtle.importKey(
      "raw",
      decryptedKey,
      {
        name: "AES-CBC"
      },
      false,
      ["decrypt"]
    );
  
    const decryptedPlaintext = await window.crypto.subtle.decrypt(
      {
        name: "AES-CBC",
        iv: base64ToArrayBuffer(iv)
      },
      key,
      base64ToArrayBuffer(ciphertext)
    );
  
    return new TextDecoder().decode(decryptedPlaintext);
  }
  
  async function importPublicKey(pemKey) {
    const pemHeader = "-----BEGIN PUBLIC KEY-----";
    const pemFooter = "-----END PUBLIC KEY-----";
    const pemContents = pemKey.substring(pemHeader.length, pemKey.length - pemFooter.length);
    const binaryDerString = window.atob(pemContents);
    const binaryDer = stringToArrayBuffer(binaryDerString);
  
    return await window.crypto.subtle.importKey(
      "spki",
      binaryDer,
      {
        name: "RSA-OAEP",
        hash: "SHA-256"
      },
      true,
      ["encrypt"]
    );
  }
  
  async function importPrivateKey(pemKey) {
    const pemHeader = "-----BEGIN PRIVATE KEY-----";
    const pemFooter = "-----END PRIVATE KEY-----";
    const pemContents = pemKey.substring(pemHeader.length, pemKey.length - pemFooter.length);
    const binaryDerString = window.atob(pemContents);
    const binaryDer = stringToArrayBuffer(binaryDerString);
  
    return await window.crypto.subtle.importKey(
      "pkcs8",
      binaryDer,
      {
        name: "RSA-OAEP",
        hash: "SHA-256"
      },
      true,
      ["decrypt"]
    );
  }
  
  function arrayBufferToPem(arrayBuffer, label) {
    const base64 = arrayBufferToBase64(arrayBuffer);
    const pemHeader = `-----BEGIN ${label}-----\n`;
    const pemFooter = `\n-----END ${label}-----`;
    const pemContents = base64.match(/.{1,64}/g).join("\n");
    return pemHeader + pemContents + pemFooter;
  }
  
  function arrayBufferToBase64(arrayBuffer) {
    const byteArray = new Uint8Array(arrayBuffer);
    let byteString = "";
    for (let i = 0; i < byteArray.byteLength; i++) {
      byteString += String.fromCharCode(byteArray[i]);
    }
    return window.btoa(byteString);
  }
  
  function base64ToArrayBuffer(base64) {
    const binaryString = window.atob(base64);
    const len = binaryString.length;
    const bytes = new Uint8Array(len);
    for (let i = 0; i < len; i++) {
      bytes[i] = binaryString.charCodeAt(i);
    }
    return bytes.buffer;
  }
  
  function stringToArrayBuffer(str) {
    const buf = new ArrayBuffer(str.length);
    const bufView = new Uint8Array(buf);
    for (let i = 0, strLen = str.length; i < strLen; i++) {
      bufView[i] = str.charCodeAt(i);
    }
    return buf;
  }