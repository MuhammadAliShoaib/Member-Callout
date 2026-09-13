from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from callouts.models import Announcement, AnnouncementRecipient, AnnouncementStats


class Command(BaseCommand):
    help = 'Fake deliver pending announcement recipients.'

    def handle(self, *args, **options):
        now = timezone.now()

        with transaction.atomic():
            pending_recipients = list(
                AnnouncementRecipient.objects.select_for_update(skip_locked=True)
                .select_related(
                    'announcement',
                    'member',
                    'local',
                )
                .filter(delivery_status=AnnouncementRecipient.DeliveryStatus.PENDING)
                .order_by('created_at')[:1000]
            )

            if not pending_recipients:
                self.stdout.write('No pending announcement recipients.')
                return

            announcement_ids = set()

            for recipient in pending_recipients:
                self.log_delivery(recipient)
                recipient.delivery_status = AnnouncementRecipient.DeliveryStatus.SENT
                recipient.sent_at = now
                announcement_ids.add(recipient.announcement_id)

            AnnouncementRecipient.objects.bulk_update(
                pending_recipients,
                ['delivery_status', 'sent_at'],
                batch_size=500,
            )

            for announcement_id in announcement_ids:
                self.update_stats(announcement_id)
                self.mark_announcement_sent_if_complete(announcement_id, now)

        delivered_count = len(pending_recipients)
        self.stdout.write(self.style.SUCCESS(f'Delivered {delivered_count} notifications.'))

    def log_delivery(self, recipient):
        self.stdout.write(
            'Fake push: '
            f'{recipient.member.email} | '
            f'{recipient.announcement.title} | '
            f'{recipient.announcement.push_preview}'
        )

    def update_stats(self, announcement_id):
        recipients = AnnouncementRecipient.objects.filter(announcement_id=announcement_id)
        announcement = Announcement.objects.get(id=announcement_id)
        stats, _ = AnnouncementStats.objects.get_or_create(
            announcement=announcement,
            defaults={'local': announcement.local},
        )
        stats.local = announcement.local
        stats.target_count = recipients.values('member_id').distinct().count()
        stats.sent_count = recipients.filter(
            delivery_status=AnnouncementRecipient.DeliveryStatus.SENT,
        ).count()
        stats.failed_count = recipients.filter(
            delivery_status=AnnouncementRecipient.DeliveryStatus.FAILED,
        ).count()
        stats.read_count = recipients.filter(read_at__isnull=False).count()
        stats.acknowledged_count = recipients.filter(acknowledged_at__isnull=False).count()
        stats.coming_count = recipients.filter(rsvp='coming').count()
        stats.cant_come_count = recipients.filter(rsvp='cant_come').count()
        stats.save()

    def mark_announcement_sent_if_complete(self, announcement_id, sent_at):
        has_pending = AnnouncementRecipient.objects.filter(
            announcement_id=announcement_id,
            delivery_status=AnnouncementRecipient.DeliveryStatus.PENDING,
        ).exists()

        if has_pending:
            return

        Announcement.objects.filter(
            id=announcement_id,
            status=Announcement.Status.QUEUED,
        ).update(status=Announcement.Status.SENT, sent_at=sent_at)
