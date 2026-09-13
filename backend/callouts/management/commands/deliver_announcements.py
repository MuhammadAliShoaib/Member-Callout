from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from callouts.models import AnnouncementRecipient
from callouts.tasks import (
    deliver_recipient_batch,
    recipient_batch_size,
    recipient_claim_stale_before,
)


def pending_recipient_ids_needing_delivery(now):
    stale_before = recipient_claim_stale_before(now)

    return (
        AnnouncementRecipient.objects.filter(
            delivery_status=AnnouncementRecipient.DeliveryStatus.PENDING,
        )
        .filter(Q(claimed_at__isnull=True) | Q(claimed_at__lt=stale_before))
        .order_by('created_at', 'id')
        .values_list('id', flat=True)
        .iterator(chunk_size=recipient_batch_size())
    )


def enqueue_recipient_delivery_batches(recipient_ids):
    enqueued_count = 0
    batch = []

    for recipient_id in recipient_ids:
        batch.append(str(recipient_id))

        if len(batch) == recipient_batch_size():
            enqueue_recipient_delivery_batch(batch)
            enqueued_count += len(batch)
            batch = []

    if batch:
        enqueue_recipient_delivery_batch(batch)
        enqueued_count += len(batch)

    return enqueued_count


def enqueue_recipient_delivery_batch(recipient_ids):
    deliver_recipient_batch.apply_async(args=[recipient_ids])


class Command(BaseCommand):
    help = 'Requeue pending announcement recipients for Celery delivery.'

    def handle(self, *args, **options):
        enqueued_count = enqueue_recipient_delivery_batches(
            pending_recipient_ids_needing_delivery(timezone.now())
        )

        if not enqueued_count:
            self.stdout.write('No pending announcement recipients need requeueing.')
            return

        self.stdout.write(
            self.style.SUCCESS(
                f'Enqueued {enqueued_count} pending announcement recipients for delivery.'
            )
        )
