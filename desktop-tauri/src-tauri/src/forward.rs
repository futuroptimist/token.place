use aes::Aes256;
use anyhow::Context;
use base64::{engine::general_purpose::STANDARD, Engine};
use cbc::cipher::{block_padding::Pkcs7, BlockEncryptMut, KeyIvInit};
use rand::RngCore;
use reqwest::Client;
use rsa::{
    pkcs8::{DecodePublicKey, EncodePublicKey},
    Pkcs1v15Encrypt, RsaPrivateKey, RsaPublicKey,
};
use serde::{Deserialize, Serialize};

#[derive(Debug, Serialize)]
struct EncryptedMessages {
    ciphertext: String,
    cipherkey: String,
    iv: String,
}

#[derive(Debug, Serialize)]
struct EncryptedForwardBody {
    model: String,
    encrypted: bool,
    client_public_key: String,
    messages: EncryptedMessages,
}

#[derive(Debug, Deserialize)]
struct PublicKeyResponse {
    public_key: String,
}

type Aes256CbcEnc = cbc::Encryptor<Aes256>;

pub async fn encrypt_and_forward(
    relay_base_url: &str,
    plaintext_output: &str,
) -> anyhow::Result<String> {
    let client = Client::new();
    let key_url = format!("{}/api/v1/public-key", relay_base_url.trim_end_matches('/'));
    let key_payload = client
        .get(&key_url)
        .send()
        .await?
        .error_for_status()?
        .json::<PublicKeyResponse>()
        .await
        .context("relay public key response invalid")?;

    let payload = assemble_encrypted_forward_body(&key_payload.public_key, plaintext_output)?;
    let forward_url = format!(
        "{}/api/v1/chat/completions",
        relay_base_url.trim_end_matches('/')
    );

    let response = client
        .post(forward_url)
        .json(&payload)
        .send()
        .await?
        .error_for_status()?;
    Ok(format!("http {}", response.status()))
}

pub fn assemble_encrypted_forward_body(
    server_public_key_b64: &str,
    plaintext_output: &str,
) -> anyhow::Result<serde_json::Value> {
    let mut rng = rand::thread_rng();
    let server_public_key_der = STANDARD.decode(server_public_key_b64)?;
    let server_public = RsaPublicKey::from_public_key_der(&server_public_key_der)?;

    let mut aes_key = [0u8; 32];
    let mut iv = [0u8; 16];
    rng.fill_bytes(&mut aes_key);
    rng.fill_bytes(&mut iv);

    let plaintext = serde_json::to_vec(&vec![serde_json::json!({
        "role": "assistant",
        "content": plaintext_output
    })])?;

    let mut buf = vec![0u8; plaintext.len() + 16];
    buf[..plaintext.len()].copy_from_slice(&plaintext);
    let ciphertext = Aes256CbcEnc::new(&aes_key.into(), &iv.into())
        .encrypt_padded_mut::<Pkcs7>(&mut buf, plaintext.len())
        .map_err(|_| anyhow::anyhow!("aes encryption failed"))?
        .to_vec();

    let encrypted_key = server_public.encrypt(&mut rng, Pkcs1v15Encrypt, &aes_key)?;

    let client_private = RsaPrivateKey::new(&mut rng, 2048)?;
    let client_public_pem = client_private
        .to_public_key()
        .to_public_key_pem(Default::default())?;

    let body = EncryptedForwardBody {
        model: "llama-3-8b-instruct".to_string(),
        encrypted: true,
        client_public_key: STANDARD.encode(client_public_pem.as_bytes()),
        messages: EncryptedMessages {
            ciphertext: STANDARD.encode(ciphertext),
            cipherkey: STANDARD.encode(encrypted_key),
            iv: STANDARD.encode(iv),
        },
    };
    Ok(serde_json::to_value(body)?)
}

#[cfg(test)]
mod tests {
    use base64::{engine::general_purpose::STANDARD, Engine};
    use rsa::{pkcs8::EncodePublicKey, RsaPrivateKey};

    use super::assemble_encrypted_forward_body;

    #[test]
    fn encrypted_forward_contract_contains_expected_fields() {
        let mut rng = rand::thread_rng();
        let private = RsaPrivateKey::new(&mut rng, 2048).unwrap();
        let public_der = private.to_public_key().to_public_key_der().unwrap();

        let payload =
            assemble_encrypted_forward_body(&STANDARD.encode(public_der.as_ref()), "hello")
                .unwrap();
        assert_eq!(payload["encrypted"], true);
        assert!(payload["messages"]["ciphertext"].as_str().unwrap().len() > 20);
        assert!(payload["messages"]["cipherkey"].as_str().unwrap().len() > 20);
        assert!(payload["messages"]["iv"].as_str().unwrap().len() > 10);
        assert!(payload["client_public_key"].as_str().unwrap().len() > 20);
    }
}
