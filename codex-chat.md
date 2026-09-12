# Codex Chat Log

## 2026-09-12

### User Prompt

Whatever the prompts i am going to give you write them in the codex-chat.md file also for record purpose

### User Prompt

Configure this Django project to use Django REST Framework.

Add:
- rest_framework
- corsheaders
- callouts

Configure CORS for a Next.js frontend running on localhost:3000.
Keep the configuration simple.

### User Prompt

Configure Django to use PostgreSQL.

Read these values from environment variables:
POSTGRES_DB
POSTGRES_USER
POSTGRES_PASSWORD
POSTGRES_HOST
POSTGRES_PORT

Also read SECRET_KEY and DEBUG from environment variables.

### User Prompt

add env file along with env example and use all those variables for settings

### User Prompt

Add GET /api/health/.

Return:
{"status": "ok"}

Keep it minimal.

### User Prompt

Create a Local model in the callouts app.

Fields:
- id: UUID primary key
- name: string

Add created_at.

### User Prompt

Create the Member model.

Member is the authentication user model.

Fields:
- id: UUID
- local: FK to Local
- full_name
- email: unique
- classification
- status: active | retired | suspended
- role: member | leader
- is_active

Use email for authentication.

A Member belongs to exactly one Local.

### User Prompt

Configure Django to use Member as AUTH_USER_MODEL.

Make sure authentication and password hashing work correctly.

### User Prompt

Create the Announcement model.

Fields:
- id: UUID
- local
- created_by
- title
- body
- push_preview
- target_classification: nullable
- needs_ack
- status
- confirmed_content_hash
- created_at
- confirmed_at
- queued_at
- sent_at

Status:
draft | confirmed | queued | sent
