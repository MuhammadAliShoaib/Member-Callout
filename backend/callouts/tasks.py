import logging
import socket
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from callouts.models import Announcement, AnnouncementRecipient, AnnouncementStats

logger = logging.getLogger(__name__)

MAX_RECIPIENT_BATCH_SIZE = 250
MAX_RETRY_BACKOFF_SECONDS = 300


class TemporaryDeliveryError(Exception):
    pass


class TerminalDeliveryError(Exception):
    pass


def worker_id():
    return socket.gethostname()


def recipient_claim_stale_before(now):
    return now - timedelta(seconds=settings.RECIPIENT_CLAIM_TIMEOUT_SECONDS)


def recipient_batch_size():
    return settings.RECIPIENT_DELIVERY_BATCH_SIZE


def retry_countdown(attempt_count):
    return min(2 ** max(attempt_count - 1, 0), MAX_RETRY_BACKOFF_SECONDS)


def claim_pending_recipients(recipient_ids, claimed_by):
    now = timezone.now()
    stale_before = recipient_claim_stale_before(now)

    with transaction.atomic():
        recipients = list(
            AnnouncementRecipient.objects.select_for_update(skip_locked=True)
            .only('id', 'claimed_at', 'claimed_by')
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
            batch_size=recipient_batch_size(),
        )

    return [recipient.id for recipient in recipients]


def recipients_for_delivery(recipient_ids):
    return (
        AnnouncementRecipient.objects.select_related('announcement', 'member')
        .only(
            'id',
            'attempt_count',
            'announcement_id',
            'member_id',
            'announcement__title',
            'announcement__push_preview',
            'member__email',
        )
        .filter(
            id__in=recipient_ids,
            delivery_status=AnnouncementRecipient.DeliveryStatus.PENDING,
        )
    )


def mark_recipients_sent(recipient_ids):
    sent_at = timezone.now()
    sent_counts = {}
    recipients_by_announcement = {}

    pending_recipients = AnnouncementRecipient.objects.filter(
        id__in=recipient_ids,
        delivery_status=AnnouncementRecipient.DeliveryStatus.PENDING,
    ).values_list('announcement_id', 'id')

    for announcement_id, recipient_id in pending_recipients:
        recipients_by_announcement.setdefault(announcement_id, []).append(recipient_id)

    for announcement_id, announcement_recipient_ids in recipients_by_announcement.items():
        updated = AnnouncementRecipient.objects.filter(
            id__in=announcement_recipient_ids,
            delivery_status=AnnouncementRecipient.DeliveryStatus.PENDING,
        ).update(
            delivery_status=AnnouncementRecipient.DeliveryStatus.SENT,
            sent_at=sent_at,
            claimed_at=None,
            claimed_by=None,
            last_error=None,
        )
        if updated:
            sent_counts[announcement_id] = updated

    return sent_counts


def mark_temporary_delivery_failure(recipient, error):
    next_attempt_count = recipient.attempt_count + 1
    delivery_status = AnnouncementRecipient.DeliveryStatus.PENDING
    retryable = next_attempt_count < settings.MAX_DELIVERY_ATTEMPTS

    if not retryable:
        delivery_status = AnnouncementRecipient.DeliveryStatus.FAILED

    updated = AnnouncementRecipient.objects.filter(
        id=recipient.id,
        delivery_status=AnnouncementRecipient.DeliveryStatus.PENDING,
    ).update(
        attempt_count=F('attempt_count') + 1,
        delivery_status=delivery_status,
        last_error=str(error),
        claimed_at=None,
        claimed_by=None,
    )
    return updated, retryable


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


def update_announcement_stats_for_delivery_batch(stats_counts):
    for announcement_id, counts in stats_counts.items():
        sent_count = counts.get('sent', 0)
        failed_count = counts.get('failed', 0)

        if not sent_count and not failed_count:
            continue

        AnnouncementStats.objects.filter(
            announcement_id=announcement_id,
        ).update(
            sent_count=F('sent_count') + sent_count,
            failed_count=F('failed_count') + failed_count,
            updated_at=timezone.now(),
        )


def reconcile_announcement_stats(announcement_id):
    with transaction.atomic():
        announcement = Announcement.objects.select_for_update().get(id=announcement_id)
        stats, _ = AnnouncementStats.objects.select_for_update().get_or_create(
            announcement=announcement,
            defaults={'local': announcement.local},
        )
        recipients = AnnouncementRecipient.objects.filter(announcement_id=announcement_id)
        pending_exists = recipients.filter(
            delivery_status=AnnouncementRecipient.DeliveryStatus.PENDING,
        ).exists()

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
        stats.save(update_fields=[
            'local',
            'target_count',
            'sent_count',
            'failed_count',
            'read_count',
            'acknowledged_count',
            'updated_at',
        ])

        if pending_exists:
            return False

        sent_at = timezone.now()
        updated = Announcement.objects.filter(
            id=announcement_id,
            status=Announcement.Status.QUEUED,
        ).update(
            status=Announcement.Status.SENT,
            sent_at=sent_at,
        )
        return bool(updated)


def reconcile_completed_announcements(announcement_ids):
    completed_count = 0

    for announcement_id in announcement_ids:
        completed_count += int(reconcile_announcement_stats(announcement_id))

    return completed_count


def add_stats_count(stats_counts, announcement_id, field, count):
    if not count:
        return

    counts = stats_counts.setdefault(announcement_id, {'sent': 0, 'failed': 0})
    counts[field] += count


def fake_push_delivery(recipient):
    payload = {
        'to': recipient.member.email,
        'title': recipient.announcement.title,
        'body': recipient.announcement.push_preview,
        'deep_link': (
            f'{settings.FRONTEND_URL}/member/announcements/{recipient.announcement_id}'
        ),
    }
    logger.info('Fake push notification: %s', payload)


@shared_task
def deliver_recipient_batch(recipient_ids):
    batch_size = recipient_batch_size()

    if len(recipient_ids) > batch_size:
        raise ValueError(f'deliver_recipient_batch accepts at most {batch_size} recipient IDs.')

    claimed_ids = claim_pending_recipients(recipient_ids, worker_id())
    recipients = list(recipients_for_delivery(claimed_ids))

    delivered_ids = []
    retryable_ids = []
    temporary_failures = 0
    terminal_failures = 0
    stats_counts = {}

    for recipient in recipients:
        try:
            fake_push_delivery(recipient)
        except TemporaryDeliveryError as exc:
            updated, retryable = mark_temporary_delivery_failure(recipient, exc)
            temporary_failures += updated
            if updated and retryable:
                retryable_ids.append(recipient.id)
            elif updated:
                add_stats_count(stats_counts, recipient.announcement_id, 'failed', updated)
        except TerminalDeliveryError as exc:
            updated = mark_terminal_delivery_failure(recipient, exc)
            terminal_failures += updated
            add_stats_count(stats_counts, recipient.announcement_id, 'failed', updated)
        else:
            delivered_ids.append(recipient.id)

    sent_counts = mark_recipients_sent(delivered_ids)
    processed = sum(sent_counts.values())

    for announcement_id, sent_count in sent_counts.items():
        add_stats_count(stats_counts, announcement_id, 'sent', sent_count)

    update_announcement_stats_for_delivery_batch(stats_counts)
    completed_announcements = reconcile_completed_announcements(stats_counts.keys())

    if retryable_ids:
        next_attempt_count = min(recipient.attempt_count + 1 for recipient in recipients if recipient.id in retryable_ids)
        deliver_recipient_batch.apply_async(
            args=[[str(recipient_id) for recipient_id in retryable_ids]],
            countdown=retry_countdown(next_attempt_count),
        )

    return {
        'processed': processed,
        'temporary_failures': temporary_failures,
        'terminal_failures': terminal_failures,
        'retryable': len(retryable_ids),
        'completed_announcements': completed_announcements,
    }
