use aes::Aes256;
use anyhow::{anyhow, Result};
use base64::Engine;
use cbc::Encryptor;
use cipher::{block_padding::Pkcs7, BlockEncryptMut, KeyIvInit};
use rand::RngCore;
use rsa::pkcs8::{DecodePublicKey, EncodePublicKey, LineEnding};
use rsa::{Pkcs1v15Encrypt, RsaPrivateKey, RsaPublicKey};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ForwardEnvelope {
    pub client_public_key: String,
    pub server_public_key: String,
    pub chat_history: String,
    pub cipherkey: String,
    pub iv: String,
}

pub fn build_faucet_envelope(plaintext: &str, server_public_key_b64: &str) -> Result<ForwardEnvelope> {
    let server_der = base64::engine::general_purpose::STANDARD.decode(server_public_key_b64)?;
    let server_pem = String::from_utf8(server_der)?;
    let server_pub = RsaPublicKey::from_public_key_pem(&server_pem)?;

    let mut rng = rand::thread_rng();
    let mut aes_key = [0u8; 32];
    let mut iv = [0u8; 16];
    rng.fill_bytes(&mut aes_key);
    rng.fill_bytes(&mut iv);

    let cipher = Encryptor::<Aes256>::new_from_slices(&aes_key, &iv)?;
    let mut buf = vec![0u8; plaintext.len() + 16];
    buf[..plaintext.len()].copy_from_slice(plaintext.as_bytes());
    let ciphertext = cipher
        .encrypt_padded_mut::<Pkcs7>(&mut buf, plaintext.len())
        .map_err(|_| anyhow!("encryption failed"))?
        .to_vec();

    let enc_key = server_pub.encrypt(&mut rng, Pkcs1v15Encrypt, &aes_key)?;

    let client_priv = RsaPrivateKey::new(&mut rng, 2048)?;
    let client_pub_pem = client_priv
        .to_public_key()
        .to_public_key_pem(LineEnding::LF)?;

    Ok(ForwardEnvelope {
        client_public_key: base64::engine::general_purpose::STANDARD
            .encode(client_pub_pem.as_bytes()),
        server_public_key: server_public_key_b64.to_string(),
        chat_history: base64::engine::general_purpose::STANDARD.encode(ciphertext),
        cipherkey: base64::engine::general_purpose::STANDARD.encode(enc_key),
        iv: base64::engine::general_purpose::STANDARD.encode(iv),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn envelope_contains_expected_fields() {
        let mut rng = rand::thread_rng();
        let private = RsaPrivateKey::new(&mut rng, 2048).expect("key");
        let public_pem = private.to_public_key().to_public_key_pem(LineEnding::LF).expect("pem");
        let public_b64 = base64::engine::general_purpose::STANDARD.encode(public_pem.as_bytes());

        let envelope = build_faucet_envelope("hello", &public_b64).expect("envelope");

        assert!(!envelope.client_public_key.is_empty());
        assert!(!envelope.chat_history.is_empty());
        assert!(!envelope.cipherkey.is_empty());
        assert!(!envelope.iv.is_empty());
    }
}
