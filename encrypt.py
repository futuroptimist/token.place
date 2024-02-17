import rsa

def generate_keys():
    """
    Generate an RSA keypair with an exponent of 65537 in PEM format
    :return: private_key, public_key
    """
    (public_key, private_key) = rsa.newkeys(2048)
    return private_key, public_key

def save_private_key(private_key, filename):
    """
    Save the private key to a PEM file
    """
    with open(filename, 'wb') as f:
        f.write(private_key.save_pkcs1('PEM'))

def save_public_key(public_key, filename):
    """
    Save the public key to a PEM file
    """
    with open(filename, 'wb') as f:
        f.write(public_key.save_pkcs1('PEM'))

def load_private_key(filename):
    """
    Load the private key from a PEM file
    """
    with open(filename, 'rb') as f:
        return rsa.PrivateKey.load_pkcs1(f.read())

def load_public_key(filename):
    """
    Load the public key from a PEM file
    """
    with open(filename, 'rb') as f:
        return rsa.PublicKey.load_pkcs1(f.read())

def encrypt_message(message_bytes, public_key):
    """
    Encrypt a message with the public key.
    :param message_bytes: Message in bytes to be encrypted.
    :param public_key: The RSA public key object for encryption.
    :return: Encrypted message as bytes.
    """
    return rsa.encrypt(message_bytes, public_key)

def decrypt_message(encrypted_message, private_key):
    """
    Decrypt a message with the private key
    """
    try:
        return rsa.decrypt(encrypted_message, private_key).decode('utf8')
    except rsa.DecryptionError as e:
        print("Decryption failed:", e)
        return None
    
def public_key_from_pem(pem_str):
    """
    Load an RSA public key from a PEM string.
    :param pem_str: The PEM string of the public key, expected to be in bytes format.
    :return: The RSA public key object.
    """
    # Directly pass the pem_str without encoding it
    return rsa.PublicKey.load_pkcs1(pem_str)