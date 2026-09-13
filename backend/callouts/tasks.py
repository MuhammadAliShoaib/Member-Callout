import logging
import socket
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from callouts.models import AnnouncementRecipient

logger = logging.getLogger(__name__)

MAX_RECIPIENT_BATCH_SIZE = 250


class TemporaryDeliveryError(Exception):
    pass


class TerminalDeliveryError(Exception):
    pass


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
                attempt_count__lt=settings.MAX_DELIVERY_ATTEMPTS,
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
        claimed_at=None,
        claimed_by=None,
        last_error=None,
    )


def mark_temporary_delivery_failure(recipient, error):
    next_attempt_count = recipient.attempt_count + 1
    delivery_status = AnnouncementRecipient.DeliveryStatus.PENDING

    if next_attempt_count >= settings.MAX_DELIVERY_ATTEMPTS:
        delivery_status = AnnouncementRecipient.DeliveryStatus.FAILED

    return AnnouncementRecipient.objects.filter(
        id=recipient.id,
        delivery_status=AnnouncementRecipient.DeliveryStatus.PENDING,
    ).update(
        attempt_count=F('attempt_count') + 1,
        delivery_status=delivery_status,
        last_error=str(error),
        claimed_at=None,
        claimed_by=None,
    )


def mark_terminal_delivery_failure(recipient, error):
    return AnnouncementRecipient.objects.filter(
        id=recipient.id,
        delivery_status=AnnouncementRecipient.DeliveryStatus.PENDING,
    ).update(
        delivery_status=AnnouncementRecipient.DeliveryStatus.FAILED,
        last_error=str(error),
        claimed_at=None,
        claimed_by=None,
    )


def fake_push_delivery(recipient):
    logger.info(
        'Fake push: %s | %s | %s',
        recipient.member.email,
        recipient.announcement.title,
        recipient.announcement.push_preview,
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

    delivered_ids = []
    temporary_failures = 0
    terminal_failures = 0

    for recipient in recipients:
        try:
            fake_push_delivery(recipient)
        except TemporaryDeliveryError as exc:
            temporary_failures += mark_temporary_delivery_failure(recipient, exc)
        except TerminalDeliveryError as exc:
            terminal_failures += mark_terminal_delivery_failure(recipient, exc)
        else:
            delivered_ids.append(recipient.id)

    processed = mark_recipients_sent(delivered_ids)

    return {
        'processed': processed,
        'temporary_failures': temporary_failures,
        'terminal_failures': terminal_failures,
    }
