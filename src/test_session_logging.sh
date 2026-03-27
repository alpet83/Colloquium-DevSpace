#!/bin/bash
# Test session logging from inside container

# Session A
echo "=== Testing Session A ==="
SESSION_A=$(curl -s -c /tmp/cq_a.cookie -H "Content-Type: application/json" \
  -d '{"username":"copilot","password":"devspace"}' \
  http://localhost:8080/login | jq -r '.session_id // empty')

echo "Session A: $SESSION_A"

# Select project
curl -s -b /tmp/cq_a.cookie "http://localhost:8080/project/select?project_id=1" > /dev/null
echo "Project selected for A"

# Make a request
sleep 0.5
curl -s -b /tmp/cq_a.cookie -X POST \
  -H "Content-Type: application/json" \
  -d '{"chat_id": 1, "message": "test-A"}' \
  http://localhost:8080/chat/post > /dev/null
echo "Message posted for A"

# Session B
echo ""
echo "=== Testing Session B ==="
SESSION_B=$(curl -s -c /tmp/cq_b.cookie -H "Content-Type: application/json" \
  -d '{"username":"copilot","password":"devspace"}' \
  http://localhost:8080/login | jq -r '.session_id // empty')

echo "Session B: $SESSION_B"

# Select project
curl -s -b /tmp/cq_b.cookie "http://localhost:8080/project/select?project_id=2" > /dev/null
echo "Project selected for B"

# Make a request
sleep 0.5
curl -s -b /tmp/cq_b.cookie -X POST \
  -H "Content-Type: application/json" \
  -d '{"chat_id": 1, "message": "test-B"}' \
  http://localhost:8080/chat/post > /dev/null
echo "Message posted for B"

# Check logs
echo ""
echo "=== Checking logs ==="
sleep 1

echo "Lines with Session A prefix (${SESSION_A:0:8}):"
tail -50 /app/logs/chatman.log | grep "${SESSION_A:0:8}" | head -3

echo ""
echo "Lines with Session B prefix (${SESSION_B:0:8}):"
tail -50 /app/logs/chatman.log | grep "${SESSION_B:0:8}" | head -3

echo ""
echo "Lines with color code ~C34 (dark blue):"
tail -50 /app/logs/chatman.log | grep "~C34" | head -5
