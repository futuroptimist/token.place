use aes::Aes256;
use base64::Engine;
use cbc::cipher::block_padding::Pkcs7;
use cbc::cipher::{BlockEncryptMut, KeyIvInit};
use rand::rngs::OsRng;
use rand::RngCore;
use rsa::pkcs8::DecodePublicKey;
use rsa::{Oaep, RsaPublicKey};
use serde::{Deserialize, Serialize};

#[derive(Debug, Serialize, Deserialize)]
pub struct RelayEnvelope {
    pub client_public_key: String,
    pub server_public_key: String,
    pub chat_history: String,
    pub cipherkey: String,
    pub iv: String,
}

pub async fn encrypt_and_forward(_relay_base_url: &str, _final_output: &str) -> anyhow::Result<()> {
    anyhow::bail!(
        "debug relay forwarding is disabled; use the API v1 relay E2EE desktop bridge path"
    )
}

pub fn assemble_relay_envelope(
    server_public_key_b64: &str,
    client_public_key_b64: &str,
    plaintext: &[u8],
) -> anyhow::Result<RelayEnvelope> {
    let mut aes_key = [0u8; 32];
    let mut iv = [0u8; 16];
    OsRng.fill_bytes(&mut aes_key);
    OsRng.fill_bytes(&mut iv);

    type Aes256CbcEnc = cbc::Encryptor<Aes256>;
    let ciphertext = Aes256CbcEnc::new((&aes_key).into(), (&iv).into())
        .encrypt_padded_vec_mut::<Pkcs7>(plaintext);

    let server_public_pem = String::from_utf8(
        base64::engine::general_purpose::STANDARD.decode(server_public_key_b64)?,
    )?;
    let rsa_public = RsaPublicKey::from_public_key_pem(&server_public_pem)?;
    let key_b64 = base64::engine::general_purpose::STANDARD.encode(aes_key);
    let encrypted_key =
        rsa_public.encrypt(&mut OsRng, Oaep::new::<sha2::Sha256>(), key_b64.as_bytes())?;

    Ok(RelayEnvelope {
        client_public_key: client_public_key_b64.to_string(),
        server_public_key: server_public_key_b64.to_string(),
        chat_history: base64::engine::general_purpose::STANDARD.encode(ciphertext),
        cipherkey: base64::engine::general_purpose::STANDARD.encode(encrypted_key),
        iv: base64::engine::general_purpose::STANDARD.encode(iv),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn assembles_expected_contract_keys() {
        let (server_pub_b64, _priv_b64) = crate::keygen::generate_rsa_keypair_b64().expect("keys");
        let (client_pub_b64, _client_priv_b64) =
            crate::keygen::generate_rsa_keypair_b64().expect("keys");
        let envelope =
            assemble_relay_envelope(&server_pub_b64, &client_pub_b64, b"hello").expect("envelope");

        assert!(!envelope.chat_history.is_empty());
        assert!(!envelope.cipherkey.is_empty());
        assert!(!envelope.iv.is_empty());
        assert_eq!(envelope.server_public_key, server_pub_b64);
        assert_eq!(envelope.client_public_key, client_pub_b64);
    }
}
