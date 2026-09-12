import hashlib
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from callouts.models import Announcement, AnnouncementRecipient, AnnouncementStats, Local, Member


class Command(BaseCommand):
    help = 'Seed locals and members for local development.'

    classifications = ['apprentice', 'journeyman', 'retiree', 'staff']
    local_specs = [
        ('Local 27', 2000),
        ('Local 99', 200),
    ]
    login_password = 'MemberCallout123!'

    def handle(self, *args, **options):
        total_created = 0
        total_updated = 0
        credentials = []
        announcement_summary = None

        with transaction.atomic():
            for local_name, member_count in self.local_specs:
                local, _ = Local.objects.get_or_create(name=local_name)
                created, updated = self.seed_members(local, member_count)
                credentials.extend(self.seed_login_accounts(local))
                total_created += created
                total_updated += updated

                if local.name == 'Local 27':
                    announcement_summary = self.seed_sent_announcement(local)

                self.stdout.write(
                    f'{local_name}: {created} created, {updated} updated'
                )

        self.stdout.write(
            self.style.SUCCESS(
                f'Seed data complete: {total_created} created, {total_updated} updated'
            )
        )
        if announcement_summary:
            self.print_announcement_summary(announcement_summary)
        self.print_credentials(credentials)

    def seed_members(self, local, member_count):
        existing_members = {
            member.email: member
            for member in Member.objects.filter(
                email__startswith=self.email_prefix(local),
                email__endswith='@seed.member-callout.local',
            )
        }
        members_to_create = []
        members_to_update = []

        for number in range(1, member_count + 1):
            email = self.member_email(local, number)
            defaults = self.member_defaults(local, number)

            member = existing_members.get(email)
            if member is None:
                member = Member(email=email, **defaults)
                member.set_unusable_password()
                members_to_create.append(member)
                continue

            changed = False
            for field, value in defaults.items():
                if getattr(member, field) != value:
                    setattr(member, field, value)
                    changed = True

            if changed:
                members_to_update.append(member)

        Member.objects.bulk_create(members_to_create, batch_size=500)
        Member.objects.bulk_update(
            members_to_update,
            ['local', 'full_name', 'classification', 'status', 'role', 'is_active'],
            batch_size=500,
        )

        return len(members_to_create), len(members_to_update)

    def seed_login_accounts(self, local):
        credentials = []

        for role in (Member.Role.LEADER, Member.Role.MEMBER):
            email = self.login_email(local, role)
            defaults = {
                'local': local,
                'full_name': f'{local.name} {role.label}',
                'classification': self.classifications[0],
                'status': Member.Status.ACTIVE,
                'role': role,
                'is_active': True,
            }
            member, _ = Member.objects.update_or_create(
                email=email,
                defaults=defaults,
            )
            member.set_password(self.login_password)
            member.save()
            credentials.append((local.name, role.label, email, self.login_password))

        return credentials

    def seed_sent_announcement(self, local):
        leader = Member.objects.get(email=self.login_email(local, Member.Role.LEADER))
        sent_at = timezone.now() - timedelta(days=3)
        queued_at = sent_at - timedelta(minutes=15)
        confirmed_at = queued_at - timedelta(hours=1)
        title = 'Local 27 Monthly Meeting Reminder'
        body = (
            'Reminder: Local 27 monthly meeting is this Thursday at 6:30 PM. '
            'Please review the agenda before arriving and RSVP if you plan to attend.'
        )
        push_preview = 'Local 27 meeting this Thursday at 6:30 PM.'
        content_hash = hashlib.sha256(f'{title}:{body}:{push_preview}'.encode()).hexdigest()

        announcement, _ = Announcement.objects.update_or_create(
            local=local,
            title=title,
            defaults={
                'created_by': leader,
                'body': body,
                'push_preview': push_preview,
                'target_classification': None,
                'needs_ack': True,
                'status': Announcement.Status.SENT,
                'confirmed_content_hash': content_hash,
                'confirmed_at': confirmed_at,
                'queued_at': queued_at,
                'sent_at': sent_at,
            },
        )

        members = list(Member.objects.filter(local=local, status=Member.Status.ACTIVE).order_by('email'))
        existing_recipients = {
            recipient.member_id: recipient
            for recipient in AnnouncementRecipient.objects.filter(announcement=announcement)
        }
        recipients_to_create = []
        recipients_to_update = []

        for index, member in enumerate(members, start=1):
            defaults = self.recipient_defaults(local, announcement, member, index, sent_at)
            recipient = existing_recipients.get(member.id)
            if recipient is None:
                recipients_to_create.append(AnnouncementRecipient(**defaults))
                continue

            changed = False
            for field, value in defaults.items():
                if getattr(recipient, field) != value:
                    setattr(recipient, field, value)
                    changed = True

            if changed:
                recipients_to_update.append(recipient)

        AnnouncementRecipient.objects.bulk_create(recipients_to_create, batch_size=500)
        AnnouncementRecipient.objects.bulk_update(
            recipients_to_update,
            [
                'local',
                'classification_snapshot',
                'delivery_status',
                'sent_at',
                'read_at',
                'acknowledged_at',
                'rsvp',
                'rsvp_at',
            ],
            batch_size=500,
        )

        stats = self.announcement_stats(announcement)
        AnnouncementStats.objects.update_or_create(
            announcement=announcement,
            defaults={'local': local, **stats},
        )

        return {
            'title': announcement.title,
            'recipients_created': len(recipients_to_create),
            'recipients_updated': len(recipients_to_update),
            **stats,
        }

    def recipient_defaults(self, local, announcement, member, index, sent_at):
        delivery_status = AnnouncementRecipient.DeliveryStatus.SENT
        recipient_sent_at = sent_at + timedelta(seconds=index % 900)
        read_at = None
        acknowledged_at = None
        rsvp = None
        rsvp_at = None

        if index % 50 == 0:
            delivery_status = AnnouncementRecipient.DeliveryStatus.FAILED
            recipient_sent_at = None
        elif index % 10 != 0:
            read_at = recipient_sent_at + timedelta(hours=index % 48)

            if index % 4 != 0:
                acknowledged_at = read_at + timedelta(minutes=5 + (index % 120))
                rsvp = 'cant_come' if index % 9 == 0 else 'coming'
                rsvp_at = acknowledged_at

        return {
            'local': local,
            'announcement': announcement,
            'member': member,
            'classification_snapshot': member.classification,
            'delivery_status': delivery_status,
            'sent_at': recipient_sent_at,
            'read_at': read_at,
            'acknowledged_at': acknowledged_at,
            'rsvp': rsvp,
            'rsvp_at': rsvp_at,
        }

    def announcement_stats(self, announcement):
        recipients = AnnouncementRecipient.objects.filter(announcement=announcement)

        return {
            'target_count': recipients.count(),
            'sent_count': recipients.filter(delivery_status=AnnouncementRecipient.DeliveryStatus.SENT).count(),
            'failed_count': recipients.filter(delivery_status=AnnouncementRecipient.DeliveryStatus.FAILED).count(),
            'read_count': recipients.filter(read_at__isnull=False).count(),
            'acknowledged_count': recipients.filter(acknowledged_at__isnull=False).count(),
            'coming_count': recipients.filter(rsvp='coming').count(),
            'cant_come_count': recipients.filter(rsvp='cant_come').count(),
        }

    def member_defaults(self, local, number):
        status = Member.Status.ACTIVE
        if number % 40 == 0:
            status = Member.Status.SUSPENDED
        elif number % 20 == 0:
            status = Member.Status.RETIRED

        return {
            'local': local,
            'full_name': f'{local.name} Member {number:04d}',
            'classification': self.classifications[(number - 1) % len(self.classifications)],
            'status': status,
            'role': Member.Role.LEADER if number % 100 == 0 else Member.Role.MEMBER,
            'is_active': status == Member.Status.ACTIVE,
        }

    def member_email(self, local, number):
        return f'{self.email_prefix(local)}{number:04d}@seed.member-callout.local'

    def login_email(self, local, role):
        local_number = local.name.lower().replace('local ', 'local')
        return f'{local_number}.{role}@seed.member-callout.local'

    def email_prefix(self, local):
        local_number = local.name.lower().replace('local ', 'local')
        return f'{local_number}.member'

    def print_credentials(self, credentials):
        self.stdout.write('')
        self.stdout.write('Login accounts:')
        for local_name, role, email, password in credentials:
            self.stdout.write(f'{local_name} {role}: {email} / {password}')

    def print_announcement_summary(self, summary):
        self.stdout.write('')
        self.stdout.write(f"Seeded announcement: {summary['title']}")
        self.stdout.write(
            'Recipients: '
            f"{summary['target_count']} target, "
            f"{summary['sent_count']} sent, "
            f"{summary['failed_count']} failed, "
            f"{summary['read_count']} read, "
            f"{summary['acknowledged_count']} acknowledged, "
            f"{summary['coming_count']} coming, "
            f"{summary['cant_come_count']} can't come"
        )
        self.stdout.write(
            f"Recipient records: {summary['recipients_created']} created, "
            f"{summary['recipients_updated']} updated"
        )
