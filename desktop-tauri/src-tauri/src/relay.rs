use aes::Aes256;
use base64::{engine::general_purpose::STANDARD as B64, Engine};
use cbc::{
    cipher::{block_padding::Pkcs7, BlockEncryptMut, KeyIvInit},
    Encryptor,
};
use rand::RngCore;
use reqwest::StatusCode;
use rsa::{
    pkcs8::{DecodePublicKey, EncodePublicKey, LineEnding},
    Oaep, RsaPrivateKey, RsaPublicKey,
};
use serde::{Deserialize, Serialize};

#[derive(Debug, Serialize)]
pub struct RelayEnvelope {
    pub client_public_key: String,
    pub chat_history: String,
    pub cipherkey: String,
    pub iv: String,
}

#[derive(Debug)]
pub struct ForwardResult {
    pub status: u16,
    pub encrypted: bool,
}

#[derive(Debug, Deserialize)]
struct PublicKeyResponse {
    public_key: String,
}

pub async fn forward_output_encrypted(
    relay_base_url: &str,
    output: &str,
) -> Result<ForwardResult, Box<dyn std::error::Error>> {
    let client = reqwest::Client::new();

    let public_key_response = client
        .get(format!(
            "{}/public-key",
            relay_base_url.trim_end_matches('/')
        ))
        .send()
        .await?
        .json::<PublicKeyResponse>()
        .await?;

    let envelope = build_envelope(&public_key_response.public_key, output)?;

    let response = client
        .post(format!("{}/sink", relay_base_url.trim_end_matches('/')))
        .json(&envelope)
        .send()
        .await?;

    Ok(ForwardResult {
        status: response.status().as_u16(),
        encrypted: response.status() != StatusCode::BAD_REQUEST,
    })
}

pub fn build_envelope(
    server_public_key_b64: &str,
    output: &str,
) -> Result<RelayEnvelope, Box<dyn std::error::Error>> {
    let server_public_key_bytes = B64.decode(server_public_key_b64)?;
    let server_public_key =
        RsaPublicKey::from_public_key_pem(std::str::from_utf8(&server_public_key_bytes)?)?;

    let mut rng = rand::thread_rng();
    let client_private = RsaPrivateKey::new(&mut rng, 2048)?;
    let client_public = client_private
        .to_public_key()
        .to_public_key_pem(LineEnding::LF)?;

    let mut aes_key = [0_u8; 32];
    let mut iv = [0_u8; 16];
    rng.fill_bytes(&mut aes_key);
    rng.fill_bytes(&mut iv);

    let ciphertext = Encryptor::<Aes256>::new(&aes_key.into(), &iv.into())
        .encrypt_padded_vec_mut::<Pkcs7>(output.as_bytes());

    let encrypted_key =
        server_public_key.encrypt(&mut rng, Oaep::new::<sha2::Sha256>(), &aes_key)?;

    Ok(RelayEnvelope {
        client_public_key: client_public,
        chat_history: B64.encode(ciphertext),
        cipherkey: B64.encode(encrypted_key),
        iv: B64.encode(iv),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn build_envelope_matches_contract_shape() {
        let mut rng = rand::thread_rng();
        let private = RsaPrivateKey::new(&mut rng, 2048).unwrap();
        let public_pem = private
            .to_public_key()
            .to_public_key_pem(LineEnding::LF)
            .unwrap();
        let public_key_b64 = B64.encode(public_pem.as_bytes());

        let envelope = build_envelope(&public_key_b64, "hello world").unwrap();

        assert!(!envelope.client_public_key.is_empty());
        assert!(!envelope.chat_history.is_empty());
        assert!(!envelope.cipherkey.is_empty());
        assert!(!envelope.iv.is_empty());
    }
}
