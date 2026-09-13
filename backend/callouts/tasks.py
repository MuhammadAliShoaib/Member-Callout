import logging
import socket
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from callouts.models import AnnouncementRecipient

logger = logging.getLogger(__name__)

MAX_RECIPIENT_BATCH_SIZE = 250


def worker_id():
    return socket.gethostname()


def recipient_claim_stale_before(now):
    return now - timedelta(seconds=settings.RECIPIENT_CLAIM_TIMEOUT_SECONDS)


def claim_pending_recipients(recipient_ids, claimed_by):
    now = timezone.now()
    stale_before = recipient_claim_stale_before(now)

    with transaction.atomic():
        recipients = list(
            AnnouncementRecipient.objects.select_for_update(skip_locked=True)
            .filter(
                id__in=recipient_ids,
                delivery_status=AnnouncementRecipient.DeliveryStatus.PENDING,
            )
            .filter(Q(claimed_at__isnull=True) | Q(claimed_at__lt=stale_before))
        )

        for recipient in recipients:
            recipient.claimed_at = now
            recipient.claimed_by = claimed_by

        AnnouncementRecipient.objects.bulk_update(
            recipients,
            ['claimed_at', 'claimed_by'],
            batch_size=MAX_RECIPIENT_BATCH_SIZE,
        )

    return [recipient.id for recipient in recipients]


def mark_recipients_sent(recipient_ids):
    sent_at = timezone.now()
    return AnnouncementRecipient.objects.filter(
        id__in=recipient_ids,
        delivery_status=AnnouncementRecipient.DeliveryStatus.PENDING,
    ).update(
        delivery_status=AnnouncementRecipient.DeliveryStatus.SENT,
        sent_at=sent_at,
        last_error=None,
    )


@shared_task
def deliver_recipient_batch(recipient_ids):
    if len(recipient_ids) > MAX_RECIPIENT_BATCH_SIZE:
        raise ValueError('deliver_recipient_batch accepts at most 250 recipient IDs.')

    claimed_ids = claim_pending_recipients(recipient_ids, worker_id())
    recipients = list(
        AnnouncementRecipient.objects.select_related('announcement', 'member').filter(
            id__in=claimed_ids,
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

    processed = mark_recipients_sent([recipient.id for recipient in recipients])

    return {'processed': processed}
