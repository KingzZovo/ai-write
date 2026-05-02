
## Workspace panels API (2026-05-03)

```
curl -s -H "Authorization: Bearer $TOK" http://127.0.0.1:8000/api/projects/$PID/strand-status      | jq .tracker
curl -s -H "Authorization: Bearer $TOK" http://127.0.0.1:8000/api/projects/$PID/foreshadows        | jq '.total, .foreshadows[0]'
curl -s -H "Authorization: Bearer $TOK" http://127.0.0.1:8000/api/projects/$PID/characters         | jq '.total, .characters[0]'
curl -s -H "Authorization: Bearer $TOK" http://127.0.0.1:8000/api/projects/$PID/world-rules        | jq '.total, .rules[0]'
curl -s -H "Authorization: Bearer $TOK" http://127.0.0.1:8000/api/projects/$PID/relationships      | jq '.total, .relationships[0]'
curl -s -H "Authorization: Bearer $TOK" http://127.0.0.1:8000/api/projects/$PID/character-states   | jq '.total, .states[0]'
curl -s -H "Authorization: Bearer $TOK" "http://127.0.0.1:8000/api/stats/tokens?project_id=$PID&since_hours=24"
```

带 `character_id` 过滤：`?character_id=<uuid>`。
