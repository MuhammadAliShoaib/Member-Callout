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

### User Prompt

Create AnnouncementRecipient.

Fields:
- id: UUID
- local
- announcement
- member
- classification_snapshot
- delivery_status: pending | sent | failed
- sent_at
- read_at
- acknowledged_at
- rsvp: nullable
- rsvp_at: nullable
- created_at

Add UNIQUE(announcement, member).

### User Prompt

Add database validation to AnnouncementRecipient.

Requirements:
- local must match announcement.local
- local must match member.local
- acknowledged_at cannot exist without read_at

Keep tenant isolation enforced wherever practical at database/model level.

## 2026-09-13

### User Prompt

Create AnnouncementStats.

Fields:
- announcement: one-to-one
- local
- target_count
- sent_count
- failed_count
- read_count
- acknowledged_count
- coming_count
- cant_come_count
- updated_at

All counters default to 0 and cannot be negative.

### User Prompt

Create a re-runnable Django command:

python manage.py seed_data

Create:
- Local 27
- Local 99
- ~2000 members in Local 27
- ~200 members in Local 99
- 3-4 classifications
- mostly active members

Do not create test accounts yet.

### User Prompt

Update seed_data to create:

- one leader account for Local 27
- one member account for Local 27
- one leader account for Local 99
- one member account for Local 99

Use deterministic emails and passwords.
Print the credentials after seeding.

### User Prompt

Update seed_data to create one already-sent announcement for Local 27.

Also create:
- recipient records
- realistic read/acknowledged states
- matching AnnouncementStats

The seed command must remain re-runnable without duplicates.

### User Prompt

Add simple API authentication using the Member model.

Create a login endpoint accepting:
- email
- password

Return what the frontend needs to authenticate subsequent requests.

Keep it simple for this technical exercise.

### User Prompt

Create a DRF permission that allows only active leaders.

A leader must also be restricted to their own local.

### User Prompt

Create reusable helpers for tenant-scoped queries.

Never trust local_id from request data.

Always derive local_id from:
request.user.local_id

### User Prompt

Add tests proving a Local 27 user cannot access Local 99 data and vice versa.

Test both leaders and members.

### User Prompt

Add tests proving normal members cannot perform leader operations.

Do not implement sending yet.

### User Prompt

Create:

POST /api/announcements/

Leader only.

Accept:
- title
- body
- push_preview
- target_classification
- needs_ack

Set local from request.user.local.
Set created_by from request.user.
Start status as draft.

### User Prompt

Create:

GET /api/announcements/{id}/

Leader only.

Only return the announcement if it belongs to the leader's local.

### User Prompt

Create:

POST /api/announcements/ai-draft/

Input:
{"note": "messy announcement text"}

Return:
- title
- body
- push_preview

push_preview must be <= 120 characters.

Do not send or save an announcement.

### User Prompt

Move AI generation behind an AI service class.

Provide:
- real provider interface
- fake local implementation

The project must work without evaluator AI credentials.

### User Prompt

Add AI error handling.

Handle:
- timeout
- provider failure
- invalid response

AI failure must never trigger announcement sending.

### User Prompt

Create an announcement confirmation endpoint.

Only leaders can confirm.

On confirmation:
- hash title + body + push_preview using SHA-256
- save confirmed_content_hash
- set confirmed_at
- change draft -> confirmed

### User Prompt

Create reusable helpers for tenant-scoped queries.

Never trust local_id from request data.

Always derive local_id from:
request.user.local_id
