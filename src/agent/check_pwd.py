import hashlib
import binascii
import sqlite3

password = "colloqium"
client_hash = hashlib.sha256(password.encode()).hexdigest()
print(f"Client hash: {client_hash}")

conn = sqlite3.connect('/app/data/multichat.db')
cur = conn.cursor()
cur.execute("SELECT password_hash, salt FROM users WHERE user_name = 'admin'")
row = cur.fetchone()
if row:
    stored_hash, salt_hex = row
    salt = binascii.unhexlify(salt_hex)
    server_hash = hashlib.sha256(salt + client_hash.encode()).hexdigest()
    print(f"Stored hash: {stored_hash}")
    print(f"Computed hash: {server_hash}")
    print(f"Match: {server_hash == stored_hash}")
else:
    print("User 'admin' not found")
conn.close()
