# Member Callout

Secure local-union announcement delivery with leader and member workflows.

## Run

1. `cp backend/.env.example backend/.env` if needed.
2. `docker compose up --build`
3. `docker compose exec backend python manage.py seed_data`
4. Open `http://localhost:3000`; API is `http://localhost:8000`.

## Docker Compose

```sh
docker compose up --build
docker compose up -d
docker compose logs -f backend
docker compose exec backend python manage.py migrate
docker compose exec backend python manage.py seed_data
docker compose exec backend python manage.py test
docker compose down
```

## Demo Logins

| Role | Email | Password |
| --- | --- | --- |
| Local 27 leader | `local27.leader@seed.member-callout.local` | `MemberCallout123!` |
| Local 27 member | `local27.member@seed.member-callout.local` | `MemberCallout123!` |

## Curl Flow

```sh
API=http://localhost:8000
L27M=$(curl -s -X POST "$API/api/login/" -H 'Content-Type: application/json' -d '{"email":"local27.member@seed.member-callout.local","password":"MemberCallout123!"}' | jq -r .token)
curl -s -H "Authorization: Token $L27M" "$API/api/member/announcements/"
AID=$(curl -s -H "Authorization: Token $L27M" "$API/api/member/announcements/" | jq -r '.[0].id')
curl -s -H "Authorization: Token $L27M" "$API/api/member/announcements/$AID/"
curl -s -X POST -H "Authorization: Token $L27M" "$API/api/member/announcements/$AID/acknowledge/"
```

## Local Isolation

```sh
L27L=$(curl -s -X POST "$API/api/login/" -H 'Content-Type: application/json' -d '{"email":"local27.leader@seed.member-callout.local","password":"MemberCallout123!"}' | jq -r .token)
L99L=$(curl -s -X POST "$API/api/login/" -H 'Content-Type: application/json' -d '{"email":"local99.leader@seed.member-callout.local","password":"MemberCallout123!"}' | jq -r .token)
OTHER=$(curl -s -X POST "$API/api/announcements/" -H "Authorization: Token $L99L" -H 'Content-Type: application/json' -d '{"title":"Local 99 only","body":"Private","push_preview":"Private","needs_ack":true}' | jq -r .id)
curl -i -H "Authorization: Token $L27L" "$API/api/announcements/$OTHER/" # returns 404
```

## Rule 2 Verification

Rule 2 is enforced by `UNIQUE(announcement, member)` plus workers claiming only `pending` recipients. Verified with:

```sh
backend/.venv/bin/python backend/manage.py test callouts.tests.DeliveryTaskDatabaseTests.test_duplicate_tasks_do_not_process_sent_recipients_or_double_count_stats
```

That test retried the same recipient batch and proved the second task processed `0` rows, with no double-counted sends.
