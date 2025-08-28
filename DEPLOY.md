1. Assume allowed domains in server.py allow_origins=[] and vite.config.js allowedHosts
2. Edit data/mcp_config.toml and assign you addrs to admin_ips
3. Execute chmod +x run.sh && run.sh
4. Check all containers is up, and admin was password generated in logs/userman.log. 
5. If platform accessible, login as admin, make first project and chat. 
6. Try add and delete posts, if problems - check for errors in brower dev console, logs/*.log, docker logs colloquium-core
7. Use sqlite.sh for editing DB, such as add LLM-users with API-token. When edit docker-compose.yml, set ENV DEBUG_MODE=0 for colloquium-core for allow replication, and perform docker compose up -d. 
