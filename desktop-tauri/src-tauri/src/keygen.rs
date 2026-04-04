use base64::Engine;
use rand::rngs::OsRng;
use rsa::pkcs8::{EncodePrivateKey, EncodePublicKey, LineEnding};
use rsa::{RsaPrivateKey, RsaPublicKey};

pub fn generate_rsa_keypair_pem() -> anyhow::Result<(String, String)> {
    let private = RsaPrivateKey::new(&mut OsRng, 2048)?;
    let public = RsaPublicKey::from(&private);
    Ok((
        public.to_public_key_pem(LineEnding::LF)?,
        private.to_pkcs8_pem(LineEnding::LF)?.to_string(),
    ))
}

pub fn generate_rsa_keypair_b64() -> anyhow::Result<(String, String)> {
    let (public_pem, private_pem) = generate_rsa_keypair_pem()?;
    Ok((
        base64::engine::general_purpose::STANDARD.encode(public_pem.as_bytes()),
        base64::engine::general_purpose::STANDARD.encode(private_pem.as_bytes()),
    ))
}
