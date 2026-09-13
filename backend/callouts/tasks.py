import logging

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from callouts.models import AnnouncementRecipient

logger = logging.getLogger(__name__)

MAX_RECIPIENT_BATCH_SIZE = 250


@shared_task
def deliver_recipient_batch(recipient_ids):
    if len(recipient_ids) > MAX_RECIPIENT_BATCH_SIZE:
        raise ValueError('deliver_recipient_batch accepts at most 250 recipient IDs.')

    now = timezone.now()

    with transaction.atomic():
        recipients = list(
            AnnouncementRecipient.objects.select_for_update(skip_locked=True)
            .select_related('announcement', 'member')
            .filter(
                id__in=recipient_ids,
                delivery_status=AnnouncementRecipient.DeliveryStatus.PENDING,
            )
        )

        for recipient in recipients:
            logger.info(
                'Fake push: %s | %s | %s',
                recipient.member.email,
                recipient.announcement.title,
                recipient.announcement.push_preview,
            )
            recipient.delivery_status = AnnouncementRecipient.DeliveryStatus.SENT
            recipient.sent_at = now

        AnnouncementRecipient.objects.bulk_update(
            recipients,
            ['delivery_status', 'sent_at'],
            batch_size=MAX_RECIPIENT_BATCH_SIZE,
        )

    return {'processed': len(recipients)}
