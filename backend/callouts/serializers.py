from rest_framework import serializers

from callouts.models import Announcement


class AnnouncementSerializer(serializers.ModelSerializer):
    content_editable = serializers.BooleanField(read_only=True)

    class Meta:
        model = Announcement
        fields = [
            'id',
            'local',
            'created_by',
            'title',
            'body',
            'push_preview',
            'target_classification',
            'needs_ack',
            'status',
            'content_editable',
            'confirmed_content_hash',
            'created_at',
            'confirmed_at',
            'queued_at',
            'sent_at',
        ]
        read_only_fields = [
            'id',
            'local',
            'created_by',
            'status',
            'confirmed_content_hash',
            'created_at',
            'confirmed_at',
            'queued_at',
            'sent_at',
        ]
