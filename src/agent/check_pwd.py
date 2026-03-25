import hashlib
import binascii
from managers.db import Database

password = "colloqium"
client_hash = hashlib.sha256(password.encode()).hexdigest()
print(f"Client hash: {client_hash}")

db = Database.get_database()
row = db.fetch_one("SELECT password_hash, salt FROM users WHERE user_name = :user_name", {"user_name": "admin"})
if row:
    stored_hash, salt_hex = row
    salt = binascii.unhexlify(salt_hex)
    server_hash = hashlib.sha256(salt + client_hash.encode()).hexdigest()
    print(f"Stored hash: {stored_hash}")
    print(f"Computed hash: {server_hash}")
    print(f"Match: {server_hash == stored_hash}")
else:
    print("User 'admin' not found")
