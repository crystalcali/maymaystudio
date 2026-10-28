import base64
import hashlib
import hmac
import re
import secrets
from cryptography.fernet import Fernet
from .db import KV

def digest(token):return hashlib.sha256(token.encode()).hexdigest()
def token():return secrets.token_urlsafe(32)
def cipher(secret):return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))
def set_secret(db,key,value,secret):
    row=db.get(KV,key)
    data={'encrypted':cipher(secret).encrypt(value.encode()).decode()}
    if row:row.value=data
    else:db.add(KV(key=key,value=data))
def get_secret(db,key,secret):
    row=db.get(KV,key)
    return cipher(secret).decrypt(row.value['encrypted'].encode()).decode() if row else ''
def redact(text):
    text=re.sub(r'hf_[A-Za-z0-9]+','[HF_TOKEN]',str(text))
    text=re.sub(r'(?i)(authorization|x-api-key|token|api_key)([\s"\x27:=]+)[^\s,"\x27]+',r'\1\2[REDACTED]',text)
    return re.sub(r'https?://[^\s]+\?[^\s]+','[SIGNED_URL]',text)
