// Create a more complete mock browser environment for jsencrypt in Node.js
const crypto = require('crypto');

// Mock window object
global.window = global;

// Add navigator object
global.navigator = {
    userAgent: 'node',
    appName: 'Node.js',
    appVersion: process.version
};

// Add document object
global.document = {
    createElement: function() {
        return {
            style: {},
            querySelector: function() { return null; }
        };
    },
    getElementsByTagName: function() { return []; },
    documentElement: { style: {} }
};

// More complete crypto object
global.crypto = {
    getRandomValues: function(buffer) {
        return crypto.randomFillSync(buffer);
    },
    subtle: {
        // Add any subtle crypto methods needed
        digest: async function() { return new Uint8Array(32); }
    }
};

// Fix for TextEncoder if needed
if (typeof global.TextEncoder === 'undefined') {
    global.TextEncoder = require('util').TextEncoder;
}

// Fix for TextDecoder if needed
if (typeof global.TextDecoder === 'undefined') {
    global.TextDecoder = require('util').TextDecoder;
}

// Mock localStorage
global.localStorage = {
    getItem: function() { return null; },
    setItem: function() {},
    removeItem: function() {}
};

// Add btoa and atob for Base64 encoding/decoding
global.btoa = function(str) {
    return Buffer.from(str, 'binary').toString('base64');
};

global.atob = function(b64Encoded) {
    return Buffer.from(b64Encoded, 'base64').toString('binary');
};
